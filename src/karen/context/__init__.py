"""Independent context memory module; lifecycle belongs to its caller."""

from .contracts import (
    ContextEvent,
    DetailQuery,
    DetailSearchResult,
    MemoryFlushError,
    MemoryQueueFull,
    PersistenceError,
    RecallQuery,
    RecallResult,
    SourceRef,
    TimeRange,
    WriteReceipt,
    WriteStatus,
)
from .service import ContextMemory

__all__ = [
    "ContextEvent",
    "ContextMemory",
    "DetailQuery",
    "DetailSearchResult",
    "MemoryFlushError",
    "MemoryQueueFull",
    "PersistenceError",
    "RecallQuery",
    "RecallResult",
    "SourceRef",
    "TimeRange",
    "WriteReceipt",
    "WriteStatus",
]
