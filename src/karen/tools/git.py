"""Clone public repositories into the local tools' permitted directory."""

import asyncio
import os
import signal
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from dynamic_graph import ToolDefinition
from dynamic_graph.execution.errors import ToolCallError


def git_clone_tool(root="/tmp"):
    root = Path(root).resolve(strict=True)

    async def clone(data, context):
        url = data["url"]
        try:
            remote = urlsplit(url)
            if (
                remote.scheme != "https"
                or not remote.hostname
                or remote.username is not None
                or remote.password is not None
                or remote.query
                or remote.fragment
                or not remote.path.strip("/")
                or any(c.isspace() or ord(c) < 32 for c in url)
            ):
                raise ValueError
        except ValueError:
            raise ToolCallError(
                "A public HTTPS repository URL is required", code="GIT_URL_INVALID"
            ) from None

        try:
            target = (root / data["path"]).resolve()
            if target == root or not target.is_relative_to(root):
                raise ValueError
        except (ValueError, OSError, RuntimeError):
            raise ToolCallError(
                "Destination is outside the permitted directory", code="GIT_PATH_INVALID"
            ) from None
        if not target.parent.is_dir():
            raise ToolCallError(
                "Destination parent directory must exist", code="GIT_PARENT_MISSING"
            )
        if target.exists():
            raise ToolCallError("Destination already exists", code="GIT_DESTINATION_EXISTS")
        if context.cancellation_token.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= context.deadline:
            raise TimeoutError("Git clone deadline exceeded")

        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            GIT_ALLOW_PROTOCOL="https",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="/usr/bin/false",
        )
        try:
            with tempfile.TemporaryDirectory(prefix=".karen-git-", dir=target.parent) as temporary:
                checkout = Path(temporary) / "checkout"
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "-c",
                    "credential.helper=",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "clone",
                    "--depth=1",
                    "--no-recurse-submodules",
                    "--template=",
                    "--",
                    url,
                    str(checkout),
                    cwd=temporary,
                    env=env,
                    start_new_session=True,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    async with asyncio.timeout_at(context.deadline):
                        code = await process.wait()
                except (TimeoutError, asyncio.CancelledError):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    raise
                if code != 0:
                    raise ToolCallError(
                        "Git clone failed", code="GIT_CLONE_FAILED", details={"exit_code": code}
                    )
                if context.cancellation_token.cancelled:
                    raise asyncio.CancelledError
                if time.monotonic() >= context.deadline:
                    raise TimeoutError("Git clone deadline exceeded")
                # Reserve the destination first, so a concurrent creator is never overwritten.
                target.mkdir(mode=0o700)
                try:
                    checkout.rename(target)
                except OSError:
                    target.rmdir()
                    raise
        except FileExistsError:
            raise ToolCallError(
                "Destination already exists", code="GIT_DESTINATION_EXISTS"
            ) from None
        except FileNotFoundError:
            raise ToolCallError(
                "Git executable or destination parent unavailable", code="GIT_UNAVAILABLE"
            ) from None
        except TimeoutError:
            raise
        except OSError:
            raise ToolCallError(
                "Cannot create the repository checkout", code="GIT_WRITE_FAILED"
            ) from None
        return {"path": str(target)}

    return ToolDefinition(
        "git.clone",
        "1.0.0",
        f"Shallow-clone a public HTTPS Git repository into a new path under {root}. "
        "The destination parent must exist; use file.create_directory first when needed. "
        "Fetches the default branch and skips submodules.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}, "path": {"type": "string"}},
            "required": ["url", "path"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        clone,
        read_only=False,
        idempotent=False,
        reentrant=False,
    )
