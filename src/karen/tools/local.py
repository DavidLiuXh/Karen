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


def file_edit_text_tool(root="/tmp"):
    """Apply a bounded edit to one uniquely identified span of UTF-8 text."""
    from dynamic_graph.tools import file_read_text_tool, file_write_text_tool

    reader = file_read_text_tool(root)
    writer = file_write_text_tool(root)

    async def edit(data, context):
        existing = await reader.handler({"path": data["path"]}, context)
        text, anchor = existing["content"], data["anchor"]
        start = text.find(anchor)
        if not anchor or start < 0 or text.find(anchor, start + 1) >= 0:
            raise ToolCallError("Edit anchor must occur exactly once", code="EDIT_ANCHOR_NOT_UNIQUE")
        end = start + len(anchor)
        position = data["position"]
        if position == "before":
            end = start
        elif position == "after":
            start = end
        updated = text[:start] + data["content"] + text[end:]
        return await writer.handler(
            {"path": existing["path"], "content": updated, "overwrite": True}, context,
        )

    return ToolDefinition(
        "file.edit_text",
        "1.0.0",
        "Atomically insert or replace a span in an existing UTF-8 file. Supply exact, unique "
        "anchor text from the file and only the new text. All other bytes are preserved. "
        "Both the original and updated file must fit the local 1 MiB limit.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "anchor": {"type": "string"},
                "position": {"type": "string", "enum": ["before", "after", "replace"]},
                "content": {"type": "string"},
            },
            "required": ["path", "anchor", "position", "content"],
            "additionalProperties": False,
        },
        writer.output_schema,
        edit,
        read_only=False,
        idempotent=False,
        reentrant=False,
    )
