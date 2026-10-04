"""Memory lifecycle: nonblocking submission, durable jobs and awaited recall."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from dynamic_graph.models.client import ModelCallError, ModelClient
from langchain_core.embeddings import Embeddings

from ..observability import Observer
from .contracts import (
    ContextEvent,
    DetailHit,
    DetailQuery,
    DetailSearchResult,
    Evidence,
    MemoryFlushError,
    MemoryQueueFull,
    PersistenceError,
    RecallQuery,
    RecallResult,
    WriteReceipt,
    WriteStatus,
    utcnow,
)
from .extraction import Extractor
from .storage import Storage, digest, encode, pointer_value, redact, terms, text_fields


class ContextMemory:
    def __init__(
        self,
        *,
        root_dir: Path,
        model: ModelClient,
        embeddings: Embeddings,
        observer: Observer | None = None,
    ):
        self.root_dir = Path(root_dir).expanduser().absolute()
        self.model = model
        self.embeddings = embeddings
        self.observer = observer or Observer()
        self.storage = Storage(self.root_dir)
        self._queue = asyncio.Queue(maxsize=256)
        self._pending = {}
        self._identities = {}
        self._sequence = 0
        self._pending_bytes = 0
        self._raw_failures = {}
        self._trace_links = {}
        self._io_tasks = set()
        self._foreground_count = 0
        self._background_allowed = asyncio.Event()
        self._background_allowed.set()
        self._wake = asyncio.Event()
        self._started = False
        self._closing = False
        self._lock_fd = None
        self._workers = []
        self._embedding_lock = asyncio.Lock()
        self._model_tag = None
        self._extractor = Extractor(
            self.storage, model, self._background_allowed.wait, self._io, self.embed, self.observer
        )

    async def _io(self, function, *args, **kwargs):
        # Shield disk transactions: cancelling a coroutine cannot stop its OS thread.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._io_tasks.add(task)

        def finished(done):
            self._io_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("MEMORY_ALREADY_STARTED")
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lock_fd = os.open(
                self.root_dir / ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self._io(self.storage.initialize)
            for event_id, sequence, checksum in await self._io(self.storage.identities):
                self._identities[event_id] = (
                    checksum,
                    WriteReceipt(event_id=event_id, sequence=sequence),
                )
                self._sequence = max(self._sequence, sequence)
            self._started = True
            self._closing = False
            from .retrieval import Retriever

            self._retriever = Retriever(self)
            self._workers = [
                asyncio.create_task(self._write_raw()),
                asyncio.create_task(self._derive()),
            ]
        except BaseException:
            self._release_lock()
            raise

    def _require_started(self):
        if not self._started or self._closing:
            raise RuntimeError("MEMORY_NOT_ACCEPTING_REQUESTS")

    def submit(self, event: ContextEvent) -> WriteReceipt:
        self._require_started()
        # Serialize/validate once to isolate all nested mutable application data.
        paths = []
        data = redact(event.model_dump(mode="json"), paths=paths)
        data["redacted_fields"] = list(dict.fromkeys([*data["redacted_fields"], *paths]))
        event = ContextEvent.model_validate(data)
        checksum = digest(data)
        if event.event_id in self._identities:
            old_hash, receipt = self._identities[event.event_id]
            if old_hash != checksum:
                raise ValueError("DUPLICATE_EVENT_MISMATCH")
            return receipt
        size = len(encode(data).encode())
        if self._queue.full() or self._pending_bytes + size > 8 * 1024 * 1024:
            raise MemoryQueueFull("MEMORY_QUEUE_FULL")
        self._sequence += 1
        receipt = WriteReceipt(event_id=event.event_id, sequence=self._sequence)
        self._identities[event.event_id] = (checksum, receipt)
        self._pending[event.event_id] = (event, receipt, utcnow(), size)
        self._pending_bytes += size
        self._queue.put_nowait(event.event_id)
        self._trace_links[event.event_id] = self.observer.context()
        self.observer.emit(
            "memory.submitted",
            data={
                "event_id": event.event_id,
                "event_type": event.event_type,
                "sequence": receipt.sequence,
                "queue_depth": self._queue.qsize(),
            },
        )
        return receipt

    @asynccontextmanager
    async def foreground(self):
        self._require_started()
        self._foreground_count += 1
        self._background_allowed.clear()
        try:
            yield
        finally:
            self._foreground_count -= 1
            if not self._foreground_count:
                self._background_allowed.set()
                self._wake.set()

    async def _write_raw(self):
        while True:
            event_id = await self._queue.get()
            try:
                event, receipt, submitted, _ = self._pending[event_id]
                link = self._trace_links.get(event_id, {})
                with self.observer.span(
                    "memory.persist",
                    trace_id=uuid4().hex,
                    source_event_id=event_id,
                    conversation_id=event.conversation_id,
                    request_id=event.request_id,
                    source_trace_id=link.get("trace_id"),
                    source_span_id=link.get("span_id"),
                ):
                    await self._io(self.storage.append, event, receipt.sequence, submitted)
                    self.observer.emit(
                        "memory.persisted", data={"sequence": receipt.sequence, "raw": "persisted"}
                    )
                _, _, _, size = self._pending.pop(event_id)
                self._pending_bytes -= size
                self._wake.set()
            except Exception:
                self._raw_failures[event_id] = "MEMORY_PERSISTENCE_FAILED"
                self.observer.emit(
                    "memory.persistence_failed", status="failed", data={"event_id": event_id}
                )
                self._trace_links.pop(event_id, None)
            finally:
                self._queue.task_done()

    async def _derive(self):
        while True:
            await self._background_allowed.wait()
            job = await self._io(self.storage.next_job)
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.25)
                except TimeoutError:
                    pass
                continue
            event_id = job["event_id"]
            event = None
            link = self._trace_links.get(event_id, {})
            try:
                event = await self._io(self.storage.load_event, event_id)
                with self.observer.span(
                    "memory.derive",
                    trace_id=uuid4().hex,
                    source_event_id=event_id,
                    conversation_id=event.conversation_id,
                    request_id=event.request_id,
                    source_trace_id=link.get("trace_id"),
                    source_span_id=link.get("span_id"),
                    attempt=job["attempts"] + 1,
                ):
                    await self._derive_job(job)
                self._trace_links.pop(event_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job = await self._io(self.storage.job, event_id)
                attempts = job["attempts"] + 1
                temporary = not isinstance(exc, ModelCallError) or exc.retryable
                terminal = attempts >= 3 or not temporary
                code = exc.code if isinstance(exc, ModelCallError) else "MEMORY_PROCESSING_FAILED"
                values = {
                    "attempts": attempts,
                    "error_code": code,
                    "next_attempt_at": utcnow().timestamp() + min(2**attempts, 30),
                }
                if job["derived"] == "committed":
                    values["index_state"] = "failed" if terminal else "partial"
                else:
                    values["derived"] = "failed" if terminal else "pending"
                await self._io(self.storage.update_job, event_id, **values)
                with self.observer.span(
                    "memory.retry",
                    trace_id=uuid4().hex,
                    source_event_id=event_id,
                    request_id=event.request_id if event else link.get("request_id"),
                    conversation_id=event.conversation_id if event else link.get("conversation_id"),
                    source_trace_id=link.get("trace_id"),
                    source_span_id=link.get("span_id"),
                ):
                    self.observer.emit(
                        "memory.retry",
                        status="failed" if terminal else "degraded",
                        data={
                            "event_id": event_id,
                            "attempt": attempts,
                            "terminal": terminal,
                            **values,
                        },
                    )
                if terminal:
                    self._trace_links.pop(event_id, None)

    async def _derive_job(self, job):
        event_id = job["event_id"]
        if job["derived"] in {"pending", "extracting"}:
            await self._extractor.graph.ainvoke({"event_id": event_id})
        job = await self._io(self.storage.job, event_id)
        if job["derived"] == "committed":
            await self._background_allowed.wait()
            items = await self._io(self.storage.index_items, event_id)
            with self.observer.span("memory.index"):
                if items:
                    vectors, tag = await self.embed([item.text for item in items], background=True)
                    await self._io(self.storage.save_vectors, items, vectors, tag)
                await self._io(
                    self.storage.update_job,
                    event_id,
                    index_state="indexed",
                    attempts=0,
                    next_attempt_at=0,
                    error_code=None,
                )
                self.observer.emit(
                    "memory.indexed",
                    data={"memory_ids": [m.memory_id for m in items], "index": "indexed"},
                )

    async def _embedding_identity(self):
        model = getattr(self.embeddings, "model", None)
        if not model:
            raise ValueError("EMBEDDING_MODEL_ID_REQUIRED")
        if type(self.embeddings).__module__.startswith("langchain_ollama"):
            import httpx

            base = getattr(self.embeddings, "base_url", None) or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(base.rstrip("/") + "/api/tags")
                response.raise_for_status()
                names = {model, model + ":latest"}
                entry = next(
                    (item for item in response.json()["models"] if item["name"] in names), None
                )
                if entry is None or not entry.get("digest"):
                    raise ValueError("EMBEDDING_MODEL_NOT_INSTALLED")
                return model + "@" + entry["digest"]
        return f"{type(self.embeddings).__module__}.{type(self.embeddings).__name__}:{model}"

    async def embed(self, texts, *, background=False):
        with self.observer.span("memory.embedding") as outcome:
            self.observer.emit(
                "embedding.request",
                data={
                    "model": getattr(self.embeddings, "model", None),
                    "texts": texts,
                    "background": background,
                },
            )
            vectors, tag = await self._embed(texts, background=background)
            outcome.update(
                model_tag=tag, dimensions=len(vectors[0]) if vectors else 0, count=len(vectors)
            )
            return vectors, tag

    async def _embed(self, texts, *, background=False):
        if background:
            await self._background_allowed.wait()
        async with self._embedding_lock:
            before = await self._embedding_identity()
            if self._model_tag != before:
                await self._io(self.storage.invalidate_vectors, before)
                self._model_tag = before
            # Derivation runs asynchronously; long summaries may need more time on
            # a local CPU. Foreground recall retains its own short deadline.
            async with asyncio.timeout(30 if background else 10):
                vectors = await self.embeddings.aembed_documents(texts)
            after = await self._embedding_identity()
            if before != after:
                raise ValueError("EMBEDDING_MODEL_CHANGED_DURING_CALL")
            return vectors, before

    async def write_status(self, receipt: WriteReceipt) -> WriteStatus:
        if self._identities.get(receipt.event_id, (None, None))[1] != receipt:
            raise ValueError("UNKNOWN_WRITE_RECEIPT")
        if receipt.event_id in self._raw_failures:
            return WriteStatus(
                receipt=receipt, raw="failed", error_code=self._raw_failures[receipt.event_id]
            )
        job = await self._io(self.storage.job, receipt.event_id)
        if job is None:
            return WriteStatus(receipt=receipt)
        return WriteStatus(
            receipt=receipt,
            raw="persisted",
            derived=job["derived"],
            index=job["index_state"],
            attempts=job["attempts"],
            error_code=job["error_code"],
        )

    async def flush(self, receipt: WriteReceipt | None = None) -> None:
        self._require_started()
        if self._foreground_count:
            raise RuntimeError("FLUSH_INSIDE_FOREGROUND_WOULD_BLOCK_BACKGROUND")
        watermark = self._sequence
        receipts = [r for _, r in self._identities.values() if r.sequence <= watermark]
        if receipt is not None:
            receipts = [receipt]
        while True:
            statuses = [await self.write_status(r) for r in receipts]
            if all(
                s.raw == "failed"
                or s.derived in {"failed", "skipped"}
                or (s.derived == "committed" and s.index in {"indexed", "failed"})
                for s in statuses
            ):
                bad = [
                    s
                    for s in statuses
                    if s.raw == "failed" or s.derived == "failed" or s.index == "failed"
                ]
                if bad:
                    raise MemoryFlushError(bad)
                return
            await asyncio.sleep(0.02)

    async def recall(self, query: RecallQuery) -> RecallResult:
        async with self.foreground():
            with self.observer.span("memory.recall"):
                result = await self._retriever.recall(query)
                self.observer.emit(
                    "memory.recalled",
                    status="degraded" if result.degradations else "ok",
                    data=result,
                )
                return result

    async def load_event(self, event_id, *, max_bytes=None):
        if event_id in self._pending:
            if max_bytes is not None and self._pending[event_id][3] > max_bytes:
                raise PersistenceError("SOURCE_READ_BUDGET_EXCEEDED")
            return self._pending[event_id][0].model_copy(deep=True)
        return await self._io(self.storage.load_event, event_id, max_bytes=max_bytes)

    async def source_ref(self, event, pointer, quote=""):
        source = await self._io(self.storage.source_ref, event, pointer, quote)
        if event.event_id in self._raw_failures:
            source = source.model_copy(update={"storage_state": "failed"})
        return source

    async def recent(self, query, analysis):
        rows = await self._io(
            self.storage.event_rows,
            conversation_id=None if analysis.time_range else query.conversation_id,
            time_range=analysis.time_range,
            captured_before=analysis.at if analysis.time_mode == "known_at" else None,
            limit=24,
        )
        events = [(row["sequence"], await self.load_event(row["event_id"])) for row in rows]
        for event, receipt, _, _ in tuple(self._pending.values()):
            if analysis.time_mode == "known_at" and event.occurred_at > analysis.at:
                continue
            if (
                analysis.time_range
                and analysis.time_range.start <= event.occurred_at < analysis.time_range.end
                or not analysis.time_range
                and event.conversation_id == query.conversation_id
            ):
                events.append((receipt.sequence, event.model_copy(deep=True)))
        events.sort(key=lambda item: item[0], reverse=True)
        tasks, selected, seen = [], [], set(query.exclude_event_ids)
        for _, event in events:
            if event.event_id in seen or event.request_id == query.request_id:
                continue
            seen.add(event.event_id)
            if event.request_id not in tasks:
                if len(tasks) >= 3:
                    continue
                tasks.append(event.request_id)
            selected.append(event)
            if len(selected) == 12:
                break
        return selected

    async def search_details(self, query: DetailQuery) -> DetailSearchResult:
        with self.observer.span("memory.details"):
            self.observer.emit("memory.details_query", data=query)
            result = await self._search_details(query)
            self.observer.emit(
                "memory.details_result",
                status="degraded" if result.status != "complete" else "ok",
                data=result,
            )
            return result

    async def _search_details(self, query: DetailQuery) -> DetailSearchResult:
        self._require_started()
        scope = query.model_dump(mode="json", exclude={"sources", "text"})
        if not query.sources and not any(
            (query.request_id, query.conversation_id, query.time_range)
        ):
            return DetailSearchResult(status="needs_scope", scope=scope)
        start = time.monotonic()
        hits, scanned, read_bytes, partial = [], 0, 0, False
        if query.sources:
            event_ids = list(dict.fromkeys(s.event_id for s in query.sources))
        else:
            rows = await self._io(
                self.storage.event_rows,
                request_id=query.request_id,
                conversation_id=query.conversation_id,
                time_range=query.time_range,
            )
            event_ids = [r["event_id"] for r in rows]
            for event, _, _, _ in tuple(self._pending.values()):
                if query.request_id and event.request_id != query.request_id:
                    continue
                if query.conversation_id and event.conversation_id != query.conversation_id:
                    continue
                if (
                    query.time_range
                    and not query.time_range.start <= event.occurred_at < query.time_range.end
                ):
                    continue
                event_ids.append(event.event_id)
            event_ids = list(dict.fromkeys(event_ids))
        words = terms(query.text)
        for event_id in event_ids:
            if scanned >= 200 or read_bytes >= 2 * 1024 * 1024 or time.monotonic() - start >= 2:
                partial = True
                break
            try:
                event = await self.load_event(event_id, max_bytes=2 * 1024 * 1024 - read_bytes)
                data = event.model_dump(mode="json")
                size = len(encode(data).encode())
                if read_bytes + size > 2 * 1024 * 1024:
                    partial = True
                    break
                read_bytes += size
                scanned += 1
                refs = [s for s in query.sources if s.event_id == event_id]
                fields = (
                    [
                        (pointer, text)
                        for source in refs
                        for pointer, text in text_fields(
                            pointer_value(data, source.pointer), source.pointer
                        )
                    ]
                    if refs
                    else list(text_fields(data["payload"], "/payload"))
                )
                for pointer, text in fields:
                    if not isinstance(text, str) or not text or "/memory" in pointer:
                        continue
                    if not refs and words and not any(w in text.casefold() for w in words):
                        continue
                    source = await self._io(
                        self.storage.source,
                        Evidence(event_id=event_id, pointer=pointer, quote=text[:1000] or " "),
                        {event_id: event},
                    )
                    if event_id in self._raw_failures:
                        source = source.model_copy(update={"storage_state": "failed"})
                    match = next(
                        (text.casefold().find(w) for w in words if w in text.casefold()), 0
                    )
                    snippet = text[max(0, match - 200) : max(0, match - 200) + 2400]
                    hits.append(DetailHit(text=snippet, source=source, truncated=snippet != text))
            except (PersistenceError, ValueError, KeyError):
                partial = True
        return DetailSearchResult(
            status="partial" if partial else "complete",
            hits=hits,
            scanned_events=scanned,
            scope=scope,
        )

    async def reindex(self) -> None:
        """Rebuild retrieval data without reinterpretation of stored facts."""
        self._require_started()
        tag = await self._embedding_identity()
        await self._io(self.storage.rebuild_keywords)
        await self._io(self.storage.invalidate_vectors, tag, force=True)
        self._model_tag = tag
        self._wake.set()

    async def close(self) -> None:
        if not self._started:
            return
        if self._foreground_count:
            raise RuntimeError("CLOSE_INSIDE_FOREGROUND")
        self._closing = True
        self._background_allowed.clear()
        await self._queue.join()
        self._workers[1].cancel()
        await asyncio.gather(self._workers[1], return_exceptions=True)
        self._workers[0].cancel()
        await asyncio.gather(self._workers[0], return_exceptions=True)
        while self._io_tasks:
            await asyncio.gather(*tuple(self._io_tasks), return_exceptions=True)
        failed = list(self._raw_failures)
        self._started = False
        self._release_lock()
        if failed:
            raise PersistenceError("MEMORY_PERSISTENCE_FAILED:" + ",".join(failed))

    def _release_lock(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
