"""Environment settings for memory retrieval, reranking and context assembly."""

import os

from pydantic import BaseModel, ConfigDict, Field


class MemoryBudgets(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    vector_candidates_per_layer: int = Field(default=100, gt=0)
    bm25_candidates_per_layer: int = Field(default=100, gt=0)
    rerank_candidates_per_layer: int = Field(default=20, gt=0)
    rerank_max_records: int = Field(default=80, gt=0)
    rerank_input_bytes: int = Field(default=48 * 1024, gt=0)
    context_max_m1: int = Field(default=5, gt=0)
    context_max_m2: int = Field(default=8, gt=0)
    facts_context_max_m1: int = Field(default=20, gt=0)
    memory_bytes: int = Field(default=10 * 1024, gt=0)
    detail_max_sources: int = Field(default=12, gt=0)
    detail_max_tasks: int = Field(default=3, gt=0)
    detail_max_events: int = Field(default=200, gt=0)
    detail_read_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    detail_timeout_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    detail_memory_bytes: int = Field(default=6 * 1024, gt=0)
    detail_context_bytes: int = Field(default=11 * 1024, gt=0)
    context_bytes: int = Field(default=12 * 1024, gt=0)

    @classmethod
    def from_env(cls):
        values = {}
        for field in cls.model_fields:
            name = f"KAREN_MEMORY_{field.upper()}"
            if name in os.environ:
                values[field] = os.environ[name]
        return cls.model_validate(values)
