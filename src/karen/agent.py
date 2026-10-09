"""Connect intent reception, selective context recall and task execution."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from dynamic_graph import (
    CancellationToken,
    DynamicGraphEngine,
    ExecutionPolicy,
    GoalSpec,
    RunResult,
)

from .capabilities import all_capabilities_policy
from .context import ContextEvent, ContextMemory, RecallQuery, RecallResult
from .context.contracts import MemoryError
from .intent import IntentRecognizer, IntentSession
from .observability import Observer
from .prompts import PUBLIC_LOOKUP_OBJECTIVE


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
    ):
        self.intent = intent
        self.engine = engine
        self.memory = memory
        self.observer = observer or Observer()

    @asynccontextmanager
    async def _foreground(self):
        if self.memory is None:
            yield
        else:
            async with self.memory.foreground():
                yield

    def _capture(self, session, event_type, payload, warnings, *, occurred_at=None):
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
                )
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
            self._capture(session, "assistant_message", {"content": text}, warnings)
        return tuple(warnings)

    async def advance(
        self,
        session: IntentSession,
        user_input: str,
        *,
        policy: ExecutionPolicy | None = None,
        cancellation_token: CancellationToken | None = None,
        received_at: datetime | None = None,
    ) -> TaskTurn:
        if session.completed:
            session = session.new_request()
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
            "input", trace_id=trace_id, conversation_id=session.conversation_id, turn_id=uuid4().hex
        ) as input_outcome:
            self.observer.emit(
                "input.received", data={"text": user_input, "timezone": session.timezone}
            )
            async with self._foreground():
                try:
                    routing = await self.intent.classify(session, user_input)
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
                session = self.intent.prepare_session(session, routing, received_at=received_at)
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
                        session, user_input, policy, cancellation_token, trace_id, received_at
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
                    input_outcome.update(outcome)
                    return turn

    async def _advance_turn(
        self, session, user_input, policy, cancellation_token, trace_id, received_at
    ):
        warnings, recalled = [], None
        receipt = self._capture(
            session, "user_message", {"content": user_input}, warnings, occurred_at=received_at
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
        kwargs = {"memory_context": recalled.context()} if recalled else {}
        if (
            recalled is not None
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
        session = await self.intent.advance(session, user_input, routing=session.routing, **kwargs)
        if session.goal and session.goal.context.get("information_to_verify"):
            queries = session.goal.context["information_to_verify"]
            if not session.public_lookup or session.public_lookup.get("queries") != queries:
                observations = await self._lookup_public_information(
                    session, queries, policy, cancellation_token,
                )
                if observations["status"] != "completed":
                    # A transport/tool failure is not a new question for the user.
                    session = session.model_copy(update={
                        "goal": None, "public_lookup": observations,
                        "reply": "所需公共资料暂未能核实（" + observations["error_code"]
                                 + "），尚未执行后续任务。请稍后重试。",
                    })
                else:
                    session = await self.intent.reassess_after_lookup(session, observations, **kwargs)
        if session.goal is None:
            if session.reply is not None:
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
        with self.observer.span("execution"):
            self.observer.emit(
                "execution.started",
                data={
                    "goal": session.goal,
                    "policy": policy,
                    "runs_dir": str(self.engine.config.runs_dir),
                },
            )
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
        return TaskTurn(session, result, recalled, tuple(warnings), trace_id)

    async def _lookup_public_information(self, session, queries, policy, cancellation_token):
        """Prepare public evidence under a narrower policy before business execution."""
        base = policy if policy is not None else all_capabilities_policy(self.engine)
        # Generic HTTP fetch can address private/local services. Only the public
        # search capability is authorized during this preparation step.
        allowed = [f"{cap.name}@{cap.version}" for cap in self.engine.list_capabilities()
                   if cap.kind == "tool" and cap.name == "tavily.search"
                   and cap.to_dict()["read_only"]
                   and f"{cap.name}@{cap.version}" in base.allowed_tools]
        if not allowed:
            observations = {"queries": queries, "status": "unavailable",
                            "error_code": "PUBLIC_LOOKUP_UNAVAILABLE"}
            self.observer.emit("intent.public_lookup", status="failed", data=observations)
            return observations
        lookup_policy = base.model_copy(update={
            "allowed_tools": allowed, "allowed_side_effect_tools": [], "allowed_evaluators": [],
            "max_nodes": min(base.max_nodes, 8), "max_tool_calls": min(base.max_tool_calls, 8),
            "max_model_calls": min(base.max_model_calls, 12),
            "max_result_bytes": min(base.max_result_bytes, 65536),
            "run_timeout_seconds": min(base.run_timeout_seconds, 120),
        })
        goal = GoalSpec(
            request_id=session.request_id,
            objective=PUBLIC_LOOKUP_OBJECTIVE,
            inputs={"questions": queries},
            context={"timezone": session.timezone, "time_context": session.time_context()},
        )
        with self.observer.span("intent.public_lookup"):
            self.observer.emit("lookup.started", data={"queries": queries, "policy": lookup_policy})
            result = await self.engine.run(goal=goal, policy=lookup_policy,
                                           cancellation_token=cancellation_token)
            completed = (result.execution_status == "COMPLETED" and result.output_complete
                        and result.usage.get("tool_calls", 0) > 0)
            observations = {
                "queries": queries, "status": "completed" if completed else "failed",
                "run_id": result.run_id, "outputs": result.outputs if completed else {},
                "error_code": ("" if completed else result.diagnostics[0].code
                               if result.diagnostics else "PUBLIC_LOOKUP_UNCONFIRMED"),
            }
            self.observer.emit("lookup.result", status="ok" if completed else "failed", data=observations)
        return observations
