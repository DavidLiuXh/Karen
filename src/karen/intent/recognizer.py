"""Intent clarification graph and conversion into the engine's goal contract."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dynamic_graph import GoalSpec, ModelRequest
from dynamic_graph.contracts import default_output_schema
from dynamic_graph.graph.schemas import SchemaSpec
from dynamic_graph.models.client import ModelClient
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator
from tzlocal import get_localzone_name

from ..observability import Observer
from ..prompts import RESPONSE_INSTRUCTION
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
    reason: str = Field(default="", max_length=1000)


class Ready(IntentContract):
    outcome: Literal["ready"]
    goal: GoalDraft
    reason: str = Field(default="", max_length=1000)


class Assessment(IntentContract):
    decision: Annotated[Clarification | Ready, Field(discriminator="outcome")]


class IntentSession(IntentContract):
    """Caller-owned conversation; no service-level mutable session storage."""

    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    conversation_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    messages: tuple[Message, ...] = ()
    user_context: dict[str, JsonValue] = Field(default_factory=dict)
    timezone: Text = Field(default_factory=get_localzone_name, validate_default=True)
    reference_time_utc: datetime | None = None
    questions: tuple[str, ...] = ()
    goal: GoalSpec | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone must be a valid IANA timezone name") from None
        return value

    @field_validator("reference_time_utc")
    @classmethod
    def validate_reference_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("reference_time_utc must be timezone-aware")
            return value.astimezone(UTC)
        return None

    def anchor_time(self) -> IntentSession:
        """Anchor relative dates at the first input, retaining them during clarification."""
        if self.reference_time_utc is not None:
            return self
        return self.model_copy(update={"reference_time_utc": datetime.now(UTC)})

    def time_context(self) -> dict[str, JsonValue]:
        if self.reference_time_utc is None:
            raise ValueError("A time reference must be anchored before assessment")
        local = self.reference_time_utc.astimezone(ZoneInfo(self.timezone))
        return {
            "reference_time_utc": self.reference_time_utc.isoformat(),
            "timezone": self.timezone,
            "local_datetime": local.isoformat(),
            "local_date": local.date().isoformat(),
            "relative_dates": {
                name: (local.date() + timedelta(days=offset)).isoformat()
                for name, offset in (
                    ("day_before_yesterday", -2),
                    ("yesterday", -1),
                    ("today", 0),
                    ("tomorrow", 1),
                    ("day_after_tomorrow", 2),
                )
            },
        }


class IntentState(TypedDict):
    session: IntentSession
    decision: Clarification | Ready
    memory_context: dict | None


class IntentRecognizer:
    def __init__(self, model: ModelClient, *, observer: Observer | None = None):
        self.model = model
        self.observer = observer or Observer()
        graph = StateGraph(IntentState)
        graph.add_node("assess", self.observer.node("intent.assess", self._assess, lambda r: r))
        graph.add_node(
            "clarify",
            self.observer.node(
                "intent.clarify", self._clarify, lambda r: {"questions": r["session"].questions}
            ),
        )
        graph.add_node(
            "build_goal",
            self.observer.node(
                "intent.build_goal", self._build_goal, lambda r: {"goal": r["session"].goal}
            ),
        )
        graph.add_edge(START, "assess")
        graph.add_conditional_edges("assess", self._route)
        graph.add_edge("clarify", END)
        graph.add_edge("build_goal", END)
        self.graph = graph.compile()

    async def advance(
        self, session: IntentSession, user_input: str, *, memory_context: dict | None = None
    ) -> IntentSession:
        if session.goal is not None:
            raise ValueError("This session already has a goal; start a new session")
        message = Message(role="user", content=user_input)
        # Isolate nested user data from a model adapter and the returned session.
        session = session.anchor_time().model_copy(deep=True)
        messages = (*session.messages, message)
        session = session.model_copy(update={"messages": messages})
        state = await self.graph.ainvoke(
            {"session": session, "memory_context": deepcopy(memory_context)}
        )
        return state["session"]

    async def _assess(self, state: IntentState) -> dict:
        session = state["session"]
        memory = state.get("memory_context")
        if (
            memory
            and (
                memory.get("coverage", {}).get("requires_history")
                or memory.get("history", {}).get("status") in {"ambiguous", "unavailable"}
            )
            and memory.get("history", {}).get("status") != "selected"
        ):
            from .prompts import HISTORY_CLARIFICATION

            return {
                "decision": Clarification(
                    outcome="needs_clarification",
                    questions=[HISTORY_CLARIFICATION],
                    reason="Required history was unavailable or ambiguous.",
                )
            }
        if memory and (memory.get("collection") or {}).get("reason") == "needs_time_range":
            from .prompts import TIME_RANGE_CLARIFICATION

            return {
                "decision": Clarification(
                    outcome="needs_clarification",
                    questions=[TIME_RANGE_CLARIFICATION],
                    reason="Task collection needs an explicit time range.",
                )
            }
        inputs = {
            "messages": [m.model_dump(mode="json") for m in session.messages],
            "user_context": session.model_dump(mode="json")["user_context"],
            "timezone": session.timezone,
            "time_context": session.time_context(),
        }
        if memory is not None:
            inputs["memory"] = memory
        request = ModelRequest(
            role="intent",
            system_instruction=INTENT_SYSTEM_INSTRUCTION,
            task_instruction=INTENT_TASK_INSTRUCTION,
            input_data=inputs,
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
        context = {
            "timezone": session.timezone,
            "time_context": session.time_context(),
            "response_instruction": RESPONSE_INSTRUCTION,
            "constraints": draft.constraints,
            "user_context": session.user_context,
            "conversation": [m.model_dump(mode="json") for m in session.messages],
        }
        if state.get("memory_context") is not None:
            context["memory"] = state["memory_context"]
        goal = GoalSpec(
            request_id=session.request_id,
            objective=draft.objective,
            success_criteria=[
                {"id": f"criterion_{index}", "description": description}
                for index, description in enumerate(draft.success_criteria, start=1)
            ],
            inputs=draft.inputs,
            output_schema=draft.output_schema,
            context=context,
        )
        return {"session": session.model_copy(update={"questions": (), "goal": goal})}
