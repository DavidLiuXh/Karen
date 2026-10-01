"""Intent clarification graph and conversion into the engine's goal contract."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

from dynamic_graph import GoalSpec, ModelRequest
from dynamic_graph.contracts import default_output_schema
from dynamic_graph.graph.schemas import SchemaSpec
from dynamic_graph.models.client import ModelClient
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from .prompts import INTENT_SYSTEM_INSTRUCTION, INTENT_TASK_INSTRUCTION

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class IntentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(IntentContract):
    role: Literal["user", "assistant"]
    content: Text


class GoalDraft(IntentContract):
    objective: Text
    success_criteria: list[Text] = Field(min_length=1)
    constraints: list[Text] = Field(default_factory=list)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: SchemaSpec = Field(
        default_factory=lambda: SchemaSpec.model_validate(default_output_schema())
    )


class Clarification(IntentContract):
    outcome: Literal["needs_clarification"]
    questions: list[Text] = Field(min_length=1)


class Ready(IntentContract):
    outcome: Literal["ready"]
    goal: GoalDraft


class Assessment(IntentContract):
    decision: Annotated[Clarification | Ready, Field(discriminator="outcome")]


class IntentSession(IntentContract):
    """Caller-owned conversation; no service-level mutable session storage."""

    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    messages: tuple[Message, ...] = ()
    user_context: dict[str, JsonValue] = Field(default_factory=dict)
    questions: tuple[str, ...] = ()
    goal: GoalSpec | None = None


class IntentState(TypedDict):
    session: IntentSession
    decision: Clarification | Ready


class IntentRecognizer:
    def __init__(self, model: ModelClient):
        self.model = model
        graph = StateGraph(IntentState)
        graph.add_node("assess", self._assess)
        graph.add_node("clarify", self._clarify)
        graph.add_node("build_goal", self._build_goal)
        graph.add_edge(START, "assess")
        graph.add_conditional_edges("assess", self._route)
        graph.add_edge("clarify", END)
        graph.add_edge("build_goal", END)
        self.graph = graph.compile()

    async def advance(self, session: IntentSession, user_input: str) -> IntentSession:
        if session.goal is not None:
            raise ValueError("This session already has a goal; start a new session")
        message = Message(role="user", content=user_input)
        # Isolate nested user data from a model adapter and the returned session.
        session = session.model_copy(deep=True)
        messages = (*session.messages, message)
        session = session.model_copy(update={"messages": messages})
        state = await self.graph.ainvoke({"session": session})
        return state["session"]

    async def _assess(self, state: IntentState) -> dict:
        session = state["session"]
        request = ModelRequest(
            role="intent",
            system_instruction=INTENT_SYSTEM_INSTRUCTION,
            task_instruction=INTENT_TASK_INSTRUCTION,
            input_data={
                "messages": [m.model_dump(mode="json") for m in session.messages],
                "user_context": session.model_dump(mode="json")["user_context"],
            },
            output_schema=Assessment.model_json_schema(),
        )
        async with asyncio.timeout(request.timeout_seconds):
            response = await self.model.generate(request)
        assessment = Assessment.model_validate(response.payload)
        return {"decision": assessment.decision}

    @staticmethod
    def _route(state: IntentState) -> Literal["clarify", "build_goal"]:
        return "clarify" if isinstance(state["decision"], Clarification) else "build_goal"

    @staticmethod
    def _clarify(state: IntentState) -> dict:
        session = state["session"]
        questions = tuple(state["decision"].questions)
        messages = (
            *session.messages,
            Message(role="assistant", content="\n".join(questions)),
        )
        return {
            "session": session.model_copy(update={"messages": messages, "questions": questions})
        }

    @staticmethod
    def _build_goal(state: IntentState) -> dict:
        session = state["session"]
        decision = state["decision"]
        draft = decision.goal
        goal = GoalSpec(
            request_id=session.request_id,
            objective=draft.objective,
            success_criteria=[
                {"id": f"criterion_{index}", "description": description}
                for index, description in enumerate(draft.success_criteria, start=1)
            ],
            inputs=draft.inputs,
            output_schema=draft.output_schema,
            context={
                "constraints": draft.constraints,
                "user_context": session.user_context,
                "conversation": [m.model_dump(mode="json") for m in session.messages],
            },
        )
        return {"session": session.model_copy(update={"questions": (), "goal": goal})}
