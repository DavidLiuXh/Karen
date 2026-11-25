"""DeepSeek clients with observable inference settings and the engine model contract."""

import asyncio
import json
import re
from dataclasses import replace

from dynamic_graph.execution.privacy import REDACTED
from dynamic_graph.graph.schemas import SchemaError, compatible, strict_loads, validate_value
from dynamic_graph.graph.spec import Binding
from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.models.client import ModelCallError, ModelResponse
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings
from pydantic import TypeAdapter

from ..prompt.prompts import RESPONSE_INSTRUCTION
from .prompts import (
    FINAL_DELIVERY_PLANNING,
    WEB_TOOL_SELECTION,
    LOCAL_EDIT_PLANNING,
    PLANNER_BINDING_REPAIR,
    PLANNER_RESPONSE_WITHOUT_RECALL,
)

DEFAULT_OUTPUT_TOKEN_LIMIT = 32768


class DeepSeekModelClient(LangChainModelClient):
    def __init__(
        self, *, model: str = "deepseek-flash", mode: str = "json_mode",
        thinking=True, reasoning_effort="low",
    ):
        settings = (
            {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": reasoning_effort}
            if thinking
            else {"temperature": 0, "extra_body": {"thinking": {"type": "disabled"}}}
        )
        chat = ChatDeepSeek(model=model, max_retries=0, **settings)
        super().__init__(
            chat_model=chat, model=model, mode=mode, allow_json_mode=mode == "json_mode",
            max_output_tokens=DEFAULT_OUTPUT_TOKEN_LIMIT,
        )
        self.output_token_limit_source = "default"
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort if thinking else None

    async def generate(self, request):
        try:
            return await super().generate(request)
        except ModelCallError as error:
            raw = error.raw_response
            syntax = error.details.get("json_syntax", {})
            cause = error.__cause__
            extra_data = syntax.get("message") == "Extra data" or (
                isinstance(cause, json.JSONDecodeError) and cause.msg == "Extra data"
            )
            if (self.mode != "json_mode" or error.code != "MODEL_RESPONSE_INVALID"
                    or not extra_data or not isinstance(raw, str)):
                raise
            payload = _recover_json_object(raw)
            if payload is None:
                raise
            return ModelResponse(
                payload, error.usage, error.provider_request_id,
                {"model": self.model, "mode": self.mode, "json_recovery": "surplus_closing_delimiters"},
            )

    @property
    def metadata(self):
        return {
            **super().metadata,
            "output_token_limit_source": self.output_token_limit_source,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "effective_reasoning_effort": (
                "high" if self.reasoning_effort in {"medium", "xhigh"}
                else "low" if self.reasoning_effort == "minimal"
                else self.reasoning_effort
            ),
        }


def _recover_json_object(raw):
    raw = raw.lstrip()
    try:
        root, end = json.JSONDecoder().raw_decode(raw)
        suffix = raw[end:].translate(str.maketrans("", "", " \t\r\n"))
        if not isinstance(root, dict) or not suffix or set(suffix) - {"}", "]"}:
            return None
        payload = strict_loads(raw[:end])
        json.dumps(payload, allow_nan=False)
        return payload
    except ValueError:
        return None


def deepseek_client(
    *, model: str = "deepseek-flash", mode: str = "json_mode", thinking=True, reasoning_effort="low"
) -> DeepSeekModelClient:
    return DeepSeekModelClient(
        model=model, mode=mode, thinking=thinking, reasoning_effort=reasoning_effort
    )


class KarenPlannerModel:
    """Project the planner request while retaining the full goal in run records."""

    def __init__(self, model):
        self.model = model

    @property
    def metadata(self):
        return getattr(self.model, "metadata", {})

    async def generate(self, request):
        goal = request.input_data["goal"]
        context = {
            key: value for key, value in goal.get("context", {}).items()
            if key != "input_routing" and value not in (None, [], {})
        }
        memory = context.get("memory")
        if isinstance(memory, dict):
            memory = context["memory"] = {
                key: value for key, value in memory.items()
                if key != "snapshot_revision" and value not in (None, [], {})
            }
        history = memory.get("history") if isinstance(memory, dict) else None
        empty_recall = (
            isinstance(memory, dict)
            and memory.get("status") == "empty"
            and memory.keys() <= {"status", "history", "coverage"}
            and isinstance(history, dict)
            and history.get("status") == "none"
            and history.get("complete") is True
            and not history.get("messages")
        )
        # An exact user quote already in inputs needs no second copy in the dialogue.
        # Retain all turns when anything refers to their positions in the ledger.
        inputs = goal.get("inputs", {})
        conversation = context.get("conversation")
        if (
            (not memory or empty_recall)
            and not context.get("clarification_items")
            and (conversation is None or isinstance(conversation, list) and len(conversation) <= 1)
            and context.get("response_instruction") == RESPONSE_INSTRUCTION
        ):
            context["response_instruction"] = PLANNER_RESPONSE_WITHOUT_RECALL
        if isinstance(conversation, list) and len(conversation) == 1:
            message = conversation[0]
            if (isinstance(message, dict) and message.get("role") == "user"
                    and isinstance(message.get("content"), str)
                    and message["content"] in inputs.values()
                    and not context.get("clarification_items")):
                context.pop("conversation")
        data = {**request.input_data, "goal": {**goal, "context": context}}
        if "examples" in data:
            # The engine has already selected examples by authorized capability.
            # Keep their goals and graph responses, omitting selection metadata.
            data["examples"] = [
                {key: value for key, value in example.items()
                 if key not in {"case_name", "capability_keys"}}
                for example in data["examples"]
            ]
        if "allowed_capabilities" in data:
            data["allowed_capabilities"] = [
                {key: value for key, value in capability.items() if value is not None}
                for capability in data["allowed_capabilities"]
            ]
        backend_request = replace(
            request,
            system_instruction=(request.system_instruction + WEB_TOOL_SELECTION + FINAL_DELIVERY_PLANNING
                                + LOCAL_EDIT_PLANNING),
            input_data=data,
        )
        scope = _binding_repair_scope(data.get("repair"))
        if scope is None:
            response = await self.model.generate(backend_request)
        else:
            previous, index = scope
            bindings_schema = TypeAdapter(dict[str, Binding]).json_schema()
            definitions = bindings_schema.pop("$defs", {})
            response = await self.model.generate(replace(
                backend_request,
                task_instruction=PLANNER_BINDING_REPAIR,
                input_data={**data, "repair_node_index": index},
                output_schema={
                    "type": "object",
                    "properties": {"input_bindings": bindings_schema},
                    "$defs": definitions,
                    "required": ["input_bindings"],
                    "additionalProperties": False,
                },
            ))
            patch = response.payload
            if (not isinstance(patch, dict) or set(patch) != {"input_bindings"}
                    or not isinstance(patch["input_bindings"], dict)):
                raise ModelCallError(
                    "MODEL_RESPONSE_INVALID", "Planner returned an invalid binding repair",
                    retryable=True, usage=response.usage, provider_request_id=response.provider_request_id,
                )
            graph = previous["graph"]
            nodes = list(graph["nodes"])
            nodes[index] = {**nodes[index], "input_bindings": patch["input_bindings"]}
            response = replace(
                response, payload={**previous, "graph": {**graph, "nodes": nodes}},
                response_metadata={**response.response_metadata, "repair_scope": "node_input_bindings"},
            )
        payload = response.payload
        graph = payload.get("graph") if isinstance(payload, dict) else None
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
            return response

        # The catalog owns capability contracts. The engine still validates
        # authorization, bindings, writes, and the rest of the candidate graph.
        contracts = {
            (entry.get("kind"), entry.get("name"), entry.get("version")): entry
            for entry in data.get("allowed_capabilities", [])
            if "input_schema" in entry and "output_schema" in entry
        }
        nodes = []
        registered = set()
        for index, node in enumerate(graph["nodes"]):
            ref = node.get("capability") if isinstance(node, dict) else None
            if isinstance(ref, dict) and node.get("kind") in ("tool", "check"):
                name, version = ref.get("name"), ref.get("version")
                if isinstance(name, str) and isinstance(version, str):
                    contract = contracts.get((node["kind"], name, version))
                    if contract is not None:
                        registered.add(index)
                        node = {**node, "input_schema": contract["input_schema"],
                                "output_schema": contract["output_schema"]}
            nodes.append(node)
        fields = _repair_data_contracts(graph, nodes, registered)
        repaired = {**graph, "nodes": nodes}
        if fields is not None:
            repaired["state_fields"] = fields
        return replace(response, payload={**payload, "graph": repaired})


def _binding_repair_scope(repair):
    if not repair or repair.get("attempt") != 2 or not repair.get("remaining_rounds"):
        return None
    previous = repair.get("previous_response")
    errors = repair.get("validation_errors")
    if not isinstance(previous, dict) or previous.get("outcome") != "graph" or not errors:
        return None
    graph = previous.get("graph")
    if not isinstance(graph, dict) or REDACTED in json.dumps(graph):
        return None
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return None
    indices = set()
    for error in errors:
        code = error.get("code")
        path = error.get("path", "")
        if code == "MISSING_INPUT_BINDING":
            match = re.fullmatch(r"/nodes/([0-9]+)", path)
        elif code in {"INVALID_BINDING", "TYPE_COMPATIBILITY_UNPROVEN"}:
            match = re.fullmatch(r"/nodes/([0-9]+)/input_bindings(?:/.*)?", path)
        else:
            return None
        if match is None:
            return None
        indices.add(int(match[1]))
    if len(indices) == 1:
        index = indices.pop()
        if index < len(nodes) and isinstance(nodes[index], dict):
            return previous, index
    return None


def _close_objects(schema):
    if not isinstance(schema, dict):
        return schema
    closed = dict(schema)
    kind = schema.get("type")
    if kind == "object" or isinstance(kind, list) and "object" in kind:
        closed.setdefault("additionalProperties", False)
    for key in ("properties", "$defs"):
        if isinstance(schema.get(key), dict):
            closed[key] = {name: _close_objects(value) for name, value in schema[key].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            closed[key] = _close_objects(schema[key])
    return closed


def _repair_data_contracts(graph, nodes, registered):
    writers = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "llm":
            nodes[index] = {**node, **{
                key: _close_objects(node[key]) for key in ("input_schema", "output_schema") if key in node
            }}
        writes = node.get("writes")
        for write in writes if isinstance(writes, list) else ():
            if isinstance(write, dict) and isinstance(write.get("field"), str):
                writers.setdefault(write["field"], []).append((index, write.get("output_pointer")))
    fields = graph.get("state_fields")
    if not isinstance(fields, dict):
        return fields
    repaired = dict(fields)
    for name, field in fields.items():
        if not isinstance(field, dict):
            continue
        field = repaired[name] = {**field, **{
            key: _close_objects(field[key]) for key in ("value_schema", "update_schema") if key in field
        }}
        reducer, initial = field.get("reducer"), field.get("initial")
        sources = writers.get(name, ())
        if (not isinstance(reducer, dict) or reducer.get("name") != "builtin.replace"
                or reducer.get("version") != "1.0.0" or len(sources) != 1
                or not isinstance(initial, dict) or "literal" not in initial):
            continue
        index, pointer = sources[0]
        if index not in registered or pointer != "":
            continue
        source = nodes[index]["output_schema"]
        try:
            if not compatible(source, field["value_schema"]):
                continue
            validate_value(source, initial["literal"])
        except (SchemaError, ValueError, KeyError, TypeError):
            continue
        # The initial value and the only writer both satisfy the registered contract.
        field["value_schema"] = field["update_schema"] = source
    return repaired


async def configure_execution_output_limits(planner, worker) -> int:
    """Read the CLI backend's model catalog once for both execution clients."""
    if not isinstance(worker, DeepSeekModelClient):
        return DEFAULT_OUTPUT_TOKEN_LIMIT
    try:
        async with asyncio.timeout(5):
            catalog = await worker._chat.root_async_client.models.list()
    except Exception:
        # Provider exceptions may include credentials or request data. Keep the
        # existing budget if model metadata cannot be retrieved at startup.
        return DEFAULT_OUTPUT_TOKEN_LIMIT

    limits = {entry.id: (entry.model_extra or {}).get("max_output_tokens")
              for entry in catalog.data}
    for client in (planner, worker):
        limit = limits.get(client.model)
        if type(limit) is int and limit > 0:
            client.max_output_tokens = limit
            client.output_token_limit_source = "provider"
    # Each adapter clamps this shared engine request to its own model limit.
    return max(planner.max_output_tokens, worker.max_output_tokens)


def memory_embeddings() -> OllamaEmbeddings:
    """Installed BGE-M3 supports Chinese/English through the Embeddings contract."""
    return OllamaEmbeddings(model="bge-m3:latest")
