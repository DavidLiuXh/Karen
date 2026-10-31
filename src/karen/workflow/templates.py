"""Restricted parameter promotion; no executable templates or arbitrary graph patches."""

import copy
import re
from datetime import UTC
from zoneinfo import ZoneInfo

from dynamic_graph import GoalSpec, GraphSpec
from dynamic_graph.graph.schemas import (
    SchemaSpec,
    canonical,
    compatible,
    infer_schema,
    validate_schema,
    validate_value,
)

from . import prompts as prompt_text


def pointer(document, value):
    if value == "":
        return document
    if not value.startswith("/"):
        raise ValueError("invalid_pointer")
    for token in value[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(document, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("invalid_array_pointer")
            document = document[int(token)]
        else:
            document = document[token]
    return document


def source_text_slots(document, value):
    """Enumerate exact source locations; the model selects rather than guesses."""
    if not isinstance(value, str) or not value:
        return []
    targets = []
    for node in document["nodes"]:
        if node["kind"] == "llm":
            targets.append(("instruction", node["id"], "", node["instruction"]))
        for name, binding in node["input_bindings"].items():
            if isinstance(binding.get("literal"), str):
                targets.append(("node_literal", node["id"], name, binding["literal"]))
    for name, state in document["state_fields"].items():
        if isinstance(state["initial"].get("literal"), str):
            targets.append(("state_literal", name, "", state["initial"]["literal"]))
    return [
        {"kind": kind, "owner": owner, "binding": binding, "occurrence": index}
        for kind, owner, binding, text in targets
        for index, _ in enumerate(re.finditer(re.escape(value), text))
    ]


def source_binding_sites(document, value, input_field=None):
    reference = (
        {"source": "input", "pointer": "/" + input_field.replace("~", "~0").replace("/", "~1")}
        if input_field
        else None
    )
    sites = []
    for node in document["nodes"]:
        for field, binding in node["input_bindings"].items():
            if canonical(binding) == canonical({"literal": value}) or (
                reference is not None and binding == reference
            ):
                sites.append({"kind": "node", "owner": node["id"], "binding": field})
    for field, state in document["state_fields"].items():
        if canonical(state["initial"]) == canonical({"literal": value}) or (
            reference is not None and state["initial"] == reference
        ):
            sites.append({"kind": "state", "owner": field, "binding": ""})
    return sites


def parameterized_graph(goal, graph, proposal, parameters=None):
    if "workflow_parameters" in goal.inputs:
        raise ValueError("reserved_input_field")
    document = graph.document()
    values = (
        parameters
        if parameters is not None
        else {p.name: p.source_value for p in proposal.parameters}
    )
    names, fields, sites, used = set(), {}, set(), set()
    for field, value in proposal.fixed_inputs.items():
        if field not in goal.inputs or canonical(goal.inputs[field]) != canonical(value):
            raise ValueError("fixed_input_mismatch")
    text_slots = {}

    nodes = {n["id"]: n for n in document["nodes"]}
    for parameter in proposal.parameters:
        if parameter.name in names or (parameter.input_field and parameter.input_field in fields):
            raise ValueError("duplicate_parameter")
        names.add(parameter.name)
        schema = SchemaSpec.model_validate(parameter.schema_)
        validate_schema(schema)
        validate_value(schema, parameter.source_value)
        validate_value(schema, values[parameter.name])
        if not parameter.input_field and not parameter.sites and not parameter.text_slots:
            raise ValueError("unbound_parameter")
        if parameter.input_field:
            if parameter.input_field in proposal.fixed_inputs:
                raise ValueError("fixed_variable_overlap")
            if parameter.input_field not in goal.inputs or canonical(
                goal.inputs[parameter.input_field]
            ) != canonical(parameter.source_value):
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
            if canonical(binding) != canonical({"literal": parameter.source_value}):
                raise ValueError("source_literal_mismatch")
            used.add(parameter.name)
            binding.clear()
            binding.update(source="input", pointer="/workflow_parameters/" + parameter.name)
        available_slots = source_text_slots(graph.document(), parameter.source_value)
        for slot in parameter.text_slots:
            if slot.model_dump(exclude={"role", "reason"}) not in available_slots:
                raise ValueError("text_slot_source_mismatch")
            if not isinstance(parameter.source_value, str) or not parameter.source_value:
                raise ValueError("text_slot_requires_string")
            key = (slot.kind, slot.owner, slot.binding)
            text_slots.setdefault(key, []).append((parameter, slot))
    if set(fields) | set(proposal.fixed_inputs) != set(goal.inputs):
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
                if field in proposal.fixed_inputs:
                    return
                used.add(fields[field])
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
    covered = set()
    for (kind, owner, binding_key), slots in text_slots.items():
        if kind == "instruction":
            node = nodes[owner]
            if node["kind"] != "llm" or binding_key:
                raise ValueError("invalid_instruction_slot")
            original = node["instruction"]
        else:
            target = (
                nodes[owner]["input_bindings"][binding_key]
                if kind == "node_literal"
                else document["state_fields"][owner]["initial"]
            )
            if set(target) != {"literal"} or not isinstance(target["literal"], str):
                raise ValueError("invalid_literal_text_slot")
            original = target["literal"]
        spans = []
        for parameter, slot in slots:
            occurrence = slot.occurrence
            matches = list(re.finditer(re.escape(parameter.source_value), original))
            if occurrence >= len(matches):
                raise ValueError("text_slot_source_mismatch")
            match = matches[occurrence]
            spans.append((match.start(), match.end(), parameter, slot))
            if slot.role == "parameter":
                used.add(parameter.name)
            if kind == "instruction":
                covered.add((owner, parameter.name, occurrence))
        spans.sort(key=lambda item: item[0])
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            raise ValueError("overlapping_text_slots")
        rendered, offset = [], 0
        for start, end, parameter, slot in spans:
            rendered.append(original[offset:start])
            if slot.role == "invariant":
                rendered.append(original[start:end])
            elif kind == "instruction":
                field = "_workflow_parameter_" + parameter.name
                source_node = next(n for n in graph.nodes if n.id == owner)
                if (
                    field in source_node.input_bindings
                    or "{{input._workflow_parameter_" in source_node.instruction
                ):
                    raise ValueError("reserved_parameter_field")
                if field not in node["input_bindings"]:
                    # Never replace a source binding or extend tool contracts.
                    node["input_bindings"][field] = {
                        "source": "input",
                        "pointer": "/workflow_parameters/" + parameter.name,
                    }
                    node["input_schema"].setdefault("properties", {})[field] = (
                        parameter.schema_.document()
                    )
                    node["input_schema"].setdefault("required", []).append(field)
                rendered.append("{{input." + field + "}}")
            else:
                if not isinstance(values[parameter.name], str):
                    raise ValueError("text_slot_requires_string_value")
                rendered.append(values[parameter.name])
            offset = end
        rendered.append(original[offset:])
        if kind == "instruction":
            node["instruction"] = "".join(rendered)
            if any(slot.role == "parameter" for _, slot in slots):
                node["instruction"] += (
                    prompt_text.PARAMETER_REFERENCE_INSTRUCTION
                )
        else:
            target["literal"] = "".join(rendered)
    for node in graph.nodes:
        if node.instruction:
            for parameter in proposal.parameters:
                if isinstance(parameter.source_value, str) and parameter.source_value:
                    for occurrence, _ in enumerate(
                        re.finditer(re.escape(parameter.source_value), node.instruction)
                    ):
                        if (node.id, parameter.name, occurrence) not in covered:
                            raise ValueError("variable_in_instruction")
    if used != names:
        raise ValueError("unused_parameter:" + ",".join(sorted(names - used)))
    return GraphSpec.model_validate(document)


def parameter_input_schema(proposal):
    """The same input contract is used in verification and actual instantiation."""
    return {
        "type": "object",
        "properties": {
            **{
                name: {**infer_schema(value), "enum": [value]}
                for name, value in proposal.fixed_inputs.items()
            },
            "workflow_parameters": {
                "type": "object",
                "properties": {p.name: p.schema_.document() for p in proposal.parameters},
                "required": [p.name for p in proposal.parameters],
                "additionalProperties": False,
            },
        },
        "required": [*proposal.fixed_inputs, "workflow_parameters"],
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
        definition.goal_template, definition.graph_spec, definition.proposal, parameters
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
        inputs={**definition.proposal.fixed_inputs, "workflow_parameters": parameters},
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
                if reference.startswith("/evidence/user_feedback/"):
                    tokens = reference.split("/")
                    if (
                        len(tokens) != 5
                        or tokens[4] != "quote"
                        or not re.fullmatch(r"0|[1-9][0-9]*", tokens[3])
                    ):
                        return False
                    feedback = evidence["user_feedback"][int(tokens[3])]
                    if feedback.get(
                        "source_role"
                    ) != "user" or check.criterion_id not in feedback.get("criterion_ids", []):
                        return False
                value = pointer({"evidence": evidence}, reference)
                if value is None or value == "" or value == []:
                    return False
            except (ValueError, KeyError, IndexError, TypeError):
                return False
    return True
