"""Bind LangChain's DeepSeek transport to the engine's existing model interface."""

from dynamic_graph.models.adapters import LangChainModelClient
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings


def deepseek_client(*, model: str = "deepseek-chat") -> LangChainModelClient:
    chat = ChatDeepSeek(model=model, temperature=0, max_retries=0)
    return LangChainModelClient(chat_model=chat, model=model, mode="function_calling")


def memory_embeddings() -> OllamaEmbeddings:
    """Installed BGE-M3 supports Chinese/English through the Embeddings contract."""
    return OllamaEmbeddings(model="bge-m3:latest")
