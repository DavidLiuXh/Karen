"""Connect intent reception, selective context recall and task execution."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from dynamic_graph import CancellationToken, DynamicGraphEngine, ExecutionPolicy, RunResult

from .capabilities import all_capabilities_policy
from .context import ContextEvent, ContextMemory, RecallQuery, RecallResult
from .context.contracts import History, MemoryError
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
    ):
        self.intent = intent
        self.engine = engine
        self.memory = memory
        self.observer = observer or Observer()
        self.workflows = workflows
        self.resources = resources or ExecutionResources(self.observer)

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

    async def _recorded_task_history(self, session, user_input, *, search_quotes=False):
        if self.observer.root_dir is None:
            return None
        user_messages = [
            user_input, *(m.content for m in reversed(session.messages) if m.role == "user")
        ]
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
        if not ids and not excerpts:
            return None
        try:
            archived = await asyncio.to_thread(
                TraceStore(self.observer.root_dir).task_execution,
                next(iter(ids), None), excerpts=excerpts,
            )
            if archived is not None:
                archived["match_reason"] = "task_id" if ids else "quoted_request"
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
                            previous_goal=previous_goal,
                            previous_run_id=previous_run_id,
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
        previous_goal=None,
        previous_run_id=None,
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
                )
            )
        if session.routing.handling == "assess":
            archived = await self._recorded_task_history(
                session, user_input,
                search_quotes=bool(recalled and recalled.coverage.get("requires_history")),
            )
            if archived is not None:
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
        kwargs = {"memory_context": recalled.context()} if recalled else {}
        workflow = None
        if (
            self.workflows is not None
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
                    memory_context=recalled.context() if recalled else {},
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
            refined = await self.intent.classify(session, user_input, **kwargs)
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
                        )
                    self.workflows.record_outcome(
                        workflow_id=workflow.workflow_id,
                        version=workflow.version,
                        result=result,
                        request_id=session.request_id,
                    )
                else:
                    result = await self.engine.run(
                        goal=session.goal, policy=policy, cancellation_token=cancellation_token
                    )
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
                        (m.content for m in session.messages if m.role == "user"), user_input
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
        return TaskTurn(session, result, recalled, tuple(warnings), trace_id)
