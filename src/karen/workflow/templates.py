"""Restricted parameter promotion; no executable templates or arbitrary graph patches."""

import copy
import re
from datetime import UTC
from zoneinfo import ZoneInfo

from dynamic_graph import GoalSpec, GraphSpec
from dynamic_graph.graph.schemas import SchemaSpec, compatible, validate_schema, validate_value


def pointer(document, value):
    if value == "":
        return document
    if not value.startswith("/"):
        raise ValueError("invalid_pointer")
    for token in value[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


def parameterized_graph(goal, graph, proposal):
    if "workflow_parameters" in goal.inputs:
        raise ValueError("reserved_input_field")
    document = graph.document()
    names, fields, sites = set(), {}, set()
    nodes = {n["id"]: n for n in document["nodes"]}
    for parameter in proposal.parameters:
        if parameter.name in names or (parameter.input_field and parameter.input_field in fields):
            raise ValueError("duplicate_parameter")
        names.add(parameter.name)
        schema = SchemaSpec.model_validate(parameter.schema_)
        validate_schema(schema)
        validate_value(schema, parameter.source_value)
        if not parameter.input_field and not parameter.sites:
            raise ValueError("unbound_parameter")
        if parameter.input_field:
            if goal.inputs.get(parameter.input_field) != parameter.source_value:
                raise ValueError("source_input_mismatch")
            original_schema = goal.input_schema.properties[parameter.input_field]
            if not compatible(schema, original_schema):
                raise ValueError("parameter_schema_broader_than_source")
            fields[parameter.input_field] = parameter.name
        for site in parameter.sites:
            key = (site.kind, site.owner, site.binding)
            if key in sites:
                raise ValueError("duplicate_binding")
            sites.add(key)
            binding = (
                nodes[site.owner]["input_bindings"][site.binding]
                if site.kind == "node"
                else document["state_fields"][site.owner]["initial"]
            )
            target_schema = (
                graph.state_fields[site.owner].value_schema
                if site.kind == "state"
                else next(n for n in graph.nodes if n.id == site.owner).input_schema.properties[
                    site.binding
                ]
            )
            if not compatible(schema, target_schema):
                raise ValueError("parameter_schema_incompatible")
            input_pointer = (
                "/" + parameter.input_field.replace("~", "~0").replace("/", "~1")
                if parameter.input_field
                else None
            )
            if parameter.input_field and binding == {"source": "input", "pointer": input_pointer}:
                continue  # Already bound to the declared whole input; translated below.
            if binding != {"literal": parameter.source_value}:
                raise ValueError("source_literal_mismatch")
            binding.clear()
            binding.update(source="input", pointer="/workflow_parameters/" + parameter.name)
    if set(fields) != set(goal.inputs):
        raise ValueError("unparameterized_inputs")

    def translate(value):
        if isinstance(value, dict):
            if value.get("source") == "input" and not value.get("pointer", "").startswith(
                "/workflow_parameters/"
            ):
                path = value["pointer"]
                tokens = path.split("/")
                if len(tokens) < 2:
                    raise ValueError("whole_input_reference")
                field = tokens[1].replace("~1", "/").replace("~0", "~")
                value["pointer"] = (
                    "/workflow_parameters/"
                    + fields[field]
                    + ("/" + "/".join(tokens[2:]) if len(tokens) > 2 else "")
                )
            else:
                for item in value.values():
                    translate(item)
        elif isinstance(value, list):
            for item in value:
                translate(item)

    # Only translate binding objects, never arbitrary literals/schema descriptions.
    for node in document["nodes"]:
        for binding in node["input_bindings"].values():
            if "source" in binding:
                translate(binding)
    for state in document["state_fields"].values():
        if "source" in state["initial"]:
            translate(state["initial"])
    for binding in document["outputs"].values():
        translate(binding)
    for node in graph.nodes:
        if node.instruction:
            for parameter in proposal.parameters:
                if (
                    isinstance(parameter.source_value, str)
                    and parameter.source_value
                    and parameter.source_value in node.instruction
                ):
                    raise ValueError("variable_in_instruction")
    return GraphSpec.model_validate(document)


def parameter_input_schema(proposal):
    """The same input contract is used in verification and actual instantiation."""
    return {
        "type": "object",
        "properties": {
            "workflow_parameters": {
                "type": "object",
                "properties": {p.name: p.schema_.document() for p in proposal.parameters},
                "required": [p.name for p in proposal.parameters],
                "additionalProperties": False,
            }
        },
        "required": ["workflow_parameters"],
        "additionalProperties": False,
    }


def instantiate(definition, request, parameters):
    parameters = copy.deepcopy(parameters)
    allowed = {p.name for p in definition.proposal.parameters}
    if set(parameters) - allowed:
        raise ValueError("unknown_parameter")
    missing = []
    for parameter in definition.proposal.parameters:
        if parameter.name not in parameters and parameter.default_rule == "today":
            parameters[parameter.name] = (
                request.received_at.astimezone(ZoneInfo(request.timezone)).date().isoformat()
            )
        if parameter.name not in parameters:
            missing.append(parameter.name)
        else:
            validate_value(SchemaSpec.model_validate(parameter.schema_), parameters[parameter.name])
    if missing:
        return None, None, parameters, missing
    graph = parameterized_graph(
        definition.goal_template, definition.graph_spec, definition.proposal
    )
    old_strings = {}
    for parameter in definition.proposal.parameters:
        old = parameter.source_value
        if isinstance(old, str) and old:
            if old in old_strings and old_strings[old] != str(parameters[parameter.name]):
                raise ValueError("ambiguous_text_parameter")
            old_strings[old] = str(parameters[parameter.name])

    def render(text):
        if not old_strings:
            return text
        pattern = "|".join(re.escape(v) for v in sorted(old_strings, key=len, reverse=True))
        return re.sub(pattern, lambda m: old_strings[m.group()], text)

    goal = GoalSpec(
        request_id=request.request_id,
        objective=render(definition.goal_template.objective),
        success_criteria=[
            {"id": c.id, "description": render(c.description)}
            for c in definition.goal_template.success_criteria
        ],
        inputs={"workflow_parameters": parameters},
        input_schema=parameter_input_schema(definition.proposal),
        output_schema=definition.goal_template.output_schema,
        context={
            "timezone": request.timezone,
            "reference_time_utc": request.received_at.astimezone(UTC).isoformat(),
            "workflow": {"id": definition.workflow_id, "version": definition.version},
        },
    )
    return goal, graph, parameters, []


def acceptance_supported(goal, verification, evidence):
    criteria = {c.id for c in goal.success_criteria}
    checked = {c.criterion_id for c in verification.criteria}
    if not criteria or checked != criteria or len(checked) != len(verification.criteria):
        return False
    for check in verification.criteria:
        if check.verdict != "supported" or not check.evidence:
            return False
        for reference in check.evidence:
            try:
                if not reference.startswith("/evidence/"):
                    return False
                value = pointer({"evidence": evidence}, reference)
                if value is None or value == "" or value == []:
                    return False
            except (ValueError, KeyError, IndexError, TypeError):
                return False
    return True
