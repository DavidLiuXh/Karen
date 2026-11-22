"""Connect intent reception, selective context recall and task execution."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from dynamic_graph import CancellationToken, DynamicGraphEngine, ExecutionPolicy, RunResult
from dynamic_graph.execution.errors import RunFailure

from .capabilities import all_capabilities_policy
from .context import ContextEvent, ContextMemory, RecallQuery, RecallResult
from .context.contracts import History, MemoryError, recall_decision_context
from .context.retrieval import fit_history_messages
from .intent import IntentRecognizer, IntentSession
from .observability import Observer
from .observability.viewer import TraceStore
from .task.resources import ExecutionResources
from .workflow import CompletedWorkflowRun, ReusableWorkflows, WorkflowRequest


@dataclass(frozen=True)
class TaskTurn:
    session: IntentSession
    result: RunResult | None = None
    memory_result: RecallResult | None = None
    memory_warnings: tuple[str, ...] = ()
    trace_id: str | None = None
    deliverables: tuple[dict, ...] = ()

    @property
    def response(self) -> str | None:
        return self.session.reply


class Karen:
    def __init__(
        self,
        *,
        intent: IntentRecognizer,
        engine: DynamicGraphEngine,
        memory: ContextMemory | None = None,
        observer: Observer | None = None,
        workflows: ReusableWorkflows | None = None,
        resources: ExecutionResources | None = None,
        snapshot_deliveries=None,
        recover_deliveries=None,
        link_run=None,
    ):
        self.intent = intent
        self.engine = engine
        self.memory = memory
        self.observer = observer or Observer()
        self.workflows = workflows
        self.resources = resources or ExecutionResources(self.observer)
        self.snapshot_deliveries = snapshot_deliveries
        self.recover_deliveries = recover_deliveries
        self.link_run = link_run

    @asynccontextmanager
    async def _foreground(self):
        if self.memory is None:
            yield
        else:
            async with self.memory.foreground():
                yield

    def _capture(self, session, event_type, payload, warnings, *, occurred_at=None, raw_only=False):
        if self.memory is None:
            return None
        try:
            return self.memory.submit(
                ContextEvent(
                    conversation_id=session.conversation_id,
                    request_id=session.request_id,
                    timezone=session.timezone,
                    project_id=session.user_context.get("project_id"),
                    event_type=event_type,
                    payload=payload,
                    **({"occurred_at": occurred_at} if occurred_at else {}),
                ),
                deferred=not raw_only,
                raw_only=raw_only
            )
        except MemoryError as exc:
            warnings.append(str(exc))
            return None

    def record_response(
        self, session: IntentSession, text: str, *, trace_id: str | None = None
    ) -> tuple[str, ...]:
        """Capture the response actually displayed by a UI, without coupling formatters."""
        warnings = []
        with self.observer.span(
            "response.displayed",
            trace_id=trace_id,
            conversation_id=session.conversation_id,
            request_id=session.request_id,
        ):
            self.observer.emit("response", data={"text": text})
            # Direct replies were captured with the request. Retain the rendered
            # execution response in m3 without extracting the same result again.
            if session.reply is None:
                self._capture(session, "assistant_message", {"content": text}, warnings, raw_only=True)
        return tuple(warnings)

    def finish_memory_request(self, session):
        if self.memory is not None:
            self.memory.finish_request(session.conversation_id, session.request_id)

    async def _recorded_task_history(self, session, user_input, *, search_quotes=False, bound_task_id=None):
        if self.observer.root_dir is None:
            return None
        user_messages = [user_input]
        if bound_task_id is None:
            user_messages.extend(m.content for m in reversed(session.messages) if m.role == "user")
        for text in user_messages:
            ids = set(re.findall(
                r"(?:task\s*id|任务\s*(?:id|编号))\s*(?:为|是|is\b|[:：=])?\s*`?([a-f0-9]{8})(?![a-z0-9_])",
                text.lower(),
            ))
            if ids:
                break
        excerpts = []
        if len(ids) > 1:
            return None
        if not ids and search_quotes:
            for text in user_messages:
                excerpts = [
                    next(part for part in match if part)
                    for match in re.findall(r'“([^”]+)”|「([^」]+)」|"([^"]+)"', text)
                ]
                if excerpts:
                    break
        if not ids and not excerpts and not bound_task_id:
            return None
        try:
            lookup = TraceStore(self.observer.root_dir).task_execution
            archived = None
            if ids or excerpts:
                archived = await asyncio.to_thread(lookup, next(iter(ids), None), excerpts=excerpts)
                if archived is not None:
                    archived["match_reason"] = "task_id" if ids else "quoted_request"
                if ids:
                    return archived
            if archived is None and bound_task_id:
                archived = await asyncio.to_thread(lookup, bound_task_id, completed_only=True)
                if archived is not None:
                    archived["match_reason"] = "bound_task"
            return archived
        except OSError:
            return None

    async def advance(
        self,
        session: IntentSession,
        user_input: str,
        *,
        policy: ExecutionPolicy | None = None,
        cancellation_token: CancellationToken | None = None,
        received_at: datetime | None = None,
        continue_task: bool = False,
        routing=None,
        task_id: str | None = None,
        turn_id: str | None = None,
    ) -> TaskTurn:
        previous_goal = session.goal
        previous_run_id = session.completed_run_id
        if session.completed:
            session = session.next_turn() if continue_task else session.new_request()
        from .intent.recognizer import Message

        Message(role="user", content=user_input)
        received_at = received_at or datetime.now(UTC)
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        received_at = received_at.astimezone(UTC)
        if session.reference_time_utc is None:
            session = session.model_copy(update={"reference_time_utc": received_at})
        trace_id = uuid4().hex
        with self.observer.span(
            "input", trace_id=trace_id, conversation_id=session.conversation_id,
            task_id=task_id, turn_id=turn_id or uuid4().hex
        ) as input_outcome:
            self.observer.emit(
                "input.received", data={"text": user_input, "timezone": session.timezone}
            )
            async with self._foreground():
                keep_open = False
                try:
                    try:
                        routing = routing or await self.intent.classify(session, user_input)
                    except BaseException:
                        # Even a failed classifier must not discard the user's raw input.
                        with self.observer.span("turn", request_id=session.request_id):
                            self.observer.emit(
                                "turn.input", data={"text": user_input, "timezone": session.timezone}
                            )
                            self._capture(
                                session,
                                "user_message",
                                {"content": user_input},
                                [],
                                occurred_at=received_at,
                            )
                            raise
                    if continue_task and routing.task_relation != "continue":
                        self.observer.emit(
                            "task.routing_bound",
                            data={
                                "request_id": session.request_id,
                                "model_relation": routing.task_relation,
                                "effective_relation": "continue",
                                "reason": "explicit_task_assignment",
                            },
                        )
                        routing = routing.model_copy(update={"task_relation": "continue"})
                    prepared = self.intent.prepare_session(session, routing, received_at=received_at)
                    if prepared.request_id != session.request_id and session.questions:
                        self.finish_memory_request(session)
                    session = prepared
                    with self.observer.span("turn", request_id=session.request_id) as outcome:
                        self.observer.emit(
                            "turn.input",
                            data={
                                "text": user_input,
                                "timezone": session.timezone,
                                "time_context": session.time_context(),
                                "clarification_round": sum(
                                    m.role == "assistant" for m in session.messages
                                ),
                            },
                        )
                        turn = await self._advance_turn(
                            session,
                            user_input,
                            policy,
                            cancellation_token,
                            trace_id,
                            received_at,
                            bound_task_id=task_id if continue_task else None,
                            previous_goal=previous_goal,
                            previous_run_id=previous_run_id,
                            task_id=task_id,
                            turn_id=turn_id,
                        )
                        if turn.result is not None:
                            outcome["outcome"] = turn.result.execution_status
                            if (
                                turn.result.execution_status == "COMPLETED"
                                and not turn.result.output_complete
                            ):
                                outcome["outcome"] = "INCOMPLETE"
                        elif routing.handling == "cancel":
                            outcome["outcome"] = "CANCELLED"
                        elif turn.response is not None:
                            outcome["outcome"] = "replied"
                        else:
                            outcome["outcome"] = "needs_clarification"
                        keep_open = outcome["outcome"] == "needs_clarification"
                        input_outcome.update(outcome)
                        return turn
                finally:
                    if not keep_open:
                        self.finish_memory_request(session)

    async def _advance_turn(
        self,
        session,
        user_input,
        policy,
        cancellation_token,
        trace_id,
        received_at,
        *,
        bound_task_id=None,
        previous_goal=None,
        previous_run_id=None,
        task_id=None,
        turn_id=None,
    ):
        warnings, recalled = [], None
        receipt = self._capture(
            session, "user_message", {"content": user_input}, warnings, occurred_at=received_at
        )
        if (
            self.workflows is not None
            and previous_goal is not None
            and (previous_run_id or previous_goal.context.get("workflow") or previous_goal.request_id)
        ):
            self.workflows.review_feedback(
                workflow=previous_goal.context.get("workflow"),
                objective=previous_goal.objective,
                text=user_input,
                source_run_id=previous_run_id,
                source_event_id=receipt.event_id if receipt else None,
                request_id=session.request_id,
                source_request_id=previous_goal.request_id,
            )
        archived = None
        bound_delivery = None
        if bound_task_id and session.routing.handling == "assess":
            archived = await self._recorded_task_history(session, user_input, bound_task_id=bound_task_id)
            if archived is not None and archived["match_reason"] == "bound_task":
                bound_delivery = {
                    "objective": archived["goal"]["objective"][:2000],
                    "delivered_files": archived["goal"].get("delivered_files", [])[:32],
                }
        if self.memory is not None and (
            session.routing.handling == "assess" or "question" in session.routing.input_types
        ):
            recalled = await self.memory.recall(
                RecallQuery(
                    text=user_input,
                    timezone=session.timezone,
                    current_time_utc=session.reference_time_utc,
                    conversation_id=session.conversation_id,
                    request_id=session.request_id,
                    exclude_event_ids=[receipt.event_id] if receipt else [],
                    current_task_messages=[m.model_dump(mode="json") for m in session.messages],
                    project_id=session.user_context.get("project_id"),
                    bound_delivery=bound_delivery,
                )
            )
        if session.routing.handling == "assess":
            requires_history = bool(recalled and recalled.coverage.get("requires_history"))
            if not bound_task_id or requires_history:
                archived = await self._recorded_task_history(
                    session, user_input, search_quotes=requires_history, bound_task_id=bound_task_id,
                )
            if archived is not None:
                files = archived["goal"].get("delivered_files")
                if archived["match_reason"] == "bound_task" and files and self.recover_deliveries:
                    recovered = self.recover_deliveries(bound_task_id, archived["run_id"], files)
                    archived["goal"]["delivered_files"] = [recovered.get(path, path) for path in files]
                    if recovered:
                        archived["goal"]["recovered_from_snapshot"] = recovered
                source = {
                    key: archived[key] for key in ("request_id", "task_id", "run_id", "occurred_at")
                }
                messages = [{
                    **source,
                    "role": "assistant",
                    "content": json.dumps(archived["goal"], ensure_ascii=False),
                    "truncated": False,
                }]
                for message in archived["conversation"]:
                    if (
                        isinstance(message, dict)
                        and message.get("role") in {"user", "assistant"}
                        and isinstance(message.get("content"), str)
                    ):
                        messages.append({
                            **source,
                            "role": message["role"],
                            "content": message["content"],
                            "truncated": False,
                        })
                bounded = fit_history_messages(messages, 48 * 1024)
                history = History(
                    status="selected",
                    reason="execution_record_" + archived["match_reason"],
                    complete=len(bounded) == len(messages) and not any(m["truncated"] for m in bounded),
                    messages=bounded,
                )
                recalled = (recalled or RecallResult(status="ok")).model_copy(
                    update={"history": history, "related_request_ids": [archived["request_id"]]}
                )
                self.observer.emit("memory.history." + archived["match_reason"], data={
                    key: archived[key] for key in ("task_id", "request_id", "run_id")
                })
        memory_context = recalled.context() if recalled is not None else None
        kwargs = {"memory_context": memory_context} if memory_context is not None else {}
        workflow = None
        uses_bound_delivery_only = (
            archived is not None and archived["match_reason"] == "bound_task"
            and recalled is not None
            and recalled.coverage.get("dialogue_dependency") == "current_task"
            and recalled.coverage.get("recall_needed") is False
        )
        if (
            self.workflows is not None
            and (not uses_bound_delivery_only or session.workflow_pending is not None)
            and session.routing.handling == "assess"
            and (
                (
                    session.routing.task_relation == "new"
                    and set(session.routing.input_types) & {"task_request", "question"}
                )
                or session.workflow_pending is not None
            )
            and (not session.questions or session.workflow_pending is not None)
        ):
            from .intent.recognizer import ClarificationItem, Message

            workflow = await self.workflows.resolve(
                WorkflowRequest(
                    text=user_input,
                    request_id=session.request_id,
                    conversation_id=session.conversation_id,
                    received_at=session.reference_time_utc or received_at,
                    timezone=session.timezone,
                    policy=policy or all_capabilities_policy(self.engine),
                    capabilities=[f"{c.name}@{c.version}" for c in self.engine.list_capabilities()],
                    memory_context=recall_decision_context(memory_context) if memory_context is not None else {},
                    pending=session.workflow_pending,
                )
            )
            if workflow.status == "needs_parameters":
                session = session.model_copy(
                    update={
                        "messages": (*session.messages, Message(role="user", content=user_input)),
                        "workflow_pending": {
                            "workflow_id": workflow.workflow_id,
                            "version": workflow.version,
                            "parameters": workflow.parameters,
                        },
                        "clarification_items": tuple(
                            ClarificationItem(
                                question_id=f"workflow_{index}",
                                text=text,
                                status="pending",
                                asked_after=len(session.messages),
                            )
                            for index, text in enumerate(workflow.questions)
                        ),
                        "displayed_question_ids": (),
                    }
                )
                self._capture(
                    session,
                    "assistant_message",
                    {"content": "\n".join(session.questions)},
                    warnings,
                )
                return TaskTurn(
                    session,
                    memory_result=recalled,
                    memory_warnings=tuple(warnings),
                    trace_id=trace_id,
                )
            if workflow.status == "matched":
                session = session.model_copy(
                    update={
                        "goal": workflow.goal,
                        "workflow_pending": None,
                        "clarification_items": (),
                        "messages": (*session.messages, Message(role="user", content=user_input)),
                    }
                )
            elif session.workflow_pending is not None:
                session = session.model_copy(update={"workflow_pending": None})
        if (
            recalled is not None
            and session.goal is None
            and session.routing.handling == "assess"
            and session.routing.task_relation == "new"
            and "question" in session.routing.input_types
            and set(session.routing.input_types) <= {"question", "task_request"}
            and (recalled.m2 or recalled.details)
        ):
            refined = await self.intent.classify(
                session, user_input, memory_context=recall_decision_context(memory_context),
            )
            session = session.model_copy(
                update={
                    "routing": session.routing.model_copy(
                        update={
                            "input_types": refined.input_types,
                            "handling": refined.handling,
                            "reason": refined.reason,
                        }
                    )
                }
            )
            self.observer.emit("intent.routing_refined", data={"routing": session.routing})
        if workflow is None or workflow.status != "matched":
            session = await self.intent.advance(
                session, user_input, routing=session.routing, **kwargs
            )
        if session.goal is None:
            if session.reply is not None:
                self._capture(session, "assistant_message", {"content": session.reply}, warnings)
                if session.routing.handling == "cancel":
                    self.observer.emit("task.cancelled", data={"scope": "pending_clarification"})
                return TaskTurn(
                    session,
                    memory_result=recalled,
                    memory_warnings=tuple(warnings),
                    trace_id=trace_id,
                )
            self._capture(
                session,
                "assistant_message",
                {"content": "\n".join(session.questions)},
                warnings,
            )
            return TaskTurn(
                session,
                memory_result=recalled,
                memory_warnings=tuple(warnings),
                trace_id=trace_id,
            )
        self._capture(session, "goal_created", session.goal.model_dump(mode="json"), warnings)
        if policy is None:
            policy = all_capabilities_policy(self.engine)
        recovery = {}
        if self.link_run is not None and task_id and turn_id and callable(getattr(self.engine, "resume", None)):
            def link(run_id):
                self.link_run(task_id, turn_id, run_id, session, trace_id)
            recovery["on_run_started"] = link
        async with self.resources.run(self.engine.list_capabilities()):
            with self.observer.span("execution"):
                self.observer.emit(
                    "execution.started",
                    data={
                        "goal": session.goal,
                        "policy": policy,
                        "runs_dir": str(self.engine.config.runs_dir),
                    },
                )
                if workflow is not None and workflow.status == "matched":
                    with self.observer.span("workflow.execution"):
                        result = await self.engine.run_graph(
                            goal=session.goal,
                            graph_spec=workflow.graph_spec,
                            policy=policy,
                            cancellation_token=cancellation_token,
                            **recovery,
                        )
                    self.workflows.record_outcome(
                        workflow_id=workflow.workflow_id,
                        version=workflow.version,
                        result=result,
                        request_id=session.request_id,
                    )
                else:
                    result = await self.engine.run(
                        goal=session.goal, policy=policy, cancellation_token=cancellation_token,
                        **recovery,
                    )
                deliveries = self._execution_result(result, task_id, turn_id)
        return self._completed_turn(session, result, recalled, warnings, trace_id, deliveries, workflow)

    async def resume_execution(self, session, *, run_id, task_id, turn_id, cancellation_token):
        resume = getattr(self.engine, "resume", None)
        if not callable(resume):
            raise RunFailure("RESUME_UNAVAILABLE", "Execution engine does not support checkpoints")
        trace_id = uuid4().hex
        with self.observer.span("turn", trace_id=trace_id, task_id=task_id, turn_id=turn_id,
                                conversation_id=session.conversation_id, request_id=session.request_id):
            with self.observer.span("execution"):
                async with self.resources.run(self.engine.list_capabilities()):
                    result = await resume(
                        run_id=run_id, policy=all_capabilities_policy(self.engine),
                        cancellation_token=cancellation_token,
                    )
                    deliveries = self._execution_result(result, task_id, turn_id)
                turn = self._completed_turn(session, result, None, [], trace_id, deliveries, None)
                self.finish_memory_request(session)
                return turn

    def _execution_result(self, result, task_id, turn_id):
        self.observer.emit(
            "execution.result",
            status="ok"
            if result.execution_status == "COMPLETED" and result.output_complete
            else "failed",
            data={
                "run_id": result.run_id,
                "execution_status": result.execution_status,
                "output_complete": result.output_complete,
                "outputs": result.outputs,
                "diagnostics": result.diagnostics,
                "node_records": result.node_records,
                "recording": result.recording,
                "usage": result.usage,
                "business_acceptance": "not_evaluated",
            },
        )
        if (self.snapshot_deliveries is not None and task_id and turn_id
                and result.execution_status == "COMPLETED" and result.output_complete):
            return self.snapshot_deliveries(task_id, turn_id, result)
        return ()

    def _completed_turn(self, session, result, recalled, warnings, trace_id, deliveries, workflow):
        public_result = result.model_dump(
            mode="json",
            include={
                "run_id",
                "request_id",
                "parent_run_id",
                "execution_status",
                "phase",
                "outputs",
                "output_complete",
                "artifacts",
                "diagnostics",
                "recording",
            },
        )
        self._capture(session, "task_result", public_result, warnings)
        if self.workflows is not None and (workflow is None or workflow.status != "matched"):
            self.workflows.submit(
                CompletedWorkflowRun(
                    text=next(
                        (m.content for m in session.messages if m.role == "user"), session.goal.objective
                    ),
                    messages=[m.model_dump(mode="json") for m in session.messages],
                    goal=session.goal,
                    result=result,
                    source_trace_id=trace_id,
                )
            )
        session = session.model_copy(
            update={
                "completed_run_id": result.run_id
                if result.execution_status == "COMPLETED" and result.output_complete
                else None
            }
        )
        return TaskTurn(session, result, recalled, tuple(warnings), trace_id, deliveries)

    def extract_workflow(self, turn: TaskTurn):
        session = turn.session
        if self.workflows is None:
            return {"accepted": False, "reason": "unavailable"}
        if (session.goal is None or turn.result is None
                or session.completed_run_id != turn.result.run_id):
            return {"accepted": False, "reason": "not_completed"}
        return self.workflows.submit(
            CompletedWorkflowRun(
                text=next((m.content for m in session.messages if m.role == "user"),
                          session.goal.objective),
                messages=[m.model_dump(mode="json") for m in session.messages],
                goal=session.goal,
                result=turn.result,
                source_trace_id=turn.trace_id or "",
            )
        )
