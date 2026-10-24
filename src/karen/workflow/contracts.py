"""Data exchanged by discovery, matching and prepared execution."""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from dynamic_graph import ExecutionPolicy, GoalSpec, GraphSpec, RunResult
from dynamic_graph.graph.schemas import SchemaSpec
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Site(Contract):
    kind: Literal["node", "state"]
    owner: str
    binding: str = ""


class Parameter(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    schema_: SchemaSpec = Field(alias="schema")
    source_value: JsonValue
    input_field: str | None = None
    sites: list[Site] = Field(default_factory=list)
    default_rule: Literal["none", "today"] = "none"


class Proposal(Contract):
    reusable: bool
    reason: str
    name: str
    purpose: str
    applicability: list[str]
    exclusions: list[str]
    parameters: list[Parameter]


class CriterionEvidence(Contract):
    criterion_id: str
    verdict: Literal["supported", "failed", "unknown"]
    evidence: list[str] = Field(
        description="Exact JSON pointers into the verifier request, starting with /evidence/; cite recorded artifacts or outputs, never invented paths."
    )
    reason: str


class Verification(Contract):
    generalizable: bool
    reason: str
    criteria: list[CriterionEvidence]


class Feedback(Contract):
    rejects_previous_result: bool
    evidence_quote: str
    reason: str


class Definition(Contract):
    schema_version: Literal["1"] = "1"
    workflow_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: int = Field(ge=1)
    created_at: datetime
    proposal: Proposal
    goal_template: GoalSpec
    graph_spec: GraphSpec
    required_capabilities: list[str]
    required_side_effect_tools: list[str] = Field(default_factory=list)
    acceptance: Verification
    validation_errors: list[str] = Field(default_factory=list)
    source: dict[str, str]


class CompletedWorkflowRun(Contract):
    text: str
    messages: list[dict]
    goal: GoalSpec
    result: RunResult
    source_trace_id: str


class WorkflowRequest(Contract):
    text: str
    request_id: str
    conversation_id: str
    received_at: datetime
    timezone: str
    policy: ExecutionPolicy
    capabilities: list[str]
    memory_context: dict = Field(default_factory=dict)
    pending: dict | None = None

    @field_validator("received_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value):
        ZoneInfo(value)
        return value


class Match(Contract):
    workflow_id: str | None
    compatible: bool
    reason: str
    parameters: dict[str, JsonValue]
    questions: dict[str, str]


class WorkflowResolution(Contract):
    status: Literal["matched", "needs_parameters", "no_match"]
    reason: str
    workflow_id: str | None = None
    version: int | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    questions: list[str] = Field(default_factory=list)
    goal: GoalSpec | None = None
    graph_spec: GraphSpec | None = None

    @model_validator(mode="after")
    def consistent_resolution(self):
        if self.status == "matched":
            if (
                not self.workflow_id
                or self.version is None
                or self.goal is None
                or self.graph_spec is None
                or self.questions
            ):
                raise ValueError(
                    "matched requires a workflow version, goal and graph, without questions"
                )
        elif self.status == "needs_parameters":
            if (
                not self.workflow_id
                or self.version is None
                or not self.questions
                or self.goal is not None
                or self.graph_spec is not None
            ):
                raise ValueError(
                    "needs_parameters requires a version and questions, without an executable graph"
                )
        elif self.goal is not None or self.graph_spec is not None:
            raise ValueError("no_match cannot include an executable graph")
        return self
