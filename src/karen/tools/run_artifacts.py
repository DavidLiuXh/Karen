"""Read recorded outputs of a finished run without exposing other run metadata."""

import asyncio
import json
import re
import time
from pathlib import Path

from dynamic_graph import ToolDefinition
from dynamic_graph.execution.errors import ToolCallError


def run_artifacts_tool(runs_dir):
    root = Path(runs_dir).resolve()

    async def read(data, context):
        if context.cancellation_token.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= context.deadline:
            raise TimeoutError("Artifact read deadline exceeded")
        try:
            directory = Path(data["path"])
            if not directory.is_absolute():
                directory = root / directory
            run = directory.parent
            if (directory.name != "artifacts" or not re.fullmatch(r"[a-f0-9]{32}", run.name)
                    or run.parent.resolve() != root or run.is_symlink() or directory.is_symlink()):
                raise ValueError
            manifest_path = run / "manifest.json"
            events_path = run / "events.jsonl"
            if manifest_path.is_symlink() or events_path.is_symlink():
                raise ValueError
            if manifest_path.stat().st_size > 64 * 1024 or events_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError
            if json.loads(manifest_path.read_text())["terminal"] is not True:
                raise ValueError
            names = set()
            for line in events_path.read_text().splitlines():
                event = json.loads(line)
                ref = event.get("payload_ref")
                if event.get("event_type") == "node_returned" and isinstance(ref, str):
                    if re.fullmatch(r"artifacts/[A-Za-z0-9_.-]+\.json", ref):
                        names.add(ref.split("/")[1])
            if len(names) > 64:
                raise ToolCallError("Too many artifacts; maximum 64 recorded files")
            files, used = [], 0
            for name in sorted(names):
                if context.cancellation_token.cancelled:
                    raise asyncio.CancelledError
                if time.monotonic() >= context.deadline:
                    raise TimeoutError("Artifact read deadline exceeded")
                path = directory / name
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError
                with path.open("rb") as stream:
                    content = stream.read(1_048_576 - used + 1)
                used += len(content)
                if used > 1_048_576:
                    raise ToolCallError("Recorded artifacts exceed total byte limit (1048576)")
                files.append({"name": name, "path": str(path.resolve()),
                              "content": content.decode("utf-8")})
            return {"files": files, "complete": True}
        except (OSError, ValueError, KeyError, TypeError):
            raise ToolCallError("Cannot read recorded artifacts of a finished run") from None

    return ToolDefinition(
        "runs.read_artifacts", "1.0.0",
        f"Enumerate and read recorded JSON node outputs of a finished run under {root}. "
        "path must be its artifacts directory (absolute, or <run_id>/artifacts). "
        "Use this for previous-run results outside file.read_text's permitted directory. "
        "Includes actual filenames, so no glob tool is needed. Maximum 64 files and 1048576 total bytes. "
        "Read-only; excludes goal, policy, prompts and unrecorded files.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"], "additionalProperties": False},
        {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "object", "properties": {
                key: {"type": "string"} for key in ("name", "path", "content")},
                "required": ["name", "path", "content"], "additionalProperties": False}},
            "complete": {"type": "boolean"}},
         "required": ["files", "complete"], "additionalProperties": False},
        read,
    )
