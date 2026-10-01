"""Connect intent reception to DynamicAgentGraph's execution lifecycle."""

from dataclasses import dataclass

from dynamic_graph import CancellationToken, DynamicGraphEngine, ExecutionPolicy, RunResult

from .capabilities import all_capabilities_policy
from .intent import IntentRecognizer, IntentSession


@dataclass(frozen=True)
class TaskTurn:
    session: IntentSession
    result: RunResult | None = None


class Karen:
    def __init__(self, *, intent: IntentRecognizer, engine: DynamicGraphEngine):
        self.intent = intent
        self.engine = engine

    async def advance(
        self,
        session: IntentSession,
        user_input: str,
        *,
        policy: ExecutionPolicy | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> TaskTurn:
        session = await self.intent.advance(session, user_input)
        if session.goal is None:
            return TaskTurn(session)
        if policy is None:
            policy = all_capabilities_policy(self.engine)
        result = await self.engine.run(
            goal=session.goal, policy=policy, cancellation_token=cancellation_token
        )
        return TaskTurn(session, result)
