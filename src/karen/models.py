"""DeepSeek clients with observable inference settings and the engine model contract."""

from dynamic_graph.models.adapters import LangChainModelClient
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings


class DeepSeekModelClient(LangChainModelClient):
    def __init__(self, *, model: str = "deepseek-flash", mode: str = "json_mode", thinking=True):
        settings = (
            {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "low"}
            if thinking
            else {"temperature": 0, "extra_body": {"thinking": {"type": "disabled"}}}
        )
        chat = ChatDeepSeek(model=model, max_retries=0, **settings)
        super().__init__(
            chat_model=chat, model=model, mode=mode, allow_json_mode=mode == "json_mode"
        )
        self.thinking = thinking

    @property
    def metadata(self):
        return {
            **super().metadata,
            "thinking": self.thinking,
            "reasoning_effort": "low" if self.thinking else None,
        }


def deepseek_client(
    *, model: str = "deepseek-flash", mode: str = "json_mode", thinking=True
) -> DeepSeekModelClient:
    return DeepSeekModelClient(model=model, mode=mode, thinking=thinking)


def memory_embeddings() -> OllamaEmbeddings:
    """Installed BGE-M3 supports Chinese/English through the Embeddings contract."""
    return OllamaEmbeddings(model="bge-m3:latest")
