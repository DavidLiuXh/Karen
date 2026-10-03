"""Connect intent reception, selective context recall and task execution."""

from contextlib import asynccontextmanager
from dataclasses import dataclass

from dynamic_graph import CancellationToken, DynamicGraphEngine, ExecutionPolicy, RunResult

from .capabilities import all_capabilities_policy
from .context import ContextEvent, ContextMemory, RecallQuery, RecallResult
from .context.contracts import MemoryError
from .intent import IntentRecognizer, IntentSession


@dataclass(frozen=True)
class TaskTurn:
    session: IntentSession
    result: RunResult | None = None
    memory_result: RecallResult | None = None
    memory_warnings: tuple[str, ...] = ()


class Karen:
    def __init__(
        self,
        *,
        intent: IntentRecognizer,
        engine: DynamicGraphEngine,
        memory: ContextMemory | None = None,
    ):
        self.intent = intent
        self.engine = engine
        self.memory = memory

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

    def record_response(self, session: IntentSession, text: str) -> tuple[str, ...]:
        """Capture the response actually displayed by a UI, without coupling formatters."""
        warnings = []
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
        warnings, recalled = [], None
        async with self._foreground():
            receipt = self._capture(session, "user_message", {"content": user_input}, warnings)
            if self.memory is not None:
                recalled = await self.memory.recall(
                    RecallQuery(
                        text=user_input,
                        timezone=session.timezone,
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
                return TaskTurn(session, memory_result=recalled, memory_warnings=tuple(warnings))
            self._capture(session, "goal_created", session.goal.model_dump(mode="json"), warnings)
            if policy is None:
                policy = all_capabilities_policy(self.engine)
            result = await self.engine.run(
                goal=session.goal, policy=policy, cancellation_token=cancellation_token
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
            return TaskTurn(session, result, recalled, tuple(warnings))
