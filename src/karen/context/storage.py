"""Local JSONL, SQLite transactions, source resolution and rebuildable indexes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import jieba
import numpy as np

from .contracts import (
    ContextEvent,
    Evidence,
    PersistenceError,
    SourceRef,
    StoredMemory,
    TimeRange,
    utcnow,
)

jieba.setLogLevel(40)

FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, layer UNINDEXED, tokens)"


def encode(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def terms(text: str) -> list[str]:
    """The same Chinese/identifier token rules apply to indexing and querying."""
    words = jieba.lcut(text.casefold(), cut_all=False)
    return list(dict.fromkeys(w for w in words if re.search(r"\w", w)))


def redact(value, *, paths=None, prefix=""):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            path = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if re.search(
                r"(?i)(?:^|_)(api[_-]?key|authorization|password|access[_-]?token|secret|private[_-]?key)$",
                key,
            ):
                result[key] = "[REDACTED]"
                if paths is not None:
                    paths.append(path)
            else:
                result[key] = redact(item, paths=paths, prefix=path)
        return result
    if isinstance(value, list):
        return [
            redact(item, paths=paths, prefix=f"{prefix}/{index}")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", value)
        redacted = re.sub(
            r"(?i)(\b(?:[a-z0-9]+_)?(?:api[_-]?key|password|access[_-]?token|authorization)\s*[:=]\s*)(?:Bearer\s+)?\S+",
            r"\1[REDACTED]",
            redacted,
        )
        if redacted != value and paths is not None:
            paths.append(prefix)
        return redacted
    return value


def pointer_value(value, pointer: str):
    if not pointer.startswith("/"):
        raise ValueError("INVALID_SOURCE_POINTER")
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def text_fields(value, prefix=""):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from text_fields(item, f"{prefix}/{escaped}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from text_fields(item, f"{prefix}/{index}")


def stable_id(event_id: str, kind: str, key: str) -> str:
    return uuid5(NAMESPACE_URL, f"karen:{event_id}:{kind}:{key}").hex


class Storage:
    def __init__(self, root: Path):
        self.root = root
        self.db = root / "memory.sqlite3"
        self.lock = threading.RLock()

    @contextmanager
    def connection(self):
        # sqlite connect's context manager commits but does not close the connection.
        with self.lock:
            conn = sqlite3.connect(self.safe_path("memory.sqlite3"), timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def initialize(self):
        for directory in (self.root, self.root / "m3", self.root / "attachments"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm"):
            self.safe_path(name)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('schema', 1);
                INSERT OR IGNORE INTO meta VALUES('revision', 0);
                CREATE TABLE IF NOT EXISTS events(
                    event_id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL,
                    hash TEXT NOT NULL, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, occurred REAL NOT NULL, relative_file TEXT NOT NULL,
                    offset INTEGER NOT NULL, length INTEGER NOT NULL, checksum TEXT NOT NULL,
                    submitted_at TEXT NOT NULL, persisted_at TEXT NOT NULL,
                    task_result TEXT
                );
                CREATE INDEX IF NOT EXISTS events_time ON events(occurred);
                CREATE INDEX IF NOT EXISTS events_task ON events(request_id, sequence);
                CREATE INDEX IF NOT EXISTS events_conversation ON events(conversation_id, sequence);
                CREATE TABLE IF NOT EXISTS files(
                    relative_file TEXT PRIMARY KEY, watermark INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS write_jobs(
                    event_id TEXT PRIMARY KEY, derived TEXT NOT NULL DEFAULT 'pending',
                    index_state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, next_attempt_at REAL NOT NULL DEFAULT 0,
                    extraction TEXT, verification TEXT, model_info TEXT
                );
                CREATE TABLE IF NOT EXISTS memories(
                    memory_id TEXT PRIMARY KEY, layer TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_revisions(
                    revision INTEGER NOT NULL, memory_id TEXT NOT NULL, body TEXT NOT NULL,
                    committed_at REAL NOT NULL, PRIMARY KEY(revision,memory_id)
                );
                CREATE INDEX IF NOT EXISTS revisions_time ON memory_revisions(committed_at);
                CREATE TABLE IF NOT EXISTS embeddings(
                    memory_id TEXT PRIMARY KEY, model_tag TEXT NOT NULL, dimension INTEGER NOT NULL,
                    text_hash TEXT NOT NULL, vector BLOB NOT NULL, indexed_at REAL NOT NULL
                );
            """)
            conn.execute(FTS_SCHEMA)
            if conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] != 1:
                raise PersistenceError("UNSUPPORTED_MEMORY_SCHEMA")
            conn.execute("SELECT bm25(memory_fts) FROM memory_fts LIMIT 1").fetchall()
        os.chmod(self.db, 0o600)
        self.recover()
        with self.connection() as conn:
            conn.execute(
                "UPDATE write_jobs SET derived='pending',attempts=0,next_attempt_at=0,"
                "error_code=NULL,extraction=NULL,verification=NULL,model_info=NULL WHERE derived='failed'"
            )
            conn.execute(
                "UPDATE write_jobs SET index_state='pending',attempts=0,next_attempt_at=0,"
                "error_code=NULL WHERE derived='committed' AND index_state='failed'"
            )

    def safe_path(self, relative: str) -> Path:
        path = self.root / relative
        if path.resolve().is_relative_to(self.root.resolve()) and not path.is_symlink():
            return path
        raise PersistenceError("INVALID_MEMORY_PATH")

    def _attachments(self, value, *, expand=False, budget=None):
        if isinstance(value, dict):
            if expand and set(value) == {"$attachment"}:
                path = self.safe_path(value["$attachment"])
                if budget is not None:
                    budget[0] -= path.stat().st_size
                    if budget[0] < 0:
                        raise PersistenceError("SOURCE_READ_BUDGET_EXCEEDED")
                return json.loads(path.read_text())
            return {k: self._attachments(v, expand=expand, budget=budget) for k, v in value.items()}
        if isinstance(value, list):
            return [self._attachments(v, expand=expand, budget=budget) for v in value]
        if not expand and isinstance(value, str) and len(value.encode()) > 16 * 1024:
            name = "attachments/" + hashlib.sha256(value.encode()).hexdigest() + ".json"
            path = self.safe_path(name)
            if not path.exists():
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=path.parent, delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(encode(value))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                self._sync_directory(path.parent)
            return {"$attachment": name}
        return value

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(self, event: ContextEvent, sequence: int, submitted_at: datetime):
        with self.lock:
            original = event.model_dump(mode="json")
            date = event.occurred_at.astimezone(ZoneInfo(event.timezone)).date().isoformat()
            relative = f"m3/{date}.jsonl"
            frame = {
                "event": self._attachments(original),
                "sequence": sequence,
                "submitted_at": submitted_at.isoformat(),
                "hash": digest(original),
            }
            raw = (encode(frame) + "\n").encode()
            path = self.safe_path(relative)
            created = not path.exists()
            with path.open("ab") as stream:
                os.chmod(path, 0o600)
                offset = stream.tell()
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if created:
                self._sync_directory(path.parent)
            self._register(frame, relative, offset, raw)

    def _register(self, frame, relative, offset, raw):
        event = ContextEvent.model_validate(self._attachments(frame["event"], expand=True))
        original = event.model_dump(mode="json")
        if digest(original) != frame["hash"]:
            raise PersistenceError("MEMORY_CHECKSUM_MISMATCH")
        result = encode(event.payload) if event.event_type == "task_result" else None
        with self.connection() as conn:
            old = conn.execute(
                "SELECT hash FROM events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if old and old[0] != frame["hash"]:
                raise PersistenceError("DUPLICATE_EVENT_MISMATCH")
            if not old:
                conn.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        frame["sequence"],
                        frame["hash"],
                        event.conversation_id,
                        event.request_id,
                        event.event_type,
                        event.occurred_at.timestamp(),
                        relative,
                        offset,
                        len(raw),
                        hashlib.sha256(raw).hexdigest(),
                        frame["submitted_at"],
                        utcnow().isoformat(),
                        result,
                    ),
                )
                conn.execute("INSERT INTO write_jobs(event_id) VALUES(?)", (event.event_id,))
            conn.execute(
                "INSERT INTO files VALUES(?,?) ON CONFLICT(relative_file) DO UPDATE SET "
                "watermark=max(watermark,excluded.watermark)",
                (relative, offset + len(raw)),
            )

    def recover(self):
        with self.connection() as conn:
            watermarks = dict(conn.execute("SELECT relative_file,watermark FROM files"))
        for path in sorted((self.root / "m3").glob("*.jsonl")):
            relative = path.relative_to(self.root).as_posix()
            path = self.safe_path(relative)
            offset = watermarks.get(relative, 0)
            if path.stat().st_size < offset:
                raise PersistenceError("MEMORY_FILE_TRUNCATED")
            with path.open("r+b") as stream:
                stream.seek(offset)
                while raw := stream.readline():
                    if not raw.endswith(b"\n"):
                        # Only an incomplete final frame may be truncated.
                        stream.seek(offset)
                        stream.truncate()
                        stream.flush()
                        os.fsync(stream.fileno())
                        break
                    try:
                        frame = json.loads(raw)
                        self._register(frame, relative, offset, raw)
                    except Exception as exc:
                        raise PersistenceError("MEMORY_RECOVERY_CORRUPT") from exc
                    offset += len(raw)

    def identities(self):
        with self.connection() as conn:
            return [
                (r[0], r[1], r[2])
                for r in conn.execute("SELECT event_id,sequence,hash FROM events")
            ]

    def event_rows(
        self,
        *,
        request_id=None,
        conversation_id=None,
        time_range=None,
        captured_before=None,
        through_event_id=None,
        limit=201,
    ):
        clauses, params = [], []
        for column, value in (("request_id", request_id), ("conversation_id", conversation_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if time_range:
            clauses.extend(["occurred>=?", "occurred<?"])
            params.extend([time_range.start.timestamp(), time_range.end.timestamp()])
        if captured_before is not None:
            clauses.append("occurred<=?")
            params.append(captured_before.timestamp())
        if through_event_id:
            clauses.append("sequence<=(SELECT sequence FROM events WHERE event_id=?)")
            params.append(through_event_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM events" + where + " ORDER BY sequence DESC LIMIT ?",
                    (*params, limit),
                )
            ]

    def load_event(self, event_id: str, *, max_bytes=None) -> ContextEvent:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise PersistenceError("SOURCE_NOT_FOUND")
        if max_bytes is not None and row["length"] > max_bytes:
            raise PersistenceError("SOURCE_READ_BUDGET_EXCEEDED")
        with self.safe_path(row["relative_file"]).open("rb") as stream:
            stream.seek(row["offset"])
            raw = stream.read(row["length"])
        if hashlib.sha256(raw).hexdigest() != row["checksum"]:
            raise PersistenceError("MEMORY_CHECKSUM_MISMATCH")
        frame = json.loads(raw)
        budget = [max_bytes - len(raw)] if max_bytes is not None else None
        event = ContextEvent.model_validate(
            self._attachments(frame["event"], expand=True, budget=budget)
        )
        if event.event_id != event_id or digest(event.model_dump(mode="json")) != row["hash"]:
            raise PersistenceError("MEMORY_CHECKSUM_MISMATCH")
        return event

    def source(self, evidence: Evidence, allowed_events: dict[str, ContextEvent]) -> SourceRef:
        event = allowed_events.get(evidence.event_id)
        if event is None or "/memory" in evidence.pointer:
            raise ValueError("INVALID_SOURCE_EVENT")
        value = pointer_value(event.model_dump(mode="json"), evidence.pointer)
        if not isinstance(value, str) or evidence.quote not in value:
            raise ValueError("INVALID_SOURCE_QUOTE")
        return self.source_ref(event, evidence.pointer, evidence.quote)

    def source_ref(self, event: ContextEvent, pointer: str, quote: str = "") -> SourceRef:
        """Assign provenance from the captured event, never model-generated metadata."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT relative_file FROM events WHERE event_id=?", (event.event_id,)
            ).fetchone()
        role = {"user_message": "user", "task_result": "tool"}.get(event.event_type, "assistant")
        return SourceRef(
            event_id=event.event_id,
            pointer=pointer,
            quote=quote,
            source_role=role,
            occurred_at=event.occurred_at,
            relative_file=row[0] if row else None,
            storage_state="persisted" if row else "queued",
        )

    def job(self, event_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM write_jobs WHERE event_id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    def update_job(self, event_id, **values):
        allowed = {
            "derived",
            "index_state",
            "attempts",
            "error_code",
            "next_attempt_at",
            "extraction",
            "verification",
            "model_info",
        }
        if not values.keys() <= allowed:
            raise ValueError("INVALID_JOB_UPDATE")
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE write_jobs SET {assignments} WHERE event_id=?",
                (*values.values(), event_id),
            )

    def next_job(self):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT j.* FROM write_jobs j JOIN events e USING(event_id) "
                "WHERE ((derived IN ('pending','extracting')) OR "
                "(derived='committed' AND index_state IN ('pending','partial'))) "
                "AND next_attempt_at<=? ORDER BY e.sequence LIMIT 1",
                (utcnow().timestamp(),),
            ).fetchone()
            return dict(row) if row else None

    def snapshot(self, *, known_at: datetime | None = None):
        with self.connection() as conn:
            conn.execute("BEGIN")
            revision = conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            if known_at:
                rows = conn.execute(
                    "SELECT r.body FROM memory_revisions r JOIN (SELECT memory_id,max(revision) rev "
                    "FROM memory_revisions WHERE committed_at<=? GROUP BY memory_id) t "
                    "ON r.memory_id=t.memory_id AND r.revision=t.rev",
                    (known_at.timestamp(),),
                ).fetchall()
            else:
                rows = conn.execute("SELECT body FROM memories").fetchall()
            memories = {
                m.memory_id: m for m in (StoredMemory.model_validate_json(r[0]) for r in rows)
            }
            if known_at is not None:
                revision = max((memory.revision for memory in memories.values()), default=0)
            vectors = {
                row["memory_id"]: dict(row) for row in conn.execute("SELECT * FROM embeddings")
            }
            return revision, memories, vectors

    def keyword_ranks(self, query: str):
        words = terms(query)[:64]
        if not words:
            return {}
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        with self.connection() as conn:
            return {
                layer: [
                    r[0]
                    for r in conn.execute(
                        "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? AND layer=? "
                        "ORDER BY bm25(memory_fts),memory_id LIMIT 100",
                        (expression, layer),
                    )
                ]
                for layer in ("m1", "m2")
            }

    def commit_memories(self, event_id: str, changed: list[StoredMemory], expected_revision: int):
        with self.connection() as conn:
            current = conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            if current != expected_revision:
                raise ValueError("MEMORY_REVISION_CHANGED")
            revision = current + 1
            now = utcnow()
            for memory in changed:
                if (
                    memory.valid_from
                    and memory.valid_to
                    and memory.valid_from.bounds()[0] > memory.valid_to.bounds()[1]
                ):
                    raise ValueError("INVALID_FACT_PERIOD")
                memory = memory.model_copy(update={"revision": revision, "updated_at": now})
                body = memory.model_dump_json()
                conn.execute(
                    "INSERT INTO memories VALUES(?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
                    "layer=excluded.layer,body=excluded.body",
                    (memory.memory_id, memory.layer, body),
                )
                conn.execute(
                    "INSERT INTO memory_revisions VALUES(?,?,?,?)",
                    (revision, memory.memory_id, body, now.timestamp()),
                )
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory.memory_id,))
                conn.execute(
                    "INSERT INTO memory_fts VALUES(?,?,?)",
                    (memory.memory_id, memory.layer, " ".join(terms(memory.text))),
                )
            if changed:
                conn.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            conn.execute(
                "UPDATE write_jobs SET derived=?,index_state=?,error_code=NULL WHERE event_id=?",
                (
                    "committed" if changed else "skipped",
                    "pending" if changed else "indexed",
                    event_id,
                ),
            )

    def index_items(self, event_id: str | None = None):
        _, memories, vectors = self.snapshot()
        result = []
        for memory in memories.values():
            if event_id and event_id not in {s.event_id for s in memory.sources}:
                continue
            row = vectors.get(memory.memory_id)
            if row is None or row["text_hash"] != digest(memory.text):
                result.append(memory)
        return result

    def save_vectors(self, items: list[StoredMemory], vectors, model_tag: str):
        if len(items) != len(vectors):
            raise ValueError("INVALID_EMBEDDING_COUNT")
        dimensions = {np.asarray(vector).size for vector in vectors}
        if len(dimensions) > 1:
            raise ValueError("INCONSISTENT_EMBEDDING_DIMENSIONS")
        with self.connection() as conn:
            old_dimensions = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT dimension FROM embeddings WHERE model_tag=?", (model_tag,)
                )
            }
            if old_dimensions and dimensions != old_dimensions:
                raise ValueError("EMBEDDING_DIMENSION_CHANGED")
            for item, vector in zip(items, vectors, strict=True):
                array = np.asarray(vector, dtype=np.float32)
                if array.ndim != 1 or not array.size or not np.isfinite(array).all():
                    raise ValueError("INVALID_EMBEDDING")
                norm = float(np.linalg.norm(array))
                if not norm:
                    raise ValueError("ZERO_EMBEDDING")
                current = conn.execute(
                    "SELECT body FROM memories WHERE memory_id=?", (item.memory_id,)
                ).fetchone()
                if not current or StoredMemory.model_validate_json(current[0]).text != item.text:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?,?)",
                    (
                        item.memory_id,
                        model_tag,
                        array.size,
                        digest(item.text),
                        (array / norm).tobytes(),
                        utcnow().timestamp(),
                    ),
                )

    def invalidate_vectors(self, model_tag: str, *, force=False):
        with self.connection() as conn:
            if force:
                conn.execute("DELETE FROM embeddings")
                changed = True
            else:
                changed = conn.execute(
                    "DELETE FROM embeddings WHERE model_tag!=?", (model_tag,)
                ).rowcount
            if changed:
                conn.execute(
                    "UPDATE write_jobs SET index_state='pending',next_attempt_at=0,attempts=0,error_code=NULL "
                    "WHERE derived='committed'"
                )

    def retrieval_snapshot(self, query: str, known_at=None):
        # The directory has one owner and every connection uses this lock; keep both
        # reads together so a local writer cannot change FTS between them.
        with self.lock:
            revision, memories, vectors = self.snapshot(known_at=known_at)
            available = True
            try:
                ranks = self.keyword_ranks(query)
            except sqlite3.Error:
                ranks, available = {}, False
            with self.connection() as conn:
                jobs = conn.execute(
                    "SELECT derived,index_state,count(*) FROM write_jobs "
                    "GROUP BY derived,index_state"
                ).fetchall()
            coverage = {
                "bm25_available": available,
                "derived_pending": sum(r[2] for r in jobs if r[0] in {"pending", "extracting"}),
                "derived_failed": sum(r[2] for r in jobs if r[0] == "failed"),
                "index_pending": sum(
                    r[2] for r in jobs if r[0] == "committed" and r[1] != "indexed"
                ),
                "index_failed": sum(r[2] for r in jobs if r[0] == "committed" and r[1] == "failed"),
            }
            return revision, memories, vectors, ranks, coverage

    def rebuild_keywords(self):
        with self.connection() as conn:
            conn.execute(FTS_SCHEMA)
            conn.execute("DELETE FROM memory_fts")
            for row in conn.execute("SELECT body FROM memories").fetchall():
                memory = StoredMemory.model_validate_json(row[0])
                conn.execute(
                    "INSERT INTO memory_fts VALUES(?,?,?)",
                    (memory.memory_id, memory.layer, " ".join(terms(memory.text))),
                )

    def collection(self, time_range: TimeRange | None, status: str | None):
        if time_range is None:
            return {"complete": False, "reason": "needs_time_range", "items": []}
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT e.* FROM events e JOIN (SELECT request_id,max(sequence) seq FROM events "
                "WHERE event_type='task_result' AND occurred<? GROUP BY request_id) t ON e.sequence=t.seq "
                "WHERE e.occurred>=? ORDER BY e.occurred,e.sequence",
                (time_range.end.timestamp(), time_range.start.timestamp()),
            ).fetchall()
        items = []
        for row in rows:
            result = json.loads(row["task_result"])
            if status is not None and result.get("execution_status") != status:
                continue
            items.append(
                {
                    "event_id": row["event_id"],
                    "request_id": row["request_id"],
                    "run_id": result.get("run_id"),
                    "execution_status": result.get("execution_status"),
                    "output_complete": result.get("output_complete"),
                    "occurred_at": datetime.fromtimestamp(
                        row["occurred"], utcnow().tzinfo
                    ).isoformat(),
                }
            )
        return {
            "complete": True,
            "total_matched": len(items),
            "returned_count": min(len(items), 50),
            "items": items[:50],
            "display_complete": len(items) <= 50,
            "time_range": time_range.model_dump(mode="json"),
        }
