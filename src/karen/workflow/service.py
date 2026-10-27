"""Background discovery and foreground bounded workflow resolution."""

import asyncio
import fcntl
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from dynamic_graph import GoalSpec, GraphSpec, ModelRequest
from dynamic_graph.models.client import ModelCallError, expanded_output_budget
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from ..observability import Observer
from . import prompts
from .contracts import (
    CompletedWorkflowRun,
    Definition,
    Feedback,
    Match,
    Proposal,
    Verification,
    WorkflowResolution,
)
from .evidence import project_artifacts
from .storage import Storage, encode
from .templates import (
    acceptance_supported,
    instantiate,
    parameter_input_schema,
    parameterized_graph,
)


class ReusableWorkflows:
    def __init__(self, *, root_dir, runs_dir, model, observer=None):
        self.storage = Storage(root_dir)
        self.runs_dir = Path(runs_dir).absolute()
        self.model = model
        self.observer = observer or Observer()
        self._queue = asyncio.Queue(maxsize=32)
        self._worker = None
        self._lock_fd = None
        self._accepting = False

    async def start(self):
        await self._io(self.storage.prepare)
        lock = self.storage.root / ".lock"
        self._lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self._io(self.storage.start)
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise
        self._accepting = True
        self._worker = asyncio.create_task(self._work())
        try:
            candidates = await self._io(self.storage.reviewable, prompts.PROMPT_VERSION)
        except BaseException:
            await self.close()
            raise
        for candidate in candidates:
            try:
                definition = await self._io(
                    self.storage.load, candidate["workflow_id"], candidate["version"], False
                )
            except (ValueError, OSError) as exc:
                self.observer.emit(
                    "workflow.review",
                    status="failed",
                    data={**candidate, "reason": type(exc).__name__},
                )
                continue
            self._submit(
                "review",
                {
                    "workflow_id": definition.workflow_id,
                    "version": definition.version,
                    "run_id": definition.source["run_id"],
                },
                source={
                    "trace_id": definition.source.get("trace_id"),
                    "request_id": definition.source.get("request_id"),
                },
            )

    def submit(self, run: CompletedWorkflowRun):
        eligible = run.result.execution_status == "COMPLETED" and run.result.output_complete
        self.observer.emit(
            "workflow.discovery", data={"source_run_id": run.result.run_id, "eligible": eligible}
        )
        if not eligible:
            self.observer.emit(
                "workflow.submission", data={"accepted": False, "reason": "not_completed"}
            )
            return {"accepted": False, "reason": "not_completed"}
        return self._submit("extract", run.model_copy(deep=True))

    def _submit(self, kind, payload, *, source=None):
        job_id = uuid4().hex
        if not self._accepting or self._queue.full():
            reason = "queue_full" if self._accepting else "closed"
            self.observer.emit("workflow.submission", data={"accepted": False, "reason": reason})
            return {"accepted": False, "reason": reason}
        self._queue.put_nowait(
            (
                kind,
                payload,
                job_id,
                uuid4().hex,
                source or self.observer.context(),
                time.monotonic(),
            )
        )
        self.observer.emit(
            "workflow.submission",
            data={"accepted": True, "job_id": job_id, "state": "queued", "pid": os.getpid()},
        )
        return {"accepted": True, "job_id": job_id}

    def record_outcome(self, *, workflow_id, version, result, request_id):
        invalid = any(
            d.code in {"GRAPH_VALIDATION_FAILED", "COMPILATION_FAILED"} for d in result.diagnostics
        )
        self._submit(
            "outcome",
            {
                "workflow_id": workflow_id,
                "version": version,
                "request_id": request_id,
                "run_id": result.run_id,
                "execution_status": result.execution_status,
                "output_complete": result.output_complete,
                "business_acceptance": "unknown",
                "pause": invalid,
                "recorded_at": datetime.now(UTC).isoformat(),
                "_result": result.model_copy(deep=True),
            },
        )

    async def _finish_outcome(self, payload):
        result = payload.pop("_result")
        try:
            if result.execution_status == "COMPLETED" and result.output_complete:
                definition = await self._io(
                    self.storage.load, payload["workflow_id"], payload["version"], False
                )

                def load_goal():
                    if len(result.run_id) != 32 or any(
                        c not in "0123456789abcdef" for c in result.run_id
                    ):
                        raise ValueError("invalid_run_id")
                    path = self.runs_dir / result.run_id / "goal.json"
                    if path.is_symlink() or not path.resolve().is_relative_to(
                        self.runs_dir.resolve()
                    ):
                        raise ValueError("invalid_source_goal")
                    with path.open("rb") as handle:
                        return GoalSpec.model_validate_json(handle.read(262145))

                goal = await self._io(load_goal)
                run = CompletedWorkflowRun(
                    text=goal.objective, messages=[], goal=goal, result=result, source_trace_id=""
                )
                graph, evidence, _ = await self._io(self._load_run, run)
                with self.observer.span("workflow.validation"):
                    verification = await self._ask(
                        "workflow_verify",
                        prompts.VERIFY,
                        {
                            "goal": goal.model_dump(mode="json"),
                            "graph": graph.document(),
                            "evidence": evidence,
                            "proposal": definition.proposal.model_dump(mode="json", by_alias=True),
                        },
                        Verification,
                    )
                    supported = acceptance_supported(goal, verification, evidence)
                    rejected = any(c.verdict == "failed" for c in verification.criteria)
                    payload["business_acceptance"] = (
                        "supported" if supported else "failed" if rejected else "unknown"
                    )
                    payload["pause"] |= rejected
                    self.observer.emit(
                        "workflow.validation",
                        data={
                            "workflow_id": definition.workflow_id,
                            "phase": "reuse_acceptance",
                            "acceptance": verification,
                            "result": payload["business_acceptance"],
                        },
                    )
        finally:
            await self._io(self.storage.outcome, payload)
            if payload["pause"]:
                self.observer.emit("workflow.paused", data=payload)

    def review_feedback(
        self,
        *,
        objective,
        text,
        workflow=None,
        source_run_id=None,
        source_event_id=None,
        request_id=None,
        source_request_id=None,
    ):
        return self._submit(
            "feedback",
            {
                "workflow": workflow,
                "previous_objective": objective,
                "text": text,
                "run_id": source_run_id,
                "source_event_id": source_event_id,
                "feedback_request_id": request_id,
                "source_request_id": source_request_id,
            },
        )

    def _source_run(self, definition):
        run_id = definition.source["run_id"]
        if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("invalid_source_run")
        directory = self.runs_dir / run_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(
            self.runs_dir.resolve()
        ):
            raise ValueError("source_path_invalid")

        def read(name, limit):
            path = directory / name
            if path.is_symlink():
                raise ValueError("source_symlink")
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("source_budget_limit")
            return data

        from dynamic_graph import RunResult

        goal = GoalSpec.model_validate_json(read("goal.json", 262144))
        result = RunResult.model_validate_json(read("result.json", 4 * 1024 * 1024))
        if (
            result.graph_hash != definition.source["graph_hash"]
            or result.execution_status != "COMPLETED"
            or not result.output_complete
        ):
            raise ValueError("source_no_longer_valid")
        messages = [
            message
            for message in goal.context.get("conversation", [])
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
        ]
        return CompletedWorkflowRun(
            text=next((m["content"] for m in messages if m["role"] == "user"), goal.objective),
            messages=messages,
            goal=goal,
            result=result,
            source_trace_id=definition.source.get("trace_id", ""),
        )

    async def _review(self, payload):
        definition = await self._io(
            self.storage.load, payload["workflow_id"], payload["version"], False
        )
        self.observer.emit(
            "workflow.review",
            data={
                "workflow_id": definition.workflow_id,
                "previous_version": definition.version,
                "reason": "algorithm_changed",
            },
        )
        run = await self._io(self._source_run, definition)
        await self._extract(run)

    async def _feedback(self, data):
        workflow = data.get("workflow")
        if workflow is None and data.get("run_id"):
            row = await self._io(self.storage.latest_source, data["run_id"])
            workflow = {"id": row["workflow_id"], "version": row["version"]} if row else None
        if workflow is None and data.get("source_request_id"):
            row = await self._io(self.storage.latest_request, data["source_request_id"])
            workflow = {"id": row["workflow_id"], "version": row["version"]} if row else None
        if workflow is None:
            self.observer.emit("workflow.feedback", data={"reason": "no_linked_definition"})
            return
        definition = await self._io(self.storage.load, workflow["id"], workflow["version"], False)
        feedback = await self._ask(
            "workflow_feedback",
            prompts.FEEDBACK,
            {
                **data,
                "criteria": [c.model_dump() for c in definition.goal_template.success_criteria],
                "acceptance": definition.acceptance.model_dump(),
            },
            Feedback,
        )
        valid_quote = (
            bool(feedback.evidence_quote.strip()) and feedback.evidence_quote in data["text"]
        )
        self.observer.emit(
            "workflow.feedback",
            data={
                "workflow_id": definition.workflow_id,
                "version": definition.version,
                "valid_quote": valid_quote,
                "feedback": feedback,
            },
        )
        if not valid_quote:
            return
        if feedback.rejects_previous_result:
            await self._io(self.storage.pause, definition.workflow_id, definition.version)
            self.observer.emit(
                "workflow.paused",
                data={
                    "workflow_id": definition.workflow_id,
                    "version": definition.version,
                    "reason": "user_rejected_result",
                    "feedback": feedback,
                },
            )
            return
        unknown = {c.criterion_id for c in definition.acceptance.criteria if c.verdict == "unknown"}
        accepted = set(feedback.accepted_criteria)
        if not accepted or not accepted <= unknown:
            return
        evidence = {
            "quote": feedback.evidence_quote,
            "criterion_ids": sorted(accepted),
            "source_role": "user",
            "source_run_id": definition.source["run_id"],
            "feedback_run_id": data.get("run_id"),
            "source_event_id": data.get("source_event_id"),
            "request_id": data.get("feedback_request_id"),
            "trace_id": self.observer.context().get("source_trace_id"),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        review_key = (
            prompts.PROMPT_VERSION
            + ":"
            + hashlib.sha256(
                encode(
                    {
                        "quote": evidence["quote"],
                        "criteria": evidence["criterion_ids"],
                        "event": evidence["source_event_id"],
                        "request": evidence["request_id"],
                    }
                )
            ).hexdigest()
        )
        self.observer.emit(
            "workflow.review",
            data={
                "workflow_id": definition.workflow_id,
                "previous_version": definition.version,
                "reason": "user_acceptance",
                "criterion_ids": sorted(accepted),
            },
        )
        run = await self._io(self._source_run, definition)
        await self._extract(run, review_key=review_key, extra_evidence=evidence)

    async def _io(self, operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Never release lifecycle locks while a storage thread is still writing.
            await task
            raise

    async def close(self):
        self._accepting = False
        if self._worker:
            await self._queue.join()
            self._queue.put_nowait(None)
            await self._worker
            self._worker = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _work(self):
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                kind, payload, job_id, trace_id, source, queued = item
                with self.observer.span(
                    "workflow.job",
                    trace_id=trace_id,
                    job_id=job_id,
                    request_id=source.get("request_id"),
                    source_request_id=source.get("request_id"),
                    source_trace_id=source.get("trace_id"),
                    source_run_id=payload.result.run_id
                    if kind == "extract"
                    else payload.get("run_id"),
                ) as outcome:
                    self.observer.emit(
                        "workflow.job",
                        data={
                            "state": "running",
                            "job_id": job_id,
                            "source_trace_id": source.get("trace_id"),
                            "pid": os.getpid(),
                            "queue_ms": round((time.monotonic() - queued) * 1000),
                        },
                    )
                    try:
                        async with asyncio.timeout(120):
                            if kind == "extract":
                                await self._extract(payload)
                            elif kind == "review":
                                await self._review(payload)
                            elif kind == "feedback":
                                await self._feedback(payload)
                            else:
                                await self._finish_outcome(payload)
                        outcome["state"] = "completed"
                    except Exception as exc:
                        outcome.update(
                            state="failed",
                            reason=getattr(exc, "code", None)
                            or (str(exc)[:200] if type(exc) is ValueError else type(exc).__name__),
                        )
                        self.observer.emit("workflow.job", status="failed", data=outcome)
            finally:
                self._queue.task_done()

    async def _ask(self, role, instruction, data, contract):
        if len(encode(data)) > 256 * 1024:
            raise ValueError("model_input_budget")
        schema = contract.model_json_schema(by_alias=True)
        if contract is Proposal:
            data, schema = Proposal.extraction_request(data)
        budget = 8192
        for attempt in range(2):
            try:
                if len(encode(data)) > 256 * 1024:
                    raise ValueError("model_input_budget")
                response = await self.model.generate(
                    ModelRequest(
                        role=role,
                        system_instruction=prompts.SYSTEM,
                        task_instruction=instruction,
                        input_data=data,
                        output_schema=schema,
                        timeout_seconds=60,
                        max_output_tokens=budget,
                    )
                )
                Draft202012Validator(schema).validate(response.payload)
                try:
                    return (
                        Proposal.from_extraction_payload(response.payload)
                        if contract is Proposal
                        else contract.model_validate(response.payload)
                    )
                except ValueError as exc:
                    if isinstance(exc, ValidationError):
                        raise
                    raise JSONSchemaValidationError(str(exc)) from exc
            except (ValidationError, JSONSchemaValidationError, ModelCallError) as exc:
                code = exc.code if isinstance(exc, ModelCallError) else "INVALID_CONTRACT"
                if attempt or code not in {
                    "INVALID_CONTRACT",
                    "MODEL_RESPONSE_INVALID",
                    "MODEL_RESPONSE_TRUNCATED",
                }:
                    raise
                if code == "MODEL_RESPONSE_TRUNCATED":
                    budget = expanded_output_budget(budget, self.model)
                if isinstance(exc, JSONSchemaValidationError):
                    data = {
                        **data,
                        "contract_feedback": {
                            "path": list(exc.absolute_path),
                            "validator": str(exc.validator),
                            "message": exc.message[:1200],
                        },
                    }
                elif isinstance(exc, ValidationError):
                    data = {
                        **data,
                        "contract_feedback": {
                            "errors": exc.errors(include_input=False, include_url=False)[:5]
                        },
                    }
                instruction = (
                    instruction
                    + "\nRepair the response to conform exactly to the supplied output schema and contract_feedback."
                )
                self.observer.emit(
                    "workflow.model_retry", data={"reason": code, "max_output_tokens": budget}
                )

    def _load_run(self, run):
        result = run.result
        if not result.recording or result.recording.get("status") != "complete":
            raise ValueError("source_unavailable")
        directory = self.runs_dir / result.run_id
        if (
            len(result.run_id) != 32
            or any(c not in "0123456789abcdef" for c in result.run_id)
            or not directory.resolve().is_relative_to(self.runs_dir.resolve())
            or directory.is_symlink()
        ):
            raise ValueError("source_path_invalid")

        def read(name, limit):
            path = directory / name
            if path.is_symlink():
                raise ValueError("source_symlink")
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("source_budget_limit")
            return data

        data = read("graph.json", 262144)
        if hashlib.sha256(data).hexdigest() != result.graph_hash:
            raise ValueError("source_hash_mismatch")
        recorded_goal = json.loads(read("goal.json", 262144))
        # Recorded goal is redacted. A discrepancy is not safe to reconstruct.
        if recorded_goal != run.goal.model_dump(mode="json", by_alias=True, exclude_none=True):
            raise ValueError("source_goal_mismatch")
        graph = GraphSpec.model_validate_json(data)
        snapshot = json.loads(read("capability_snapshot.json", 262144))
        artifacts = []
        read_bytes = 0
        skipped = 0
        for artifact in result.artifacts[:32]:
            if artifact.get("commit_state") != "committed":
                continue
            # Only load recorder-owned JSON artifact files under this exact run.
            reference = artifact.get("path") or artifact.get("ref")
            if not isinstance(reference, str):
                continue
            path = Path(reference)
            if not path.is_absolute():
                path = directory / path
            if (
                path.resolve().is_relative_to(directory.resolve())
                and path.suffix == ".json"
                and not path.is_symlink()
            ):
                try:
                    limit = min(4 * 1024 * 1024, 8 * 1024 * 1024 - read_bytes)
                    if limit <= 0:
                        skipped += 1
                        continue
                    with path.open("rb") as handle:
                        raw = handle.read(limit + 1)
                    read_bytes += len(raw)
                    if len(raw) <= limit:
                        document = json.loads(raw)
                        if isinstance(document, dict):
                            artifacts.append(document)
                    else:
                        skipped += 1
                except (OSError, ValueError):
                    skipped += 1
        artifacts, coverage = project_artifacts(artifacts, result.outputs)
        coverage["unread"] = skipped
        return (
            graph,
            {
                "outputs": result.outputs,
                "node_records": result.node_records,
                "artifacts": artifacts,
                "artifact_coverage": coverage,
            },
            snapshot,
        )

    async def _extract(self, run, *, review_key=None, extra_evidence=None):
        review_key = review_key or prompts.PROMPT_VERSION
        if await self._io(
            self.storage.known_source, run.result.run_id, run.result.graph_hash, review_key
        ):
            self.observer.emit("workflow.persistence", data={"reason": "duplicate_source"})
            return
        previous = await self._io(
            self.storage.latest_source, run.result.run_id, run.result.graph_hash
        )
        prior = (
            await self._io(self.storage.load, previous["workflow_id"], previous["version"], False)
            if previous
            else None
        )
        if previous and previous["status"] == "paused":
            self.observer.emit("workflow.review", data={"reason": "paused_requires_new_source"})
            return
        graph, evidence, snapshot = await self._io(self._load_run, run)
        user_evidence = list(prior.acceptance_evidence) if prior else []
        if extra_evidence and extra_evidence not in user_evidence:
            user_evidence.append(extra_evidence)
        evidence["user_feedback"] = user_evidence
        # Keep task constraints, not unrelated memory or full old conversations.
        goal_data = run.goal.model_dump(mode="json")
        goal_data["context"] = {
            key: value
            for key, value in run.goal.context.items()
            if key in {"constraints", "timezone", "time_context", "reference_time_utc"}
        }
        data = {
            "request": run.text,
            "messages": run.messages,
            "goal": goal_data,
            "graph": graph.document(),
            "evidence": evidence,
        }
        # Do not start unbounded model requests or store accidentally huge tool output.
        if len(encode(data)) > 256 * 1024:
            raise ValueError("extraction_input_budget")
        with self.observer.span("workflow.extraction"):
            proposal = await self._ask("workflow_extract", prompts.EXTRACT, data, Proposal)
            self.observer.emit("workflow.extraction", data={"proposal": proposal})
        if not proposal.reusable:
            self.observer.emit(
                "workflow.discovery",
                data={"reason": "not_reusable", "explanation": proposal.reason},
            )
            if prior:
                await self._io(
                    self.storage.review_rejected,
                    prior.workflow_id,
                    prior.version,
                    prompts.PROMPT_VERSION,
                    proposal.reason,
                )
            return
        with self.observer.span("workflow.validation"):
            prepared_graph = None
            prepared_goal = None
            verification = Verification(
                generalizable=False,
                reason="No reusable proposal verified",
                criteria=[
                    {
                        "criterion_id": c.id,
                        "verdict": "unknown",
                        "evidence": [],
                        "reason": "No reusable proposal verified",
                    }
                    for c in run.goal.success_criteria
                ],
            )
            for repair in range(3):
                try:
                    prepared_graph = parameterized_graph(run.goal, graph, proposal)
                    prepared_goal = GoalSpec.model_validate(
                        {
                            **run.goal.model_dump(mode="json"),
                            "context": goal_data["context"],
                            "inputs": {
                                **proposal.fixed_inputs,
                                "workflow_parameters": {
                                    p.name: p.source_value for p in proposal.parameters
                                },
                            },
                            "input_schema": parameter_input_schema(proposal),
                        }
                    )
                    structural, reason = True, ""
                except (ValueError, KeyError, TypeError) as exc:
                    structural, reason = False, str(exc)
                    prepared_graph, prepared_goal = None, None
                if structural or repair == 2:
                    verification = await self._ask(
                        "workflow_verify",
                        prompts.VERIFY,
                        {
                            # Structural compilation already checks every source location.
                            # Supplying both full graphs makes the semantic reviewer mistake
                            # removed source literals for retained runtime instructions.
                            **{key: value for key, value in data.items() if key != "graph"},
                            "proposal": proposal.model_dump(mode="json", by_alias=True),
                            "prepared_graph": prepared_graph.document()
                            if prepared_graph is not None
                            else None,
                            "prepared_goal": prepared_goal.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                            if prepared_goal is not None
                            else None,
                            "structural_error": reason,
                        },
                        Verification,
                    )
                    if structural and verification.generalizable:
                        break
                if repair == 2 or not proposal.reusable:
                    break
                feedback = (
                    reason
                    if not structural
                    else "generalization_rejected: " + verification.reason[:2000]
                )
                self.observer.emit(
                    "workflow.extraction_repair", data={"reason": feedback, "attempt": repair + 1}
                )
                proposal = await self._ask(
                    "workflow_extract",
                    prompts.REPAIR,
                    {
                        **data,
                        "previous_proposal": proposal.model_dump(mode="json", by_alias=True),
                        "validation_error": feedback,
                    },
                    Proposal,
                )
            supported = acceptance_supported(run.goal, verification, evidence)
            status = (
                "enabled"
                if proposal.reusable and structural and verification.generalizable and supported
                else "candidate"
            )
            self.observer.emit(
                "workflow.validation",
                data={
                    "structural": structural,
                    "reason": reason,
                    "acceptance": verification,
                    "status": status,
                },
            )
        # Keep goal input values for exact source reconstruction, but not old context/history.
        clean_goal = run.goal.model_copy(
            update={"context": {}, "request_id": None, "parent_run_id": None}
        )
        workflow_id = prior.workflow_id if prior else uuid4().hex
        definition = Definition(
            workflow_id=workflow_id,
            version=await self._io(self.storage.next_version, workflow_id),
            created_at=datetime.now(UTC),
            proposal=proposal,
            goal_template=clean_goal,
            graph_spec=graph,
            required_capabilities=sorted(
                {n.capability.key for n in graph.nodes if n.capability}
                | {s.reducer.key for s in graph.state_fields.values()}
            ),
            required_side_effect_tools=sorted(
                {
                    f"{c['name']}@{c['version']}"
                    for c in snapshot.get("capabilities", [])
                    if c.get("kind") == "tool"
                    and c.get("read_only") is False
                    and f"{c['name']}@{c['version']}"
                    in {n.capability.key for n in graph.nodes if n.capability}
                }
            ),
            acceptance=verification,
            acceptance_evidence=user_evidence,
            validation_errors=[] if structural else [reason],
            source={
                "run_id": run.result.run_id,
                "graph_hash": run.result.graph_hash,
                "request_id": run.goal.request_id or "",
                "trace_id": run.source_trace_id,
                "prompt_version": prompts.PROMPT_VERSION,
                "review_key": review_key,
                "extractor_model": str(getattr(self.model, "metadata", {}).get("model", "unknown")),
            },
        )
        with self.observer.span("workflow.persistence"):
            published = await self._io(self.storage.save, definition, status)
            self.observer.emit(
                "workflow.saved",
                data={
                    **published,
                    "workflow_id": definition.workflow_id,
                    "version": definition.version,
                    "previous_version": prior.version if prior else None,
                    "name": proposal.name,
                },
            )

    async def resolve(self, request):
        with self.observer.span("workflow.resolve"):
            try:
                async with asyncio.timeout(15):
                    return await self._resolve(request)
            except Exception as exc:
                self.observer.emit(
                    "workflow.resolve", data={"reason": type(exc).__name__, "status": "no_match"}
                )
                return WorkflowResolution(status="no_match", reason="unavailable")

    async def _resolve(self, request):
        pending = request.pending
        candidates = (
            [await self._io(self.storage.load, pending["workflow_id"], pending["version"])]
            if pending
            else await self._io(self.storage.candidates, request.text)
        )
        authorized = set(
            request.policy.allowed_tools
            + request.policy.allowed_reducers
            + request.policy.allowed_evaluators
        )
        candidates = [
            d
            for d in candidates
            if set(d.required_capabilities) <= authorized & set(request.capabilities)
            and set(d.required_side_effect_tools) <= set(request.policy.allowed_side_effect_tools)
        ]
        if not candidates:
            self.observer.emit(
                "workflow.resolve",
                data={
                    "status": "no_match",
                    "reason": "no_enabled_compatible_candidate",
                    "template_status_counts": await self._io(self.storage.status_counts),
                },
            )
            return WorkflowResolution(status="no_match", reason="not_found")
        match = await self._ask(
            "workflow_match",
            prompts.MATCH,
            {
                "text": request.text,
                "memory": request.memory_context,
                "pending": pending,
                "timezone": request.timezone,
                "local_date": request.received_at.astimezone(ZoneInfo(request.timezone))
                .date()
                .isoformat(),
                "received_at": request.received_at.isoformat(),
                "candidates": [
                    {
                        "workflow_id": d.workflow_id,
                        "purpose": d.proposal.purpose,
                        "applicability": d.proposal.applicability,
                        "exclusions": d.proposal.exclusions,
                        "fixed_inputs": d.proposal.fixed_inputs,
                        "parameters": [
                            {
                                k: v
                                for k, v in p.model_dump(by_alias=True).items()
                                if k not in {"source_value", "sites", "input_field", "text_slots"}
                            }
                            for p in d.proposal.parameters
                        ],
                    }
                    for d in candidates
                ],
            },
            Match,
        )
        selected = next((d for d in candidates if d.workflow_id == match.workflow_id), None)
        if not match.compatible or selected is None:
            self.observer.emit(
                "workflow.resolve", data={"status": "no_match", "reason": match.reason}
            )
            return WorkflowResolution(status="no_match", reason="incompatible")
        # Side effects must be authorized now, never by the source run.
        # ExecutionPolicy additionally enforces capability metadata at graph validation.
        parameters = {**(pending.get("parameters", {}) if pending else {}), **match.parameters}
        goal, graph, parameters, missing = instantiate(selected, request, parameters)
        self.observer.emit(
            "workflow.parameter_binding",
            data={
                "workflow_id": selected.workflow_id,
                "version": selected.version,
                "parameter_names": list(parameters),
                "missing": missing,
            },
        )
        if missing:
            if any(name not in match.questions for name in missing):
                return WorkflowResolution(status="no_match", reason="invalid_questions")
            return WorkflowResolution(
                status="needs_parameters",
                reason="missing_parameters",
                workflow_id=selected.workflow_id,
                version=selected.version,
                parameters=parameters,
                questions=[match.questions[n] for n in missing],
            )
        self.observer.emit(
            "workflow.resolve",
            data={
                "status": "matched",
                "workflow_id": selected.workflow_id,
                "version": selected.version,
                "reason": match.reason,
                "planner_skipped": True,
            },
        )
        return WorkflowResolution(
            status="matched",
            reason="compatible",
            workflow_id=selected.workflow_id,
            version=selected.version,
            parameters=parameters,
            goal=goal,
            graph_spec=graph,
        )
