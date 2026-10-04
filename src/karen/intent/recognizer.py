"""Intent clarification graph and conversion into the engine's goal contract."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dynamic_graph import GoalSpec, ModelRequest
from dynamic_graph.contracts import default_output_schema
from dynamic_graph.graph.schemas import SchemaSpec
from dynamic_graph.models.client import ModelCallError, ModelClient
from langgraph.graph import END, START, StateGraph
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)
from tzlocal import get_localzone_name

from ..observability import Observer
from ..prompts import RESPONSE_INSTRUCTION
from .prompts import (
    DIRECT_RESPONSE_INSTRUCTION,
    GOAL_CONTEXT_INSTRUCTION,
    INTENT_SYSTEM_INSTRUCTION,
    INTENT_TASK_INSTRUCTION,
    ROUTING_INSTRUCTION,
)

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


class InputRouting(IntentContract):
    input_types: list[
        Literal["information", "conversation", "question", "task_request", "task_control"]
    ] = Field(min_length=1)
    handling: Literal["respond", "assess", "cancel"]
    task_relation: Literal["new", "continue"]
    reason: Text = Field(max_length=1000)

    @model_validator(mode="after")
    def valid_routing(self):
        if len(self.input_types) != len(set(self.input_types)):
            raise ValueError("input_types must be unique")
        if self.handling == "cancel" and (
            "task_control" not in self.input_types or self.task_relation != "continue"
        ):
            raise ValueError("cancel must refer to a pending task")
        if "task_request" in self.input_types and self.handling != "assess":
            raise ValueError("task requests must be assessed before execution")
        return self

    def check_session(self, session: IntentSession) -> None:
        if self.task_relation == "continue" and (session.completed or not session.questions):
            raise ValueError("No pending task is available to continue or cancel")


class Reply(IntentContract):
    outcome: Literal["reply"]
    answer: Text
    reason: str = Field(default="", max_length=1000)


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


class DirectAssessment(IntentContract):
    decision: Annotated[Reply | Clarification, Field(discriminator="outcome")]


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
    reply: Text | None = None
    routing: InputRouting | None = None

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

    @property
    def completed(self) -> bool:
        return self.goal is not None or self.reply is not None

    def new_request(self) -> IntentSession:
        return IntentSession(
            user_context=deepcopy(self.user_context),
            timezone=self.timezone,
            conversation_id=self.conversation_id,
        )

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
    decision: Clarification | Ready | Reply
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
        graph.add_node("respond", self.observer.node("intent.respond", self._respond, lambda r: r))
        graph.add_node("finish_reply", self._finish_reply)
        graph.add_node(
            "cancel",
            self.observer.node(
                "intent.cancel", self._cancel, lambda r: {"answer": r["session"].reply}
            ),
        )
        graph.add_conditional_edges(
            START,
            lambda state: state["session"].routing.handling,
            {"respond": "respond", "assess": "assess", "cancel": "cancel"},
        )
        graph.add_conditional_edges("respond", self._route)
        graph.add_edge("finish_reply", END)
        graph.add_edge("cancel", END)
        graph.add_conditional_edges("assess", self._route)
        graph.add_edge("clarify", END)
        graph.add_edge("build_goal", END)
        self.graph = graph.compile()

    async def classify(self, session: IntentSession, user_input: str) -> InputRouting:
        message = Message(role="user", content=user_input)
        pending = bool(session.questions) and not session.completed
        with self.observer.span("intent.classify"):
            request = ModelRequest(
                role="intent_router",
                system_instruction=ROUTING_INSTRUCTION,
                task_instruction="确定输入类型、与待澄清任务的关系及处理方式；不要生成回复、目标或执行计划。",
                input_data={
                    "text": message.content,
                    "pending_task": {
                        "messages": [m.model_dump(mode="json") for m in session.messages],
                        "questions": list(session.questions),
                    }
                    if pending
                    else None,
                },
                output_schema=InputRouting.model_json_schema(),
            )
            response = await self._generate(request)
            routing = InputRouting.model_validate(response.payload)
            routing.check_session(session)
            self.observer.emit("decision", data={"routing": routing})
            return routing

    async def _generate(self, request: ModelRequest):
        """Retry transient transport failures within one request's total time budget."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        try:
            async with asyncio.timeout(request.timeout_seconds):
                for attempt in range(1, 4):
                    try:
                        return await self.model.generate(
                            replace(request, timeout_seconds=max(0, deadline - loop.time()))
                        )
                    except ModelCallError as exc:
                        if (
                            not exc.retryable
                            or exc.code
                            not in {"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "MODEL_RATE_LIMITED"}
                            or attempt == 3
                        ):
                            raise
                        delay = 0.5 * attempt
                        if deadline - loop.time() <= delay:
                            raise
                        self.observer.emit(
                            "model.retry",
                            data={
                                "model_role": request.role,
                                "error_code": exc.code,
                                "attempt": attempt,
                                "next_attempt": attempt + 1,
                                "delay_seconds": delay,
                            },
                        )
                        await asyncio.sleep(delay)
        except TimeoutError as exc:
            raise ModelCallError(
                "MODEL_TIMEOUT", "Intent model request timed out", retryable=True
            ) from exc

    @staticmethod
    def prepare_session(
        session: IntentSession, routing: InputRouting, *, received_at: datetime | None = None
    ) -> IntentSession:
        routing.check_session(session)
        if session.completed or (session.messages and routing.task_relation == "new"):
            session = session.new_request().model_copy(update={"reference_time_utc": received_at})
        return session.anchor_time().model_copy(
            deep=True, update={"routing": routing.model_copy(deep=True)}
        )

    async def advance(
        self,
        session: IntentSession,
        user_input: str,
        *,
        memory_context: dict | None = None,
        routing: InputRouting | None = None,
    ) -> IntentSession:
        if session.completed:
            raise ValueError("This session already has a goal or reply; start a new session")
        message = Message(role="user", content=user_input)
        received_at = datetime.now(UTC)
        session = session.anchor_time()
        routing = routing or await self.classify(session, user_input)
        session = self.prepare_session(session, routing, received_at=received_at)
        session = session.model_copy(update={"messages": (*session.messages, message)})
        state = await self.graph.ainvoke(
            {"session": session, "memory_context": deepcopy(memory_context)}
        )
        updated = state["session"]
        if routing.handling == "cancel":
            route = "cancel"
        elif updated.reply is not None:
            route = "respond"
        elif updated.goal is not None:
            route = "execute"
        else:
            route = "clarify"
        self.observer.emit(
            "intent.routed",
            data={
                "input_types": routing.input_types,
                "task_relation": routing.task_relation,
                "route": route,
                "reason": routing.reason,
            },
        )
        return updated

    async def _respond(self, state: IntentState) -> dict:
        session = state["session"]
        # A direct reply cannot fabricate an unresolved, necessary history reference.
        guard = (
            self._memory_clarification(state) if "question" in session.routing.input_types else None
        )
        if guard:
            return guard
        request = ModelRequest(
            role="intent_response",
            system_instruction=DIRECT_RESPONSE_INSTRUCTION,
            task_instruction="直接回应本轮输入；必要信息缺失时提出简短澄清。",
            input_data=self._model_inputs(state),
            output_schema=DirectAssessment.model_json_schema(),
        )
        response = await self._generate(request)
        return {"decision": DirectAssessment.model_validate(response.payload).decision}

    @staticmethod
    def _finish_reply(state: IntentState) -> dict:
        session = state["session"]
        answer = state["decision"].answer
        return {
            "session": session.model_copy(
                update={
                    "reply": answer,
                    "questions": (),
                    "messages": (*session.messages, Message(role="assistant", content=answer)),
                }
            )
        }

    @staticmethod
    def _cancel(state: IntentState) -> dict:
        session = state["session"]
        answer = "好的，已取消当前待澄清的任务。"
        return {
            "session": session.model_copy(
                update={
                    "reply": answer,
                    "questions": (),
                    "messages": (*session.messages, Message(role="assistant", content=answer)),
                }
            )
        }

    @staticmethod
    def _model_inputs(state: IntentState) -> dict:
        session = state["session"]
        inputs = {
            "messages": [m.model_dump(mode="json") for m in session.messages],
            "user_context": session.model_dump(mode="json")["user_context"],
            "timezone": session.timezone,
            "time_context": session.time_context(),
            "routing": session.routing.model_dump(mode="json"),
        }
        if state.get("memory_context") is not None:
            inputs["memory"] = state["memory_context"]
        return inputs

    @staticmethod
    def _memory_clarification(state: IntentState) -> dict | None:
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
        return None

    async def _assess(self, state: IntentState) -> dict:
        guard = self._memory_clarification(state)
        if guard:
            return guard
        inputs = self._model_inputs(state)
        request = ModelRequest(
            role="intent",
            system_instruction=INTENT_SYSTEM_INSTRUCTION,
            task_instruction=INTENT_TASK_INSTRUCTION,
            input_data=inputs,
            output_schema=Assessment.model_json_schema(),
        )
        response = await self._generate(request)
        assessment = Assessment.model_validate(response.payload)
        return {"decision": assessment.decision}

    @staticmethod
    def _route(state: IntentState) -> Literal["clarify", "build_goal", "finish_reply"]:
        decision = state["decision"]
        if isinstance(decision, Clarification):
            return "clarify"
        return "finish_reply" if isinstance(decision, Reply) else "build_goal"

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
            "execution_instruction": GOAL_CONTEXT_INSTRUCTION,
            "input_routing": session.routing.model_dump(mode="json"),
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
