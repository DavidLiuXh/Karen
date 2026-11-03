"""In-memory multi-task scheduling and channel-neutral conversation commands."""

from .interaction import TaskInteraction
from .manager import DEFAULT_MAX_CONCURRENT_TASKS, TaskManager

__all__ = ["DEFAULT_MAX_CONCURRENT_TASKS", "TaskManager", "TaskInteraction"]
