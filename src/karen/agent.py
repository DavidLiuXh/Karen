"""Connect intent reception, selective context recall and task execution."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from dynamic_graph import CancellationToken, DynamicGraphEngine, ExecutionPolicy, RunResult

from .capabilities import all_capabilities_policy
from .context import ContextEvent, ContextMemory, RecallQuery, RecallResult
from .context.contracts import MemoryError
from .intent import IntentRecognizer, IntentSession
from .observability import Observer


@dataclass(frozen=True)
class TaskTurn:
    session: IntentSession
    result: RunResult | None = None
    memory_result: RecallResult | None = None
    memory_warnings: tuple[str, ...] = ()
    trace_id: str | None = None


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

    def _capture(self, session, event_type, payload, warnings):
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
    ) -> TaskTurn:
        if session.goal is not None:
            session = IntentSession(
                user_context=session.user_context,
                timezone=session.timezone,
                conversation_id=session.conversation_id,
            )
        # Validate before capturing an empty/invalid request.
        from .intent.recognizer import Message

        Message(role="user", content=user_input)
        session = session.anchor_time()
        trace_id = uuid4().hex
        with self.observer.span(
            "turn",
            trace_id=trace_id,
            turn_id=uuid4().hex,
            conversation_id=session.conversation_id,
            request_id=session.request_id,
        ) as outcome:
            self.observer.emit(
                "turn.input",
                data={
                    "text": user_input,
                    "timezone": session.timezone,
                    "time_context": session.time_context(),
                    "clarification_round": sum(m.role == "assistant" for m in session.messages),
                },
            )
            turn = await self._advance_turn(
                session, user_input, policy, cancellation_token, trace_id
            )
            outcome["outcome"] = (
                turn.result.execution_status if turn.result else "needs_clarification"
            )
            if (
                turn.result
                and turn.result.execution_status == "COMPLETED"
                and not turn.result.output_complete
            ):
                outcome["outcome"] = "INCOMPLETE"
            return turn

    async def _advance_turn(self, session, user_input, policy, cancellation_token, trace_id):
        warnings, recalled = [], None
        async with self._foreground():
            receipt = self._capture(session, "user_message", {"content": user_input}, warnings)
            if self.memory is not None:
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
            session = await self.intent.advance(session, user_input, **kwargs)
            if session.goal is None:
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
