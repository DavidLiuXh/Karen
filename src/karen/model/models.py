"""DeepSeek clients with observable inference settings and the engine model contract."""

import asyncio
from dataclasses import replace

from dynamic_graph.models.adapters import LangChainModelClient
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings

from ..prompt.prompts import RESPONSE_INSTRUCTION
from .prompts import FINAL_DELIVERY_PLANNING, PLANNER_RESPONSE_WITHOUT_RECALL

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
        context = goal.get("context", {})
        memory = context.get("memory")
        if isinstance(memory, dict):
            memory = {
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
        conversation = context.get("conversation")
        if (
            (not memory or empty_recall)
            and not context.get("clarification_items")
            and (conversation is None or isinstance(conversation, list) and len(conversation) <= 1)
            and context.get("response_instruction") == RESPONSE_INSTRUCTION
        ):
            context = {**context, "response_instruction": PLANNER_RESPONSE_WITHOUT_RECALL}
            request = replace(request, input_data={**request.input_data, "goal": {**goal, "context": context}})
        return await self.model.generate(replace(
            request, system_instruction=request.system_instruction + FINAL_DELIVERY_PLANNING
        ))


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
