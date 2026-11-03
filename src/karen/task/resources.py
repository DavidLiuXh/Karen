"""Run-scoped file leases for the explicitly registered local tools."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from dynamic_graph.execution.errors import ToolCallError

from ..observability import Observer


class ExecutionResources:
    def __init__(self, observer=None):
        self.observer = observer or Observer()
        self._current = ContextVar("execution_resource_owner", default=None)
        self._changed = asyncio.Condition()
        self._runs = {}
        self._owners = {}
        self._waiting = {}
        self._calls = {}
        self._managed = set()

    @asynccontextmanager
    async def run(self, capabilities):
        owner = uuid4().hex
        exclusive = any(
            c.kind in {"tool", "check"} and f"{c.name}@{c.version}" not in self._managed
            for c in capabilities
        )
        async with self._changed:
            while self._runs and (exclusive or any(self._runs.values())):
                await self._changed.wait()
            self._runs[owner] = exclusive
        context = self._current.set(owner)
        self.observer.emit("execution.resources", data={"owner": owner, "exclusive": exclusive})
        try:
            yield
        finally:
            async with self._changed:
                for path in list(self._owners):
                    self._owners[path].pop(owner, None)
                    if not self._owners[path]:
                        del self._owners[path]
                        self._calls.pop(path, None)
                self._runs.pop(owner)
                self._changed.notify_all()
            self._current.reset(context)

    def _cycle(self, owner, blockers):
        pending, seen = list(blockers), set()
        while pending:
            other = pending.pop()
            if other == owner:
                return True
            if other in seen:
                continue
            seen.add(other)
            for (waiter, _), dependencies in self._waiting.items():
                if waiter == other:
                    pending.extend(dependencies)
        return False

    async def _acquire(self, owner, path, write, context):
        key = (owner, object())
        async with self._changed:
            try:
                while True:
                    holders = self._owners.get(path, {})
                    blockers = {
                        other
                        for other, writing in holders.items()
                        if other != owner and (write or writing)
                    }
                    if not blockers:
                        self._owners.setdefault(path, {})[owner] = write or holders.get(
                            owner, False
                        )
                        return
                    if self._cycle(owner, blockers):
                        raise ToolCallError("RESOURCE_WAIT_CYCLE")
                    self._waiting[key] = blockers
                    self.observer.emit(
                        "execution.resource_wait",
                        data={
                            "run_id": context.run_id,
                            "node_id": context.node_id,
                            "resource": path,
                            "write": write,
                        },
                    )
                    await self._changed.wait()
            finally:
                self._waiting.pop(key, None)

    def independent_read(self, definition):
        """Declare an audited remote read that does not touch tracked local files."""
        if not definition.read_only:
            raise ValueError("Independent reads must be read-only")
        self._managed.add(f"{definition.name}@{definition.version}")
        return definition

    def file_tool(self, definition, *, root="/tmp", write=False):
        """Wrap a known path-based handler; do not infer effects from model output."""
        root = Path(root).resolve(strict=True)
        original = definition.handler
        self._managed.add(f"{definition.name}@{definition.version}")

        async def guarded(data, context):
            owner = self._current.get()
            if owner is None:
                raise ToolCallError("RESOURCE_CONTEXT_REQUIRED")
            try:
                target = (root / data["path"]).resolve()
                if not target.is_relative_to(root) or target == root:
                    raise ValueError
            except (ValueError, TypeError, OSError, RuntimeError):
                raise ToolCallError("RESOURCE_PATH_INVALID") from None
            path = str(target)
            await self._acquire(owner, path, write, context)
            lock = self._calls.setdefault(path, asyncio.Lock())
            async with lock:
                if context.cancellation_token.cancelled:
                    raise asyncio.CancelledError
                # Pass the canonical path checked by the coordinator to the real tool.
                work = asyncio.create_task(original({**data, "path": path}, context))
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    # A file/snapshot thread may still be writing. Keep its lease
                    # until the issued handler has actually stopped.
                    await asyncio.gather(work, return_exceptions=True)
                    raise

        # Per-file call serialization replaces the tool-wide registry mutex.
        return replace(definition, handler=guarded, reentrant=True)
