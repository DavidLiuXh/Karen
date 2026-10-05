"""Current-input analysis, hybrid retrieval, relation bundles and bounded recall."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import TypedDict

import numpy as np
from dynamic_graph.models.client import ModelRequest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .contracts import (
    DetailQuery,
    History,
    MemoryHit,
    QueryAnalysis,
    Ranking,
    RecallQuery,
    RecallResult,
    TimeRange,
)
from .prompts import MEMORY_SYSTEM, QUERY, RERANK
from .storage import digest, encode, terms

FALLBACK_STOP_WORDS = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "什么",
        "需要",
        "可以",
        "请问",
        "一下",
        "帮我",
        "现在",
        "今天",
        "昨天",
        "之前",
        "如何",
        "是不是",
        "the",
        "and",
        "with",
        "that",
        "this",
        "what",
        "please",
        "can",
        "you",
        "your",
    }
)


class RecallState(TypedDict, total=False):
    query: RecallQuery
    deadline: float
    analysis: QueryAnalysis
    degraded: list[str]
    revision: int
    memories: dict
    primary: list[str]
    bundles: dict
    scores: dict
    index_coverage: dict
    anchors: list
    ranking: Ranking
    result: RecallResult


def time_bounds(value):
    """An imprecise date denotes an interval, never a fabricated exact instant."""
    return value.bounds() if value is not None else None


def evidence_only(memory, analysis, now=None):
    if memory.layer != "m1":
        return False
    if analysis.time_mode == "unspecified":
        return True
    if memory.verification_state != "supported" or memory.state in {"corrected", "conflicted"}:
        return True
    if analysis.time_mode == "effective_at":
        if any(
            value and value.origin == "inferred" for value in (memory.valid_from, memory.valid_to)
        ):
            return True
        start, end = time_bounds(memory.valid_from), time_bounds(memory.valid_to)
        return (
            start is None
            or analysis.at < start[1]
            or (end is not None and analysis.at >= end[0])
            or (memory.state == "superseded" and end is None)
        )
    if analysis.time_mode == "current" and now is not None:
        start, end = time_bounds(memory.valid_from), time_bounds(memory.valid_to)
        if start and now < start[0] or end and now >= end[0]:
            return True
    return memory.state != "active"


def hybrid(memories, vectors, keyword, query_vector, tag, query):
    """Cosine top-100 and BM25 top-100 per layer, fused by reciprocal rank."""
    eligible = {
        mid: m
        for mid, m in memories.items()
        if m.verification_state != "rejected"
        and (m.scope.kind == "global" or m.scope.project_id == query.project_id)
        and not (m.layer == "m2" and m.request_id == query.request_id)
        and not all(s.event_id in query.exclude_event_ids for s in m.sources)
    }
    cosine = {}
    if query_vector is not None:
        vector = np.asarray(query_vector, dtype=np.float32)
        if (
            vector.ndim != 1
            or not vector.size
            or not np.isfinite(vector).all()
            or not np.linalg.norm(vector)
        ):
            raise ValueError("INVALID_QUERY_EMBEDDING")
        vector = vector / np.linalg.norm(vector)
        for mid, row in vectors.items():
            if mid not in eligible or row["model_tag"] != tag or row["dimension"] != vector.size:
                continue
            if row["text_hash"] != digest(eligible[mid].text):
                continue
            stored = np.frombuffer(row["vector"], dtype=np.float32)
            if stored.size == vector.size and np.isfinite(stored).all():
                cosine[mid] = float(stored @ vector)
    scores, roots = {}, []
    for layer in ("m1", "m2"):
        vector_order = sorted(
            (mid for mid in cosine if eligible[mid].layer == layer),
            key=lambda mid: (-cosine[mid], mid),
        )[:100]
        keyword_order = [mid for mid in keyword.get(layer, []) if mid in eligible][:100]
        fused = {}
        for kind, order in (("vector", vector_order), ("bm25", keyword_order)):
            for rank, mid in enumerate(order, 1):
                fused[mid] = fused.get(mid, 0) + 1 / (60 + rank)
                scores.setdefault(mid, {})[kind] = rank
        order = sorted(fused, key=lambda mid: (-fused[mid], mid))
        for rank, mid in enumerate(order, 1):
            scores[mid].update(fusion_rank=rank, vector_score=cosine.get(mid))
        roots.extend(order[:20])
    return eligible, roots, scores


def relation_bundle(mid, memories, reverse, *, timeline=False):
    """Resolve current versions without injecting an unbounded chain of past moves."""
    found, todo = set(), [mid]
    while todo:
        current = todo.pop()
        if current in found or current not in memories:
            continue
        found.add(current)
        m = memories[current]
        todo.extend(m.related_memory_ids + m.supersedes + m.corrects + reverse.get(current, []))
        if m.conflict_group_id:
            todo.extend(
                other.memory_id
                for other in memories.values()
                if other.conflict_group_id == m.conflict_group_id
            )
    if timeline:
        return sorted(found)
    root = memories[mid]
    required = {mid, *root.related_memory_ids}
    required.update(
        other
        for other in found
        if memories[other].layer == "m1" and memories[other].state in {"active", "conflicted"}
    )
    return sorted(found & required)


class Retriever:
    def __init__(self, service):
        self.service = service
        self.storage = service.storage
        graph = StateGraph(RecallState)
        summaries = {
            "analyze": lambda r: r,
            "retrieve": lambda r: {
                "revision": r["revision"],
                "fusion_order": r["primary"],
                "scores": r["scores"],
                "bundles": r["bundles"],
                "candidates": {
                    mid: r["memories"][mid]
                    for mid in set(m for group in r["bundles"].values() for m in group)
                },
                "index_coverage": r["index_coverage"],
                "degraded": r["degraded"],
                "history_candidate_ids": [e.event_id for e in r["anchors"]],
            },
            "rank": lambda r: r,
            "assemble": lambda r: r,
        }
        for name in ("analyze", "retrieve", "rank", "assemble"):
            graph.add_node(
                name,
                service.observer.node(
                    "memory.recall." + name, getattr(self, name), summaries[name]
                ),
            )
        graph.add_edge(START, "analyze")
        graph.add_edge("analyze", "retrieve")
        graph.add_edge("retrieve", "rank")
        graph.add_edge("rank", "assemble")
        graph.add_edge("assemble", END)
        self.graph = graph.compile()

    def report_degradation(self, code, error):
        """Record handled failures without copying response values or exception messages."""
        details = {
            "code": code,
            "error_type": type(error).__name__,
            "error_code": getattr(error, "code", type(error).__name__),
        }
        if isinstance(error, ValidationError):
            details["error_code"] = "MODEL_RESPONSE_VALIDATION_FAILED"
            details["validation_errors"] = []
            for item in error.errors(include_input=False, include_url=False):
                issue = {"type": item["type"], "location": list(item["loc"])}
                if item["type"] == "time_reference_required":
                    mode = item["ctx"]["time_mode"]
                    issue.update(
                        field="at",
                        time_mode=mode,
                        reason=f"{mode} 需要显式、带时区的 at；time_range 不能替代 at。",
                    )
                elif item["type"] == "at_timezone_required":
                    issue.update(field="at", reason="at 缺少时区偏移。")
                details["validation_errors"].append(issue)
        elif isinstance(error, TimeoutError):
            details["error_code"] = "RECALL_TIMEOUT"
        elif (
            isinstance(error, ValueError)
            and error.args
            and isinstance(error.args[0], str)
            and error.args[0]
            in {
                "RERANK_INPUT_TOO_LARGE",
                "INVALID_RERANK_IDS",
                "DUPLICATE_HISTORY_EVENT",
                "INVALID_HISTORY_EVENT",
                "INVALID_HISTORY_TASK",
                "EMPTY_SELECTED_HISTORY",
                "UNEXPECTED_HISTORY",
                "UNRELATED_HISTORY",
            }
        ):
            details["error_code"] = error.args[0]
        self.service.observer.emit("memory.degraded", status="degraded", data=details)

    async def recall(self, query):
        state = {"query": query, "deadline": time.monotonic() + 20, "degraded": []}
        try:
            async with asyncio.timeout(20):
                async for update in self.graph.astream(state, stream_mode="updates"):
                    for values in update.values():
                        state.update(values)
            return state["result"]
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.report_degradation("MEMORY_RECALL_UNAVAILABLE", error)
            analysis = state.get("analysis")
            dependency = analysis.dialogue_dependency if analysis else "none"
            return RecallResult(
                status="unavailable",
                history=History(
                    status="unavailable" if analysis and analysis.uses_external_history else "none",
                    reason="本轮记忆读取未能完成。",
                ),
                degradations=["MEMORY_RECALL_UNAVAILABLE"],
                coverage={
                    "complete": False,
                    "dialogue_dependency": dependency,
                    "requires_history": dependency == "needed",
                },
            )

    async def call(self, state, role, instruction, inputs, schema):
        remaining = min(state["deadline"] - time.monotonic() - 1.5, 8)
        if remaining <= 0:
            raise TimeoutError("RECALL_DEADLINE")
        request = ModelRequest(
            role=role,
            system_instruction=MEMORY_SYSTEM,
            task_instruction=instruction,
            input_data=inputs,
            output_schema=schema.model_json_schema(),
            max_output_tokens=4096,
            timeout_seconds=remaining,
        )
        async with asyncio.timeout(remaining):
            response = await self.service.model.generate(request)
        return schema.model_validate(response.payload)

    async def analyze(self, state):
        query = state["query"]
        degraded = list(state["degraded"])
        try:
            analysis = await self.call(
                state,
                "memory_query",
                QUERY,
                {
                    "text": query.text,
                    "timezone": query.timezone,
                    "current_time_utc": query.current_time_utc.isoformat(),
                    "time_constraint": query.time_constraint.model_dump(mode="json")
                    if query.time_constraint
                    else None,
                    "project_id": query.project_id,
                    "current_task_messages": query.current_task_messages,
                },
                QueryAnalysis,
            )
            if analysis.dialogue_dependency == "current_task" and not query.current_task_messages:
                raise ValueError("CURRENT_TASK_CONTEXT_MISSING")
        except Exception as error:
            self.report_degradation("QUERY_ANALYSIS_FAILED", error)
            analysis = QueryAnalysis(search_text=query.text, time_mode="unspecified")
            degraded.append("QUERY_ANALYSIS_FAILED")
        if query.time_constraint:
            analysis = analysis.model_copy(update={"time_range": query.time_constraint})
        return {"analysis": analysis, "degraded": degraded}

    async def retrieve(self, state):
        query, analysis = state["query"], state["analysis"]
        degraded = list(state["degraded"])
        vector, tag = None, None
        remaining = min(state["deadline"] - time.monotonic() - 1.5, 8)
        try:
            async with asyncio.timeout(max(0, remaining)):
                values, tag = await self.service.embed([analysis.search_text])
                vector = values[0]
        except Exception:
            degraded.append("VECTOR_RECALL_UNAVAILABLE")
        known_at = analysis.at if analysis.time_mode == "known_at" else None
        revision, memories, vectors, keyword, coverage = await self.service._io(
            self.storage.retrieval_snapshot, analysis.search_text, known_at
        )
        if not coverage["bm25_available"]:
            degraded.append("BM25_RECALL_UNAVAILABLE")
            if vector is None:
                raise RuntimeError("BOTH_RECALL_BRANCHES_UNAVAILABLE")
        if vector is not None:
            dimensions = {row["dimension"] for row in vectors.values() if row["model_tag"] == tag}
            if dimensions and dimensions != {len(vector)}:
                await self.service._io(self.storage.invalidate_vectors, tag, force=True)
                degraded.append("EMBEDDING_DIMENSION_CHANGED")
                vectors = {}
        try:
            eligible, roots, scores = await self.service._io(
                hybrid, memories, vectors, keyword, vector, tag, query
            )
        except ValueError:
            if not coverage["bm25_available"]:
                raise RuntimeError("BOTH_RECALL_BRANCHES_UNAVAILABLE") from None
            eligible, roots, scores = await self.service._io(
                hybrid, memories, vectors, keyword, None, None, query
            )
            degraded.append("INVALID_QUERY_EMBEDDING")
        if analysis.kind == "facts" and analysis.time_mode == "current":
            # Personal facts are answered from m1; m2 is a fallback when no m1 was retrieved.
            # Keep version/conflict bundles so preference changes remain visible.
            fact_roots = [mid for mid in roots if eligible[mid].layer == "m1"]
            if fact_roots:
                roots = fact_roots
        if vector is not None and any(mid not in vectors for mid in eligible):
            degraded.append("VECTOR_INDEX_INCOMPLETE")
        reverse = {}
        for mid, memory in eligible.items():
            # m2 links to m1; adding every other event to an m1 would flood context.
            for old in memory.supersedes + memory.corrects:
                reverse.setdefault(old, []).append(mid)
        bundles, primary, unique = {}, [], set()
        for mid in roots:
            bundle = relation_bundle(
                mid, eligible, reverse, timeline=analysis.time_mode == "timeline"
            )
            extra = set(bundle) - unique
            if len(unique | extra) > 80:
                degraded.append("CANDIDATE_BUDGET_LIMIT")
                continue
            primary.append(mid)
            bundles[mid] = bundle
            unique.update(extra)
        anchors = []
        if analysis.uses_external_history:
            anchors = await self.service.recent(query, analysis)
            # Retrieved older tasks can be located even outside the recent window.
            known_ids = {e.event_id for e in anchors}
            for mid in primary:
                for source in eligible[mid].sources:
                    if len(anchors) >= 24:
                        break
                    if (
                        source.event_id not in known_ids
                        and source.event_id not in query.exclude_event_ids
                    ):
                        try:
                            event = await self.service.load_event(source.event_id)
                            known_ids.add(source.event_id)
                            if event.request_id != query.request_id:
                                anchors.append(event)
                        except Exception:
                            degraded.append("HISTORY_SOURCE_UNAVAILABLE")
        return {
            "revision": revision,
            "memories": eligible,
            "primary": primary,
            "bundles": bundles,
            "scores": scores,
            "index_coverage": coverage,
            "anchors": anchors,
            "degraded": list(dict.fromkeys(degraded)),
        }

    async def rank(self, state):
        primary, analysis = state["primary"], state["analysis"]
        degraded = list(state["degraded"])
        if not primary and not analysis.uses_external_history:
            return {"ranking": Ranking(ranking=[])}
        records = {mid for bundle in state["bundles"].values() for mid in bundle}
        anchors = [
            {
                "event_id": e.event_id,
                "request_id": e.request_id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
                "payload_preview": encode(e.payload)
                .encode()[:600]
                .decode("utf-8", errors="ignore"),
            }
            for e in state["anchors"]
        ]
        inputs = {
            "query": state["query"].text,
            "current_request_id": state["query"].request_id,
            "current_task_messages": state["query"].current_task_messages,
            "analysis": analysis.model_dump(mode="json"),
            "primary_ids": list(primary),
            "memories": [state["memories"][mid].ranking_context() for mid in sorted(records)],
            "history_candidates": anchors,
        }
        # Bound the whole request, including raw locating clues, not just memories.
        primary = list(primary)
        while len(encode(inputs).encode()) > 24 * 1024 and primary:
            primary.pop()
            records = {mid for root in primary for mid in state["bundles"][root]}
            inputs["primary_ids"] = primary
            inputs["memories"] = [
                state["memories"][mid].ranking_context() for mid in sorted(records)
            ]
            degraded.append("RERANK_INPUT_BUDGET_LIMIT")
        while len(encode(inputs).encode()) > 24 * 1024 and inputs["history_candidates"]:
            inputs["history_candidates"].pop()
            degraded.append("HISTORY_CANDIDATE_BUDGET_LIMIT")
        allowed_event_ids = {item["event_id"] for item in inputs["history_candidates"]}
        try:
            if len(encode(inputs).encode()) > 24 * 1024:
                raise ValueError("RERANK_INPUT_TOO_LARGE")
            ranking = await self.call(
                state,
                "memory_rerank",
                RERANK,
                inputs,
                Ranking,
            )
            if sorted(r.memory_id for r in ranking.ranking) != sorted(primary):
                raise ValueError("INVALID_RERANK_IDS")
            events = {e.event_id: e for e in state["anchors"] if e.event_id in allowed_event_ids}
            if len(set(ranking.selected_event_ids)) != len(ranking.selected_event_ids):
                raise ValueError("DUPLICATE_HISTORY_EVENT")
            if not set(ranking.selected_event_ids) <= events.keys():
                raise ValueError("INVALID_HISTORY_EVENT")
            selected_tasks = {events[eid].request_id for eid in ranking.selected_event_ids}
            if set(ranking.related_request_ids) != selected_tasks:
                raise ValueError("INVALID_HISTORY_TASK")
            if ranking.history_status == "selected" and not selected_tasks:
                raise ValueError("EMPTY_SELECTED_HISTORY")
            if ranking.history_status != "selected" and ranking.selected_event_ids:
                raise ValueError("UNEXPECTED_HISTORY")
            if not analysis.uses_external_history and ranking.history_status != "none":
                raise ValueError("UNRELATED_HISTORY")
        except Exception as error:
            self.report_degradation("RERANK_FAILED_FUSION_ORDER", error)
            # Preserve the existing fused order and label it; no invented precision.
            ranking = Ranking(
                ranking=[],
                history_status=(
                    "unavailable" if analysis.dialogue_dependency == "needed" else "none"
                ),
                history_reason="关联核验未完成，不能确定所指任务。",
            )
            degraded.append("RERANK_FAILED_FUSION_ORDER")
        return {"ranking": ranking, "degraded": degraded, "primary": primary}

    async def assemble(self, state):
        query, analysis, ranking = state["query"], state["analysis"], state["ranking"]
        memories, scores = state["memories"], state["scores"]
        degraded = list(state["degraded"])
        ranked = [
            (r.memory_id, r.relevance, index)
            for index, r in enumerate(ranking.ranking, 1)
            if r.relevance != "irrelevant"
        ]
        if "RERANK_FAILED_FUSION_ORDER" in degraded:
            keywords = set(analysis.needed_fact_keys)
            # Fallback needs positive lexical/slot evidence, not a cosine cutoff.
            query_words = {
                word
                for word in terms(analysis.search_text)
                if len(word) >= 2 and word not in FALLBACK_STOP_WORDS
            }
            entity_words = {word for entity in analysis.entities for word in terms(entity)}
            ranked = [
                (mid, "unverified", None)
                for mid in state["primary"]
                if memories[mid].fact_key in keywords
                or (
                    any(entity and entity in memories[mid].text for entity in analysis.entities)
                    and bool((query_words & set(terms(memories[mid].text))) - entity_words)
                )
                or len((query_words & set(terms(memories[mid].text))) - entity_words) >= 2
            ]
        m1, m2, included, used = [], [], set(), 0
        memory_groups = []
        root_counts = {"m1": 0, "m2": 0}
        for mid, relevance, rank in ranked:
            layer = memories[mid].layer
            root_limit = (
                20 if analysis.kind == "facts" and layer == "m1" else (5 if layer == "m1" else 8)
            )
            if root_counts[layer] >= root_limit:
                degraded.append("CONTEXT_BUDGET_LIMIT")
                continue
            extra = [related for related in state["bundles"][mid] if related not in included]
            hits = []
            for related in extra:
                score = scores.get(related, scores[mid])
                hits.append(
                    MemoryHit(
                        memory=memories[related],
                        relevance=relevance,
                        fusion_rank=score["fusion_rank"],
                        rerank_rank=rank,
                        vector_score=score.get("vector_score"),
                        bm25_rank=score.get("bm25"),
                        evidence_only=evidence_only(
                            memories[related], analysis, query.current_time_utc
                        ),
                    )
                )
            size = len(
                encode(
                    [{**h.model_dump(mode="json"), "memory": h.memory.context()} for h in hits]
                ).encode()
            )
            memory_budget = (6 if analysis.kind == "detail" else 10) * 1024
            if used + size > memory_budget:
                degraded.append("CONTEXT_BUDGET_LIMIT")
                continue
            for hit in hits:
                (m1 if hit.memory.layer == "m1" else m2).append(hit)
            if hits:
                memory_groups.append({hit.memory.memory_id for hit in hits})
            included.update(extra)
            used += size
            root_counts[layer] += 1
        if "RERANK_FAILED_FUSION_ORDER" in degraded:
            m1.sort(key=lambda hit: hit.fusion_rank)
            m2.sort(key=lambda hit: hit.fusion_rank)
        history_messages = []
        before_history = used
        if ranking.history_status == "selected":
            events = {e.event_id: e for e in state["anchors"]}
            selected = sorted(
                (events[eid] for eid in ranking.selected_event_ids), key=lambda e: e.occurred_at
            )
            for event in selected:
                text = event.payload.get("content") or encode(
                    {k: v for k, v in event.payload.items() if k != "context"}
                )
                message = {
                    "event_id": event.event_id,
                    "request_id": event.request_id,
                    "content": text[:2400],
                    "truncated": len(text) > 2400,
                    "occurred_at": event.occurred_at.isoformat(),
                }
                source = await self.service.source_ref(
                    event,
                    "/payload/content"
                    if isinstance(event.payload.get("content"), str)
                    else "/payload",
                    text[:1000] if isinstance(event.payload.get("content"), str) else "",
                )
                message["role"] = source.source_role
                message["source"] = source.model_dump(mode="json")
                size = len(encode(message).encode())
                if used + size > 11 * 1024:
                    degraded.append("HISTORY_BUDGET_LIMIT")
                    break
                history_messages.append(message)
                used += size
        history_status = ranking.history_status
        all_history_events = len(history_messages) == len(ranking.selected_event_ids)
        history_complete = all_history_events and not any(
            message["truncated"] for message in history_messages
        )
        if history_status == "selected" and not all_history_events:
            history_messages = []
            used = before_history
        if history_status == "selected" and not history_complete:
            degraded.append("HISTORY_BUDGET_LIMIT")
        history = History(
            status=history_status,
            reason=ranking.history_reason,
            messages=history_messages,
            complete=history_complete,
        )
        details, detail_status = [], None
        detail_priority = []
        if analysis.kind == "detail" and time.monotonic() < state["deadline"]:
            # Summary bodies and source locators have different budgets. A relevant
            # summary that does not fit must still lead us to its original evidence.
            source_memories = [
                memories[related]
                for mid, _, _ in ranked
                if memories[mid].layer == "m2"
                for related in state["bundles"][mid]
            ]
            source_memories.extend(hit.memory for hit in m1)
            refs, seen_sources = [], set()
            # Give each ranked record a source before taking additional sources
            # from a dense summary. A single record must not consume every locator.
            for source_group in zip_longest(*(memory.sources for memory in source_memories)):
                for source in source_group:
                    if source is None:
                        continue
                    identity = (source.event_id, source.pointer)
                    if identity not in seen_sources:
                        seen_sources.add(identity)
                        refs.append(source)
            if len(refs) > 12:
                degraded.append("DETAIL_SOURCE_BUDGET_LIMIT")
            scope_task = (
                ranking.related_request_ids[0] if len(ranking.related_request_ids) == 1 else None
            )
            detail_range = analysis.time_range
            if analysis.time_mode == "known_at":
                end = analysis.at + timedelta(microseconds=1)
                detail_range = TimeRange(
                    start=detail_range.start
                    if detail_range
                    else datetime.min.replace(tzinfo=timezone.utc),
                    end=min(detail_range.end, end) if detail_range else end,
                )
            result = await self.service.search_details(
                DetailQuery(
                    text=analysis.search_text,
                    sources=refs[:12],
                    request_id=scope_task,
                    time_range=detail_range,
                    conversation_id=query.conversation_id
                    if not refs and not scope_task and not analysis.time_range
                    else None,
                )
            )
            detail_status = result.status
            detail_hits = list(result.hits)
            # Only successfully read anchors are already covered. Omitted or
            # unavailable locators can still be found by bounded task expansion.
            reference_ids = {hit.source.event_id for hit in result.hits}
            words = set(terms(analysis.search_text)) - FALLBACK_STOP_WORDS
            direct_details = sorted(
                result.hits,
                key=lambda hit: (
                    hit.source.source_role == "user",
                    len(words & set(terms(hit.text))),
                    hit.source.occurred_at,
                    hit.source.sequence or 0,
                ),
                reverse=True,
            )
            priority_details = []
            bridge_details = []
            # A summary can cite an earlier turn while its missing detail lives in
            # a later turn of that same task. Expand only positively selected tasks.
            scopes = list(
                dict.fromkeys(
                    [
                        *ranking.related_request_ids,
                        *(
                            memories[mid].request_id
                            for mid, relevance, _ in ranked
                            if memories[mid].layer == "m2"
                        ),
                    ]
                )
            )
            scopes = [scope for scope in scopes if scope != query.request_id]
            if len(scopes) > 3:
                degraded.append("DETAIL_SCOPE_BUDGET_LIMIT")
            for scope in scopes[:3]:
                if time.monotonic() >= state["deadline"]:
                    detail_status = "partial"
                    break
                expanded = await self.service.search_details(
                    DetailQuery(
                        # Replies can omit the entity and query vocabulary entirely.
                        # The verified task boundary supplies the search scope.
                        text="",
                        request_id=scope,
                        time_range=detail_range,
                    )
                )
                detail_hits.extend(expanded.hits)
                novel_user_hits = [
                    hit
                    for hit in expanded.hits
                    if hit.source.event_id not in reference_ids and hit.source.source_role == "user"
                ]
                if novel_user_hits:
                    novel_user_hits.sort(
                        key=lambda hit: (hit.source.occurred_at, hit.source.sequence or 0)
                    )
                    anchor = max(
                        range(len(novel_user_hits)),
                        key=lambda index: len(words & set(terms(novel_user_hits[index].text))),
                    )
                    # Follow-up replies often carry the requested value without
                    # repeating the subject. Keep adjacent user evidence together.
                    neighbors = novel_user_hits[max(0, anchor - 1) : anchor + 2]
                    priority_details.extend(neighbors)
                    first = neighbors[0].source.sequence
                    last = neighbors[-1].source.sequence
                    for hit in expanded.hits:
                        sequence = hit.source.sequence
                        if (
                            hit.source.source_role == "assistant"
                            and first is not None
                            and last is not None
                            and sequence is not None
                            and first < sequence < last
                        ):
                            preview = hit.text[:600]
                            bridge_details.append(
                                hit.model_copy(
                                    update={
                                        "text": preview,
                                        "source": hit.source.model_copy(update={"quote": preview}),
                                        "truncated": hit.truncated or preview != hit.text,
                                    }
                                )
                            )
                if expanded.status != "complete":
                    detail_status = expanded.status
            detail_hits.sort(
                key=lambda hit: (
                    hit.source.event_id not in reference_ids and hit.source.source_role == "user",
                    len(words & set(terms(hit.text))),
                ),
                reverse=True,
            )
            seen_details = set()
            for hit in [*direct_details, *priority_details, *bridge_details, *detail_hits]:
                identity = (hit.source.event_id, hit.source.pointer)
                if identity in seen_details:
                    continue
                seen_details.add(identity)
                size = len(encode(hit.context()).encode())
                if used + size > 11 * 1024:
                    degraded.append("DETAIL_BUDGET_LIMIT")
                    continue
                details.append(hit)
                detail_priority.append(identity)
                used += size
                if hit.truncated:
                    degraded.append("DETAIL_CONTEXT_TRUNCATED")
            details.sort(key=lambda hit: (hit.source.occurred_at, hit.source.sequence or 0))
            if detail_status != "complete":
                degraded.append("DETAIL_SEARCH_" + detail_status.upper())
        collection = None
        if analysis.kind == "collection":
            collection = await self.service._io(
                self.storage.collection, analysis.time_range, analysis.task_status
            )
            while collection["items"] and used + len(encode(collection).encode()) > 11 * 1024:
                collection["items"].pop()
                collection["returned_count"] = len(collection["items"])
                collection["display_complete"] = False
            if any(e[0].event_type == "task_result" for e in self.service._pending.values()):
                collection["complete"] = False
                collection["reason"] = "raw_writes_pending"
        coverage = {
            "complete": not degraded,
            "query_kind": analysis.kind,
            "dialogue_dependency": analysis.dialogue_dependency,
            "requires_history": analysis.dialogue_dependency == "needed",
            "detail_status": detail_status,
            "pending_events": len(self.service._pending),
            "visibility": "committed_snapshot_plus_selected_raw_events",
            "relation_mode": "timeline"
            if analysis.time_mode == "timeline"
            else "matched_and_current_versions",
            "index": state["index_coverage"],
        }
        result = RecallResult(
            status="degraded"
            if degraded
            else ("ok" if m1 or m2 or details or history_messages or collection else "empty"),
            m1=m1,
            m2=m2,
            details=details,
            history=history,
            related_request_ids=ranking.related_request_ids if history_status == "selected" else [],
            snapshot_revision=state["revision"],
            query_time_basis={
                "mode": analysis.time_mode,
                "at": analysis.at.isoformat() if analysis.at else None,
                "timezone": query.timezone,
                "current_time_utc": query.current_time_utc.isoformat(),
            },
            degradations=list(dict.fromkeys(degraded)),
            coverage=coverage,
            collection=collection,
        )
        # Account for the actual envelope. A small overflow must not erase every
        # recalled fact and source. Keep the highest-priority raw details; memory
        # timeline groups are removed together so successors never stand alone.
        while len(encode(result.context()).encode()) > 12 * 1024 and detail_priority:
            identity = detail_priority.pop()
            result = result.model_copy(
                update={
                    "details": [
                        hit
                        for hit in result.details
                        if (hit.source.event_id, hit.source.pointer) != identity
                    ],
                    "status": "degraded",
                    "degradations": list(
                        dict.fromkeys([*result.degradations, "CONTEXT_BUDGET_LIMIT"])
                    ),
                }
            )
        while len(encode(result.context()).encode()) > 12 * 1024 and memory_groups:
            removed = memory_groups.pop()
            result = result.model_copy(
                update={
                    layer: [
                        hit for hit in getattr(result, layer) if hit.memory.memory_id not in removed
                    ]
                    for layer in ("m1", "m2")
                }
                | {
                    "status": "degraded",
                    "degradations": list(
                        dict.fromkeys([*result.degradations, "CONTEXT_BUDGET_LIMIT"])
                    ),
                }
            )
        if len(encode(result.context()).encode()) > 12 * 1024 and result.history.messages:
            result = result.model_copy(
                update={
                    "history": result.history.model_copy(
                        update={"messages": [], "complete": False}
                    ),
                    "status": "degraded",
                    "degradations": [*result.degradations, "HISTORY_BUDGET_LIMIT"],
                }
            )
        if result.collection:
            while len(encode(result.context()).encode()) > 12 * 1024 and result.collection["items"]:
                result.collection["items"].pop()
                result.collection["returned_count"] = len(result.collection["items"])
                result.collection["display_complete"] = False
        if result.degradations and result.coverage["complete"]:
            result = result.model_copy(update={"coverage": {**result.coverage, "complete": False}})
        return {"result": result}
