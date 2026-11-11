"""Directory creation for Karen's bounded local file tools."""

import asyncio
import os
import time
from pathlib import Path

from dynamic_graph import ToolDefinition
from dynamic_graph.execution.errors import ToolCallError


def file_create_directory_tool(root="/tmp"):
    root = Path(root).resolve(strict=True)

    async def create(data, context):
        try:
            target = (root / data["path"]).resolve()
            parts = target.relative_to(root).parts
            if not parts:
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            raise ToolCallError("Path is outside the permitted directory") from None

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory = os.open(root, flags)
            try:
                for part in parts:
                    if context.cancellation_token.cancelled:
                        raise asyncio.CancelledError
                    if time.monotonic() >= context.deadline:
                        raise TimeoutError("Local operation deadline exceeded")
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    # Refuse files and links swapped in after path validation.
                    child = os.open(part, flags, dir_fd=directory)
                    os.close(directory)
                    directory = child
            finally:
                os.close(directory)
        except TimeoutError:
            raise
        except OSError:
            raise ToolCallError("Cannot create local directory") from None
        return {"path": str(target)}

    path_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    return ToolDefinition(
        "file.create_directory",
        "1.0.0",
        f"Create a directory and missing parents under {root}. Existing directories are accepted. "
        "This is a side effect. For a new output directory, make subsequent file.write_text nodes "
        "depend on this node so their parent directories exist.",
        path_schema,
        path_schema,
        create,
        read_only=False,
        idempotent=True,
        reentrant=False,
    )
