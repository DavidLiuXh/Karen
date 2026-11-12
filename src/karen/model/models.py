"""DeepSeek clients with observable inference settings and the engine model contract."""

import asyncio
import json

from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.models.client import ModelCallError
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings

DEFAULT_OUTPUT_TOKEN_LIMIT = 32768
MAX_MODEL_TEXT_BYTES = 48 * 1024
MAX_WORKER_REQUEST_BYTES = 64 * 1024


def _check_model_input(request):
    payload = {
        "system_instruction": request.system_instruction,
        "task_instruction": request.task_instruction,
        "input_data": request.input_data,
        "output_schema": request.output_schema,
    }
    pending = list(payload.values())
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
            if size > MAX_MODEL_TEXT_BYTES:
                raise ModelCallError(
                    "MODEL_INPUT_TOO_LARGE", "Model input contains oversized text",
                    usage={"input_tokens": 0, "output_tokens": 0},
                    details={"input_bytes": size, "limit_bytes": MAX_MODEL_TEXT_BYTES},
                )
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if request.role == "worker":
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_WORKER_REQUEST_BYTES:
            raise ModelCallError(
                "MODEL_INPUT_TOO_LARGE", "Worker request exceeds the input budget",
                usage={"input_tokens": 0, "output_tokens": 0},
                details={"input_bytes": size, "limit_bytes": MAX_WORKER_REQUEST_BYTES},
            )



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
        _check_model_input(request)
        return await super().generate(request)

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
