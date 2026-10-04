"""Bounded, best-effort event capture with task-local causal spans."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..privacy import redact

LOG = logging.getLogger(__name__)


def utcnow():
    return datetime.now(UTC).isoformat()


def error_code(error):
    return getattr(error, "code", type(error).__name__)


def error_details(error):
    """Record diagnostic types and numeric codes without provider messages or bodies."""
    result = {
        "error_code": error_code(error),
        "error_type": type(error).__name__,
        "http_status": None,
        "errno": None,
        "cause_types": [],
    }
    current = error
    seen = set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if current is not error:
            result["cause_types"].append(type(current).__name__)
        details = getattr(current, "details", {})
        status = (
            getattr(current, "status_code", None)
            or getattr(getattr(current, "response", None), "status_code", None)
            or (details.get("http_status") if isinstance(details, dict) else None)
        )
        errno = getattr(current, "errno", None)
        if result["http_status"] is None and isinstance(status, int):
            result["http_status"] = status
        if result["errno"] is None and isinstance(errno, int):
            result["errno"] = errno
        current = current.__cause__ or current.__context__
    return result


def sanitize(value, sensitive_values=()):
    """Redact before clipping, including credentials embedded in ordinary text."""
    return redact(json_value(value), sensitive_values=sensitive_values)


def json_value(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"unrecorded_type": type(value).__name__}


def bounded(value, *, string_limit=8000, list_limit=100, depth=0):
    if depth > 14:
        return {"truncated": True, "reason": "depth_limit"}
    if isinstance(value, str) and len(value.encode()) > string_limit:
        raw = value.encode()
        return {
            "preview": raw[:string_limit].decode(errors="ignore"),
            "truncated": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, dict):
        result = {
            key: bounded(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1)
            for key, item in list(value.items())[:list_limit]
        }
        if len(value) > list_limit:
            result["_truncated_fields"] = len(value) - list_limit
        return result
    if isinstance(value, list):
        result = [
            bounded(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            result.append({"truncated": True, "omitted_items": len(value) - list_limit})
        return result
    return value


class Observer:
    """Inject one instance; ContextVar scopes isolate concurrent asyncio tasks.

    A None root is disabled. Capture never waits for filesystem I/O. Disk errors,
    queue overflow and post-close captures count as gaps and emit a warning.
    """

    def __init__(self, root_dir: Path | None = None, *, sensitive_values=()):
        self.root_dir = Path(root_dir).expanduser().absolute() if root_dir is not None else None
        self.sensitive_values = tuple(v for v in sensitive_values if v)
        self._context = ContextVar(f"karen_trace_{uuid4().hex}", default={})
        self._queue = asyncio.Queue(maxsize=512)
        self._pending_bytes = 0
        self._worker = None
        self._accepting = False
        self._session_id = uuid4().hex
        self._seq = 0
        self.dropped = 0
        self.write_failures = 0
        self._warned = False

    async def start(self):
        if self.root_dir is None:
            return
        if self._worker is not None:
            raise RuntimeError("OBSERVABILITY_ALREADY_STARTED")
        try:
            await asyncio.to_thread(self._initialize)
        except OSError:
            self._gap("storage_unavailable", failed=True)
            return
        self._accepting = True
        self._worker = asyncio.create_task(self._write())

    def _initialize(self):
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root_dir.is_symlink():
            raise OSError("Symlink observation root")
        (self.root_dir / "traces").mkdir(mode=0o700, exist_ok=True)
        if (self.root_dir / "traces").is_symlink():
            raise OSError("Symlink trace directory")

    def _gap(self, reason, *, failed=False):
        self.dropped += 1
        self.write_failures += int(failed)
        if not self._warned:
            LOG.warning("Karen observation gap (%s); task execution continues", reason)
            self._warned = True

    def context(self):
        return dict(self._context.get())

    @contextmanager
    def span(self, stage, *, trace_id=None, **identity):
        parent = self.context()
        detached = trace_id is not None and trace_id != parent.get("trace_id")
        context = dict(identity) if detached else {**parent, **identity}
        if trace_id is not None or not context.get("trace_id"):
            context["trace_id"] = trace_id or uuid4().hex
        context["parent_span_id"] = None if detached else parent.get("span_id")
        context["span_id"] = uuid4().hex
        context["stage"] = stage
        token = self._context.set(context)
        started = time.monotonic()
        outcome = {}
        self.emit("span.started")
        try:
            yield outcome
        except BaseException as exc:
            self.emit(
                "span.finished",
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                data=error_details(exc),
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            raise
        else:
            self.emit(
                "span.finished",
                data=outcome,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
        finally:
            self._context.reset(token)

    def emit(self, event_type, *, data=None, status="ok", duration_ms=None):
        if self.root_dir is None:
            return
        if not self._accepting:
            self._gap("not_accepting")
            return
        try:
            context = self.context()
            # Caller-provided identity strings never become filenames.
            trace_id = context.get("trace_id", self._session_id)
            if not re.fullmatch(r"[a-f0-9]{32}", trace_id):
                raise ValueError("Invalid trace id")
            self._seq += 1
            details = bounded(sanitize(data or {}, self.sensitive_values))
            if len(json.dumps(details, ensure_ascii=False).encode()) > 48 * 1024:
                details = bounded(details, string_limit=1000, list_limit=12)
                details = {"preview": details, "truncated": True, "reason": "event_budget"}
                if len(json.dumps(details, ensure_ascii=False).encode()) > 48 * 1024:
                    details = {"truncated": True, "reason": "event_budget"}
            event = {
                **context,
                "schema_version": "1.0",
                "event_id": uuid4().hex,
                "process_session_id": self._session_id,
                "seq": self._seq,
                "timestamp_utc": utcnow(),
                "trace_id": trace_id,
                "event_type": event_type,
                "status": status,
                "duration_ms": duration_ms,
                "data": details,
                "coverage": {"dropped": self.dropped, "write_failures": self.write_failures},
            }
            line = (
                json.dumps(
                    sanitize(event, self.sensitive_values), ensure_ascii=False, allow_nan=False
                ).encode()
                + b"\n"
            )
            if self._queue.full() or self._pending_bytes + len(line) > 4 * 1024 * 1024:
                self._gap("queue_full")
                return
            self._pending_bytes += len(line)
            self._queue.put_nowait((trace_id, line))
        except Exception:
            self._gap("capture_failed")

    def _append(self, trace_id, line):
        path = self.root_dir / "traces" / f"{trace_id}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(line)
            handle.flush()

    async def _write(self):
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                trace_id, line = item
                # Finish issued I/O before returning from close, including cancellation.
                task = asyncio.create_task(asyncio.to_thread(self._append, trace_id, line))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            except Exception:
                self._gap("write_failed", failed=True)
            finally:
                if item is not None:
                    self._pending_bytes -= len(item[1])
                self._queue.task_done()

    async def close(self):
        if self._worker is None:
            self._accepting = False
            return
        self.emit(
            "observer.closed", data={"dropped": self.dropped, "write_failures": self.write_failures}
        )
        self._accepting = False
        await self._queue.put(None)
        await self._worker
        self._worker = None
        # Telemetry is best-effort, but known gaps survive normal process exit.
        try:
            await asyncio.to_thread(self._save_health)
        except OSError:
            self._gap("health_write_failed", failed=True)

    def _save_health(self):
        path = self.root_dir / "traces" / f"{self._session_id}.health.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "process_session_id": self._session_id,
                    "closed_at": utcnow(),
                    "dropped": self.dropped,
                    "write_failures": self.write_failures,
                },
                handle,
            )

    def node(self, stage, function, summarize):
        if self.root_dir is None:
            return function

        async def invoke(state):
            with self.span(stage):
                result = function(state)
                if inspect.isawaitable(result):
                    result = await result
                try:
                    self.emit("decision", data=summarize(result))
                except Exception:
                    self._gap("summary_failed")
                return result

        return invoke


class ObservedModel:
    """Preserve the engine ModelClient contract; record actual usage, not estimates."""

    def __init__(self, client, observer: Observer):
        self.client = client
        self.observer = observer
        self.metadata = getattr(client, "metadata", {})

    async def generate(self, request):
        with self.observer.span("model.call", model_role=request.role) as outcome:
            prompt = request.system_instruction + "\n" + request.task_instruction
            self.observer.emit(
                "model.request",
                data={
                    "model": self.metadata,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "system_instruction": request.system_instruction,
                    "task_instruction": request.task_instruction,
                    "input": request.input_data,
                    "output_schema": request.output_schema,
                    "timeout_seconds": request.timeout_seconds,
                },
            )
            try:
                response = await self.client.generate(request)
            except Exception as exc:
                self.observer.emit(
                    "model.error",
                    status="failed",
                    data={
                        **error_details(exc),
                        "usage": getattr(exc, "usage", None),
                        "retryable": getattr(exc, "retryable", None),
                    },
                )
                raise
            outcome.update(usage=response.usage, provider_request_id=response.provider_request_id)
            self.observer.emit(
                "model.response",
                data={
                    "payload": response.payload,
                    "usage": response.usage,
                    "provider_request_id": response.provider_request_id,
                    "model": response.response_metadata.get("model"),
                },
            )
            return response
