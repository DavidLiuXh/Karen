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
from .storage import Storage, encode
from .templates import acceptance_supported, instantiate, parameterized_graph


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
        await self._io(self.storage.start)
        lock = self.storage.root / ".lock"
        self._lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise
        self._accepting = True
        self._worker = asyncio.create_task(self._work())

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

    def _submit(self, kind, payload):
        job_id = uuid4().hex
        if not self._accepting or self._queue.full():
            reason = "queue_full" if self._accepting else "closed"
            self.observer.emit("workflow.submission", data={"accepted": False, "reason": reason})
            return {"accepted": False, "reason": reason}
        self._queue.put_nowait(
            (kind, payload, job_id, uuid4().hex, self.observer.context(), time.monotonic())
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
                    self.storage.load, payload["workflow_id"], payload["version"]
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

    def review_feedback(self, *, workflow, objective, text):
        return self._submit(
            "feedback", {"workflow": workflow, "previous_objective": objective, "text": text}
        )

    async def _feedback(self, data):
        feedback = await self._ask("workflow_feedback", prompts.FEEDBACK, data, Feedback)
        if (
            feedback.rejects_previous_result
            and feedback.evidence_quote.strip()
            and feedback.evidence_quote in data["text"]
        ):
            workflow = data["workflow"]
            await self._io(self.storage.pause, workflow["id"], workflow["version"])
            self.observer.emit(
                "workflow.paused",
                data={
                    "workflow_id": workflow["id"],
                    "version": workflow["version"],
                    "reason": "user_rejected_result",
                    "feedback": feedback,
                },
            )

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
        budget = 8192
        for attempt in range(2):
            try:
                response = await self.model.generate(
                    ModelRequest(
                        role=role,
                        system_instruction=prompts.SYSTEM,
                        task_instruction=instruction,
                        input_data=data,
                        output_schema=contract.model_json_schema(by_alias=True),
                        timeout_seconds=60,
                        max_output_tokens=budget,
                    )
                )
                return contract.model_validate(response.payload)
            except (ValidationError, ModelCallError) as exc:
                code = exc.code if isinstance(exc, ModelCallError) else "INVALID_CONTRACT"
                if attempt or code not in {
                    "INVALID_CONTRACT",
                    "MODEL_RESPONSE_INVALID",
                    "MODEL_RESPONSE_TRUNCATED",
                }:
                    raise
                if code == "MODEL_RESPONSE_TRUNCATED":
                    budget = expanded_output_budget(budget, self.model)
                instruction = (
                    instruction
                    + "\nRepair the response to conform exactly to the supplied output schema."
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
                    with path.open("rb") as handle:
                        raw = handle.read(32769)
                    if len(raw) <= 32768:
                        artifacts.append(json.loads(raw))
                except (OSError, ValueError):
                    pass
        return (
            graph,
            {
                "outputs": result.outputs,
                "node_records": result.node_records,
                "artifacts": artifacts,
            },
            snapshot,
        )

    async def _extract(self, run):
        if await self._io(self.storage.known_source, run.result.run_id, run.result.graph_hash):
            self.observer.emit("workflow.persistence", data={"reason": "duplicate_source"})
            return
        graph, evidence, snapshot = await self._io(self._load_run, run)
        data = {
            "request": run.text,
            "messages": run.messages,
            "goal": run.goal.model_dump(mode="json"),
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
            return
        with self.observer.span("workflow.validation"):
            try:
                parameterized_graph(run.goal, graph, proposal)
                structural = True
                reason = ""
            except (ValueError, KeyError, TypeError) as exc:
                structural, reason = False, str(exc)
            verification = await self._ask(
                "workflow_verify",
                prompts.VERIFY,
                {**data, "proposal": proposal.model_dump(mode="json", by_alias=True)},
                Verification,
            )
            supported = acceptance_supported(run.goal, verification, evidence)
            status = (
                "enabled"
                if structural and verification.generalizable and supported
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
        definition = Definition(
            workflow_id=uuid4().hex,
            version=1,
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
            validation_errors=[] if structural else [reason],
            source={
                "run_id": run.result.run_id,
                "graph_hash": run.result.graph_hash,
                "request_id": run.goal.request_id or "",
                "trace_id": run.source_trace_id,
                "prompt_version": prompts.PROMPT_VERSION,
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
                    "version": 1,
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
                        "parameters": [
                            {
                                k: v
                                for k, v in p.model_dump(by_alias=True).items()
                                if k not in {"source_value", "sites", "input_field"}
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
