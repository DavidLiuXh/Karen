"""Data exchanged by discovery, matching and prepared execution."""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from dynamic_graph import ExecutionPolicy, GoalSpec, GraphSpec, RunResult
from dynamic_graph.graph.schemas import SchemaSpec, infer_schema
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from . import prompts as prompt_text


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Site(Contract):
    kind: Literal["node", "state"]
    owner: str
    binding: str = ""


class TextSlot(Contract):
    kind: Literal["instruction", "node_literal", "state_literal"]
    owner: str
    binding: str = ""
    occurrence: int = Field(default=0, ge=0)
    role: Literal["parameter", "invariant"] = "parameter"
    reason: str = ""

    @model_validator(mode="after")
    def invariant_is_explained(self):
        if self.role == "invariant" and not self.reason.strip():
            raise ValueError("invariant text needs an explanation")
        return self


class Parameter(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    schema_: SchemaSpec = Field(alias="schema")
    source_value: JsonValue
    input_field: str | None = None
    sites: list[Site] = Field(default_factory=list)
    default_rule: Literal["none", "today"] = "none"
    text_slots: list[TextSlot] = Field(default_factory=list)


class Proposal(Contract):
    reusable: bool
    reason: str
    name: str
    purpose: str
    applicability: list[str]
    exclusions: list[str]
    parameters: list[Parameter]
    fixed_inputs: dict[str, JsonValue] = Field(default_factory=dict)

    @classmethod
    def extraction_request(cls, data):
        """Build a source-bound wire schema; stored definitions keep dictionary inputs."""
        from .templates import source_binding_sites, source_text_slots

        schema = cls.model_json_schema(by_alias=True)
        source_inputs = data["goal"]["inputs"]
        # A list avoids models treating every optional object property as a
        # required fixed value. Normalize once at this model boundary.
        schema["properties"]["fixed_inputs"] = {
            "type": "array",
            "default": [],
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": [name]},
                            "value": {**infer_schema(value), "enum": [value]},
                        },
                        "required": ["field", "value"],
                        "additionalProperties": False,
                    }
                    for name, value in source_inputs.items()
                ]
            }
            if source_inputs
            else False,
        }
        previous = data.get("previous_proposal")
        if previous is not None:
            data = {
                **data,
                "previous_proposal": {
                    **previous,
                    "fixed_inputs": [
                        {"field": name, "value": value}
                        for name, value in previous.get("fixed_inputs", {}).items()
                    ],
                },
            }
        schema["$defs"]["Parameter"]["properties"]["input_field"]["enum"] = [
            *source_inputs,
            None,
        ]

        def locator_schema(locations):
            return (
                {
                    "allOf": [
                        {"$ref": "#/$defs/TextSlot"},
                        {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        key: {"enum": [value]} for key, value in location.items()
                                    },
                                    "required": list(location),
                                }
                                for location in locations
                            ]
                        },
                    ]
                }
                if locations
                else False
            )

        def site_schema(locations):
            return (
                {
                    "allOf": [
                        {"$ref": "#/$defs/Site"},
                        {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        key: {"enum": [value]} for key, value in location.items()
                                    },
                                    "required": list(location),
                                }
                                for location in locations
                            ]
                        },
                    ]
                }
                if locations
                else False
            )

        valid_sites = [
            {"kind": "node", "owner": node["id"], "binding": name}
            for node in data["graph"]["nodes"]
            for name in node["input_bindings"]
        ]
        valid_sites += [
            {"kind": "state", "owner": name, "binding": ""}
            for name in data["graph"]["state_fields"]
        ]
        schema["$defs"]["Parameter"]["properties"]["sites"]["items"] = site_schema(valid_sites)
        inventory = []
        for field, value in source_inputs.items():
            locations = source_text_slots(data["graph"], value)
            inventory.append(
                {
                    "input_field": field,
                    "source_value": value,
                    "text_slots": locations,
                    "binding_sites": source_binding_sites(data["graph"], value, field),
                    "input_references": [
                        {"node": node["id"], "binding": key}
                        for node in data["graph"]["nodes"]
                        for key, binding in node["input_bindings"].items()
                        if binding.get("source") == "input"
                        and binding.get("pointer", "").split("/")[1:2]
                        == [field.replace("~", "~0").replace("/", "~1")]
                    ],
                }
            )
            schema["$defs"]["Parameter"].setdefault("allOf", []).append(
                {
                    "if": {
                        "properties": {"input_field": {"enum": [field]}},
                        "required": ["input_field"],
                    },
                    "then": {
                        "properties": {
                            "source_value": {"enum": [value]},
                            "text_slots": {"items": locator_schema(locations)},
                            "sites": {
                                "items": site_schema(
                                    source_binding_sites(data["graph"], value, field)
                                )
                            },
                        }
                    },
                }
            )
            schema.setdefault("allOf", []).append(
                {
                    "if": {
                        "properties": {
                            "fixed_inputs": {
                                "contains": {
                                    "properties": {"field": {"enum": [field]}},
                                    "required": ["field"],
                                }
                            }
                        },
                        "required": ["fixed_inputs"],
                    },
                    "then": {
                        "properties": {
                            "parameters": {
                                "not": {
                                    "contains": {
                                        "properties": {"input_field": {"enum": [field]}},
                                        "required": ["input_field"],
                                    }
                                }
                            }
                        }
                    },
                }
            )
        data = {**data, "source_parameter_inventory": inventory}
        return data, schema

    @classmethod
    def from_extraction_payload(cls, payload):
        if "fixed_inputs" in payload:
            fixed = {item["field"]: item["value"] for item in payload["fixed_inputs"]}
            if len(fixed) != len(payload["fixed_inputs"]):
                raise ValueError("Duplicate fixed input field")
            payload = {**payload, "fixed_inputs": fixed}
        return cls.model_validate(payload)


class CriterionEvidence(Contract):
    criterion_id: str
    verdict: Literal["supported", "failed", "unknown"]
    evidence: list[str] = Field(
        description=prompt_text.CRITERION_EVIDENCE_EVIDENCE_DESCRIPTION
    )
    reason: str


class Verification(Contract):
    generalizable: bool = Field(
        description=prompt_text.VERIFICATION_GENERALIZABLE_DESCRIPTION
    )
    reason: str
    criteria: list[CriterionEvidence] = Field(
        description=prompt_text.VERIFICATION_CRITERIA_DESCRIPTION
    )


class Feedback(Contract):
    rejects_previous_result: bool
    evidence_quote: str
    reason: str
    accepted_criteria: list[str] = Field(default_factory=list)


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
    acceptance_evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
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
