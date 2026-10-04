"""Extract, verify, then atomically apply source-backed memory changes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TypedDict

from dynamic_graph.models.client import ModelCallError, ModelClient, ModelRequest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .contracts import Extraction, Scope, StoredMemory, Verification, utcnow
from .prompts import EXTRACT, MEMORY_SYSTEM, PROMPT_VERSION, REPAIR, VERIFY
from .storage import Storage, digest, encode, stable_id


def direct_fact_evidence(event, sources):
    return event.event_type in {"user_message", "task_result"} and any(
        source.event_id == event.event_id and source.source_role in {"user", "tool"}
        for source in sources
    )


class ExtractionState(TypedDict, total=False):
    event_id: str
    extraction: Extraction
    verification: Verification
    allowed_events: dict
    existing: dict[str, StoredMemory]
    revision: int
    model_info: dict


class EvidenceValidationError(ValueError):
    def __init__(self, code, evidence):
        super().__init__(code)
        self.feedback = {"code": code, "invalid_evidence": evidence.model_dump(mode="json")}


class Extractor:
    def __init__(self, storage: Storage, model: ModelClient, background_ready, io, embed, observer):
        self.storage = storage
        self.model = model
        self.background_ready = background_ready
        self.io = io
        self.embed = embed
        self.observer = observer
        graph = StateGraph(ExtractionState)
        graph.add_node(
            "extract",
            observer.node(
                "memory.extract",
                self.extract,
                lambda r: {"extraction": r["extraction"], "model_info": r["model_info"]},
            ),
        )
        graph.add_node(
            "verify",
            observer.node(
                "memory.verify",
                self.verify,
                lambda r: {
                    "verification": r["verification"],
                    "revision": r["revision"],
                    "model_info": r["model_info"],
                },
            ),
        )
        graph.add_node("commit", observer.node("memory.commit", self.commit, lambda r: r))
        graph.add_edge(START, "extract")
        graph.add_edge("extract", "verify")
        graph.add_edge("verify", "commit")
        graph.add_edge("commit", END)
        self.graph = graph.compile()

    async def call(self, role, instruction, inputs, schema, *, validate=None):
        await self.background_ready()
        request = ModelRequest(
            role=role,
            system_instruction=MEMORY_SYSTEM,
            task_instruction=instruction,
            input_data=inputs,
            output_schema=schema.model_json_schema(),
            max_output_tokens=4096,
            timeout_seconds=30,
        )
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        async with asyncio.timeout(request.timeout_seconds):
            for attempt in range(2):
                payload = None
                try:
                    await self.background_ready()
                    response = await self.model.generate(
                        replace(
                            request,
                            timeout_seconds=max(0, deadline - asyncio.get_running_loop().time()),
                        )
                    )
                    payload = response.payload
                    result = schema.model_validate(payload)
                    if validate is not None:
                        result = await validate(result)
                    return result, response.response_metadata.get("model")
                except ModelCallError as error:
                    if error.code != "MODEL_RESPONSE_INVALID" or attempt:
                        raise
                    payload = error.raw_response
                    feedback = {"code": error.code}
                    if error.details.get("json_syntax"):
                        feedback["json_syntax"] = error.details["json_syntax"]
                except ValidationError as error:
                    if attempt:
                        raise
                    feedback = [
                        {"path": list(e["loc"]), "type": e["type"]}
                        for e in error.errors(include_input=False, include_url=False)
                    ]
                except ValueError as error:
                    if payload is None or attempt:
                        raise
                    # validate callbacks emit contract codes, never provider exception text.
                    feedback = getattr(error, "feedback", {"code": str(error)})
                self.observer.emit(
                    "memory.model_repair",
                    data={"model_role": role, "validation_error": feedback, "next_attempt": 2},
                )
                request = replace(
                    request,
                    task_instruction=instruction + REPAIR,
                    input_data={
                        **inputs,
                        "previous_response": payload,
                        "validation_error": feedback,
                    },
                )

    async def extract(self, state):
        event_id = state["event_id"]
        event = await self.io(self.storage.load_event, event_id)
        rows = await self.io(
            self.storage.event_rows,
            request_id=event.request_id,
            through_event_id=event_id,
            limit=12,
        )
        allowed = {}
        for row in rows:
            old = await self.io(self.storage.load_event, row["event_id"])
            allowed[old.event_id] = old
        # Old memory in a goal is never submitted to the extractor as new evidence.
        inputs = []
        for old in allowed.values():
            data = old.model_dump(mode="json")
            if old.event_type == "goal_created":
                data["payload"] = {k: v for k, v in old.payload.items() if k != "context"}
            data["payload"] = bounded_data(data["payload"])
            inputs.append(data)
        job = await self.io(self.storage.job, event_id)
        import json

        info = json.loads(job["model_info"] or "{}")

        async def checked_extraction(extraction):
            ids = [fact.candidate_id for fact in extraction.facts]
            if len(ids) != len(set(ids)):
                raise ValueError("DUPLICATE_CANDIDATE_ID")
            eligible = []
            for candidate in (*extraction.facts, *extraction.summaries):
                # Old fact echoes cannot be new evidence; discard them before
                # validating quotes that will never be used for this event.
                if candidate in extraction.facts and not any(
                    e.event_id == event_id for e in candidate.evidence
                ):
                    continue
                sources = []
                for evidence in candidate.evidence:
                    try:
                        sources.append(await self.io(self.storage.source, evidence, allowed))
                    except ValueError as error:
                        raise EvidenceValidationError(str(error), evidence) from error
                if candidate in extraction.facts and direct_fact_evidence(event, sources):
                    eligible.append(candidate)
            return extraction.model_copy(update={"facts": eligible})

        extraction = None
        if job["extraction"]:
            try:
                extraction = await checked_extraction(
                    Extraction.model_validate_json(job["extraction"])
                )
            except ValueError:
                # Older/invalid cached output is not evidence and must be regenerated.
                await self.io(self.storage.update_job, event_id, extraction=None, verification=None)
        if extraction is None:
            extraction, name = await self.call(
                "memory_extract",
                EXTRACT,
                {"new_event_id": event_id, "events": inputs},
                Extraction,
                validate=checked_extraction,
            )
            info["extractor"] = name
        # Only validated output is cached. Retry cannot be poisoned by an invalid source/status.
        await self.io(
            self.storage.update_job,
            event_id,
            extraction=extraction.model_dump_json(),
            model_info=encode(info),
            derived="extracting",
        )
        return {"extraction": extraction, "allowed_events": allowed, "model_info": info}

    async def verify(self, state):
        revision, all_memories, vectors = await self.io(self.storage.snapshot)
        facts = state["extraction"].facts
        exact = [
            m
            for m in all_memories.values()
            if m.layer == "m1"
            and any(
                m.subject == f.subject and m.scope == f.scope and m.fact_key == f.fact_key
                for f in facts
            )
        ]
        query = " ".join(f.text for f in facts)
        keywords = await self.io(self.storage.keyword_ranks, query)
        expanded = [
            all_memories[mid]
            for mid in keywords.get("m1", [])
            if mid in all_memories
            and any(
                all_memories[mid].subject == f.subject and all_memories[mid].scope == f.scope
                for f in facts
            )
        ]
        existing = {m.memory_id: m for m in (*exact, *expanded[:30])}
        info = dict(state["model_info"])
        if facts and vectors:
            try:
                embeddings, tag = await self.embed([query], background=True)
                similar = await self.io(
                    similar_facts, all_memories, vectors, embeddings[0], tag, facts
                )
                existing.update({m.memory_id: m for m in similar})
            except Exception:
                info["matching"] = "bm25_and_exact_keys"
        inputs = {
            "candidates": [c.model_dump(mode="json") for c in facts],
            "events": [
                bounded_data(e.model_dump(mode="json")) for e in state["allowed_events"].values()
            ],
            "existing": [m.context() for m in existing.values()],
        }

        async def checked_verification(verification):
            if sorted(d.candidate_id for d in verification.decisions) != sorted(
                f.candidate_id for f in facts
            ):
                raise ValueError("INCOMPLETE_VERIFICATION")
            self.build_changes(
                {**state, "verification": verification, "existing": existing, "model_info": info}
            )
            return verification

        if facts:
            verification, name = await self.call(
                "memory_verify", VERIFY, inputs, Verification, validate=checked_verification
            )
            info["verifier"] = name
        else:
            verification = Verification(decisions=[])
        await self.io(
            self.storage.update_job,
            state["event_id"],
            verification=verification.model_dump_json(),
            model_info=encode(info),
        )
        return {
            "verification": verification,
            "existing": existing,
            "revision": revision,
            "model_info": info,
        }

    async def commit(self, state):
        changed = await self.io(self.build_changes, state)
        await self.io(self.storage.commit_memories, state["event_id"], changed, state["revision"])
        return {"committed_memory_ids": [m.memory_id for m in changed]}

    def build_changes(self, state):
        event = state["allowed_events"][state["event_id"]]
        facts = {c.candidate_id: c for c in state["extraction"].facts}
        existing = state["existing"]
        changes, accepted = {}, []
        now = utcnow()
        for decision in state["verification"].decisions:
            candidate = facts[decision.candidate_id]
            if decision.verification != "supported" or decision.operation == "ignore":
                continue
            if candidate.scope.kind == "project" and candidate.scope.project_id != event.project_id:
                raise ValueError("UNVERIFIED_PROJECT_SCOPE")
            sources = [self.storage.source(e, state["allowed_events"]) for e in candidate.evidence]
            # Assistant echoes and goals cannot create durable personal assertions.
            if not direct_fact_evidence(event, sources):
                continue
            if len(decision.matched_ids) != len(set(decision.matched_ids)):
                raise ValueError("DUPLICATE_MATCHED_ID")
            matched = []
            for mid in decision.matched_ids:
                old = changes.get(mid, existing.get(mid))
                if old is None or old.subject != candidate.subject or old.scope != candidate.scope:
                    raise ValueError("INVALID_FACT_MATCH")
                matched.append(old)
            # A new conflict joins every member; otherwise one overwritten group
            # ID could disconnect earlier contradictory evidence.
            groups = {old.conflict_group_id for old in matched if old.conflict_group_id}
            if groups:
                for old in existing.values():
                    if old.conflict_group_id in groups and old.memory_id not in {
                        m.memory_id for m in matched
                    }:
                        matched.append(old)
            matched_ids = [old.memory_id for old in matched]
            if decision.operation != "new" and not matched:
                raise ValueError("MISSING_FACT_MATCH")
            if decision.operation == "new" and matched:
                raise ValueError("NEW_FACT_CANNOT_REUSE_MATCHED_SLOT")
            if decision.operation == "new" and any(
                old.fact_key == candidate.fact_key
                and old.subject == candidate.subject
                and old.scope == candidate.scope
                for old in existing.values()
            ):
                raise ValueError("EXISTING_FACT_NOT_MATCHED")
            if decision.operation == "coexist" and any(
                old.fact_key != matched[0].fact_key
                or old.state != "active"
                or digest(old.value) == digest(candidate.value)
                for old in matched
            ):
                raise ValueError("INVALID_COEXISTING_FACT")
            if decision.operation in {"replace", "correct"} and any(
                max(s.occurred_at for s in old.sources) > event.occurred_at for old in matched
            ):
                # Delayed work must be reconsidered as historical evidence, not overwrite.
                raise ValueError("STALE_FACT_CHANGE")
            if decision.operation == "reinforce":
                for old in matched:
                    value = (
                        decision.canonical_value
                        if decision.canonical_value is not None
                        else candidate.value
                    )
                    if digest(old.value) != digest(value):
                        raise ValueError("REINFORCE_DIFFERENT_VALUE")
                    refs = {(s.event_id, s.pointer): s for s in (*old.sources, *sources)}
                    update = {"sources": list(refs.values())}
                    for field in ("valid_from", "valid_to"):
                        value = getattr(candidate, field)
                        if getattr(old, field) is None and value and value.origin == "explicit":
                            update[field] = value
                    changes[old.memory_id] = old.model_copy(update=update)
                    accepted.append(old.memory_id)
                continue
            mid = stable_id(event.event_id, "m1", candidate.candidate_id)
            group = (
                stable_id(event.event_id, "conflict", candidate.candidate_id)
                if decision.operation == "conflict"
                else None
            )
            memory = StoredMemory(
                memory_id=mid,
                layer="m1",
                text=candidate.text,
                subject=candidate.subject,
                fact_key=matched[0].fact_key if matched else candidate.fact_key,
                value=candidate.value,
                scope=candidate.scope,
                sources=sources,
                state="conflicted" if group else "active",
                verification_reason=decision.reason,
                assertion_type="observed" if event.event_type == "task_result" else "explicit",
                recorded_at=min(s.occurred_at for s in sources),
                created_at=now,
                updated_at=now,
                valid_from=candidate.valid_from,
                valid_to=candidate.valid_to,
                supersedes=matched_ids if decision.operation == "replace" else [],
                corrects=matched_ids if decision.operation == "correct" else [],
                conflict_group_id=group,
                request_id=event.request_id,
                extractor_model=state["model_info"].get("extractor"),
                verifier_model=state["model_info"].get("verifier"),
                prompt_version=PROMPT_VERSION,
            )
            changes[mid] = memory
            for old in matched:
                update = {}
                if decision.operation == "replace":
                    update = {"state": "superseded", "valid_to": candidate.valid_from}
                elif decision.operation == "correct":
                    update = {"state": "corrected"}
                elif group:
                    update = {"state": "conflicted", "conflict_group_id": group}
                if update:
                    changes[old.memory_id] = old.model_copy(update=update)
            accepted.extend([mid, *matched_ids])
        for index, summary in enumerate(state["extraction"].summaries):
            sources = [self.storage.source(e, state["allowed_events"]) for e in summary.evidence]
            if not any(s.event_id == event.event_id for s in sources):
                continue
            mid = stable_id(event.event_id, "m2", str(index))
            changes[mid] = StoredMemory(
                memory_id=mid,
                layer="m2",
                text=summary.text,
                sources=sources,
                scope=Scope(kind="project", project_id=event.project_id)
                if event.project_id
                else Scope(),
                recorded_at=event.occurred_at,
                created_at=now,
                updated_at=now,
                request_id=event.request_id,
                event_kind=summary.event_kind,
                actions=summary.actions,
                facts=summary.facts,
                assertion_type=(
                    "explicit"
                    if event.event_type == "user_message"
                    else "observed"
                    if event.event_type == "task_result"
                    else "inferred"
                ),
                outcome=(
                    {
                        k: event.payload[k]
                        for k in ("run_id", "execution_status", "output_complete", "diagnostics")
                        if k in event.payload
                    }
                    if event.event_type == "task_result"
                    else summary.outcome
                ),
                artifact_refs=(
                    event.payload.get("artifacts", [])
                    if event.event_type == "task_result"
                    else summary.artifact_refs
                ),
                related_memory_ids=list(dict.fromkeys(accepted)),
                extractor_model=state["model_info"].get("extractor"),
                prompt_version=PROMPT_VERSION,
            )
        for decision in state["verification"].decisions:
            if decision.verification != "uncertain":
                continue
            candidate = facts[decision.candidate_id]
            sources = [self.storage.source(e, state["allowed_events"]) for e in candidate.evidence]
            if not any(source.event_id == event.event_id for source in sources):
                continue
            mid = stable_id(event.event_id, "m2-uncertain", candidate.candidate_id)
            changes[mid] = StoredMemory(
                memory_id=mid,
                layer="m2",
                text="尚未核验的候选陈述：" + candidate.text,
                sources=sources,
                verification_state="uncertain",
                verification_reason=decision.reason,
                scope=Scope(kind="project", project_id=event.project_id)
                if event.project_id
                else Scope(),
                assertion_type="inferred",
                recorded_at=event.occurred_at,
                created_at=now,
                updated_at=now,
                request_id=event.request_id,
                event_kind="unverified_candidate",
                extractor_model=state["model_info"].get("extractor"),
                verifier_model=state["model_info"].get("verifier"),
                prompt_version=PROMPT_VERSION,
            )
        return list(changes.values())


def similar_facts(memories, vectors, embedding, tag, facts):
    import numpy as np

    vector = np.asarray(embedding, dtype=np.float32)
    if vector.ndim != 1 or not np.isfinite(vector).all() or not np.linalg.norm(vector):
        raise ValueError("INVALID_MATCH_EMBEDDING")
    vector = vector / np.linalg.norm(vector)
    scores = []
    for mid, memory in memories.items():
        row = vectors.get(mid)
        if memory.layer != "m1" or not any(
            memory.subject == f.subject and memory.scope == f.scope for f in facts
        ):
            continue
        if (
            row
            and row["model_tag"] == tag
            and row["dimension"] == vector.size
            and row["text_hash"] == digest(memory.text)
        ):
            value = np.frombuffer(row["vector"], dtype=np.float32)
            scores.append((float(value @ vector), mid))
    return [memories[mid] for _, mid in sorted(scores, reverse=True)[:30]]


def bounded_data(value, budget=12000):
    """Limit model input while keeping complete originals for source validation."""
    remaining = [budget]

    def visit(item):
        if isinstance(item, str):
            text = item[: max(0, remaining[0])]
            remaining[0] -= len(text)
            return text
        if isinstance(item, dict):
            return {k: visit(v) for k, v in item.items() if remaining[0] > 0 and k != "memory"}
        if isinstance(item, list):
            return [visit(v) for v in item[:40] if remaining[0] > 0]
        return item

    return visit(value)
