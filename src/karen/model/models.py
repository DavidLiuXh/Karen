"""DeepSeek clients with observable inference settings and the engine model contract."""

import asyncio
from dataclasses import replace

from dynamic_graph.graph.schemas import SchemaError, validate_value
from dynamic_graph.models.adapters import LangChainModelClient
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings

from ..prompt.prompts import RESPONSE_INSTRUCTION
from .prompts import (
    FINAL_DELIVERY_PLANNING,
    LOCAL_EDIT_PLANNING,
    PLANNER_RESPONSE_WITHOUT_RECALL,
    WEB_TOOL_SELECTION,
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
        response = await self.model.generate(replace(
            request,
            system_instruction=(request.system_instruction + WEB_TOOL_SELECTION + FINAL_DELIVERY_PLANNING
                                + LOCAL_EDIT_PLANNING),
            input_data=data,
        ))
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
        if field.get("value_schema") == source and field.get("update_schema") != source:
            try:
                validate_value(source, initial["literal"])
            except (SchemaError, ValueError, KeyError, TypeError):
                continue
            field["update_schema"] = source
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
