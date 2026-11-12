"""Read bounded, contiguous UTF-8 excerpts from authorized local files."""

import asyncio
import codecs
import time
from pathlib import Path

from dynamic_graph import ToolDefinition
from dynamic_graph.execution.errors import ToolCallError

READ_RANGE_BYTES = 24_000


def file_read_text_range_tool(root="/tmp"):
    root = Path(root).resolve(strict=True)

    async def read(data, context):
        if context.cancellation_token.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= context.deadline:
            raise TimeoutError("File read deadline exceeded")
        offset = data.get("offset_bytes", 0)
        try:
            target = (root / data["path"]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError
            with target.open("rb") as stream:
                if offset > stream.seek(0, 2):
                    raise ValueError
                stream.seek(offset)
                raw = stream.read(READ_RANGE_BYTES + 1)
            more = len(raw) > READ_RANGE_BYTES
            decoder = codecs.getincrementaldecoder("utf-8")()
            content = decoder.decode(raw[:READ_RANGE_BYTES], final=not more)
        except (OSError, ValueError, RuntimeError):
            raise ToolCallError("Cannot read a UTF-8 file range at the supplied byte offset") from None
        next_offset = offset + len(content.encode("utf-8"))
        return {
            "path": str(target),
            "offset_bytes": offset,
            "content": content,
            "next_offset_bytes": next_offset if more else None,
        }

    properties = {
        "path": {"type": "string"},
        "offset_bytes": {"type": "integer", "minimum": 0},
    }
    output = {
        **properties,
        "content": {"type": "string"},
        "next_offset_bytes": {"type": ["integer", "null"], "minimum": 0},
    }
    return ToolDefinition(
        name="file.read_text_range",
        version="1.0.0",
        description=(
            f"Read at most {READ_RANGE_BYTES} UTF-8 bytes from a file under {root}. "
            "Prefer this for large files before passing text to an LLM. "
            "Omit offset_bytes to start at zero. Use next_offset_bytes for the next read; "
            "null means end of file. Process ranges separately if the entire file is required."
        ),
        input_schema={
            "type": "object", "properties": properties, "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object", "properties": output, "required": list(output),
            "additionalProperties": False,
        },
        handler=read,
        read_only=True,
    )
