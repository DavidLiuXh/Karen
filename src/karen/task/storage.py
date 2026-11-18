"""Local CLI task checkpoints and immutable delivery snapshots."""

import fcntl
import json
import os
import sqlite3
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..intent import IntentSession
from ..model.response import committed_file_operations

MAX_FILE_BYTES = 1_048_576
MAX_DELIVERY_FILES = 32


class TaskStorageError(ValueError):
    pass


class TaskStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise OSError("Task storage root must be a directory")
        self.root.chmod(0o700)
        self.files = self.root / "deliveries"
        self.files.mkdir(mode=0o700, exist_ok=True)
        if self.files.is_symlink():
            raise OSError("Task deliveries root must be a directory")
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        self._lock = os.fdopen(os.open(self.root / "lock", flags, 0o600), "rb+")
        self.db = None
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / "tasks.sqlite3"
            if path.is_symlink():
                raise OSError("Task database must be a regular file")
            self.db = sqlite3.connect(path)
            path.chmod(0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    session TEXT NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS task_turns (
                    turn_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    turn_no INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    was_running INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT,
                    resume_session TEXT,
                    trace_id TEXT,
                    run_id TEXT,
                    error TEXT,
                    deliverables_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(task_id, turn_no)
                );
            """)
            with self.db:
                if "resume_session" not in {row["name"] for row in self.db.execute("PRAGMA table_info(task_turns)")}:
                    self.db.execute("ALTER TABLE task_turns ADD COLUMN resume_session TEXT")
                self.db.execute("""
                    UPDATE task_turns SET state='interrupted' WHERE state IN ('queued', 'running')
                """)
        except (OSError, sqlite3.Error):
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self._lock.close()

    def summaries(self):
        return self.db.execute("""
            SELECT t.task_id, t.title, t.closed, r.state, r.turn_no, r.run_id, r.error
            FROM tasks t JOIN task_turns r ON r.turn_id = (
                SELECT turn_id FROM task_turns WHERE task_id=t.task_id ORDER BY turn_no DESC LIMIT 1
            )
            ORDER BY t.rowid
        """).fetchall()

    def session(self, task_id):
        row = self.db.execute("SELECT session FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return IntentSession.model_validate_json(row["session"])

    def history(self, task_id):
        return self.db.execute(
            "SELECT * FROM task_turns WHERE task_id=? ORDER BY turn_no", (task_id,)
        )

    def latest_turn(self, task_id):
        return self.db.execute("""
            SELECT turn_id, text, received_at, state, was_running, run_id, trace_id
            FROM task_turns WHERE task_id=? ORDER BY turn_no DESC LIMIT 1
        """, (task_id,)).fetchone()

    def requeue(self, task_id, turn_id, *, run_id=None):
        with self.db:
            changed = self.db.execute("""
                UPDATE task_turns SET state='queued'
                WHERE task_id=? AND turn_id=? AND state='interrupted' AND (was_running=0 OR (run_id=? AND resume_session IS NOT NULL))
                  AND turn_no=(SELECT MAX(turn_no) FROM task_turns WHERE task_id=?)
            """, (task_id, turn_id, run_id, task_id))
            if changed.rowcount != 1:
                raise ValueError("TASK_NOT_WAITING")

    def link_run(self, task_id, turn_id, run_id, session, trace_id):
        with self.db:
            changed = self.db.execute("""
                UPDATE task_turns SET run_id=?, trace_id=?, request_id=?, resume_session=?
                WHERE task_id=? AND turn_id=? AND state='running' AND run_id IS NULL
            """, (run_id, trace_id, session.request_id, session.model_dump_json(), task_id, turn_id))
            if changed.rowcount != 1:
                raise TaskStorageError("TASK_RUN_LINK_FAILED")

    def execution_session(self, turn_id):
        row = self.db.execute(
            "SELECT resume_session FROM task_turns WHERE turn_id=?", (turn_id,)
        ).fetchone()
        return IntentSession.model_validate_json(row["resume_session"])

    def interrupted_suffix(self, task_id):
        return self.db.execute("""
            SELECT turn_id, text, was_running FROM task_turns
            WHERE task_id=? AND turn_no > COALESCE((
                SELECT MAX(turn_no) FROM task_turns WHERE task_id=? AND state!='interrupted'
            ), 0) ORDER BY turn_no
        """, (task_id, task_id))

    def enqueue(self, task_id, title, session, turn_id, text, received_at, turn_no):
        with self.db:
            if turn_no == 1:
                self.db.execute(
                    "INSERT INTO tasks(task_id, title, session) VALUES (?, ?, ?)",
                    (task_id, title, session.model_dump_json()),
                )
            self.db.execute("""
                INSERT INTO task_turns(turn_id, task_id, turn_no, text, received_at, state)
                VALUES (?, ?, ?, ?, ?, 'queued')
            """, (turn_id, task_id, turn_no, text, received_at.isoformat()))

    def start(self, turn_id):
        with self.db:
            self.db.execute(
                "UPDATE task_turns SET state='running', was_running=1 WHERE turn_id=?", (turn_id,)
            )

    def settle(self, task_id, turn_id, session, state, *, trace_id=None, run_id=None,
               error=None, deliverables=()):
        with self.db:
            self.db.execute("UPDATE tasks SET session=? WHERE task_id=?",
                            (session.model_dump_json(), task_id))
            self.db.execute("""
                UPDATE task_turns SET state=?,
                    request_id=CASE WHEN ?='interrupted' THEN request_id ELSE ? END,
                    resume_session=CASE WHEN ?='interrupted' THEN resume_session END,
                    trace_id=COALESCE(?, trace_id), run_id=COALESCE(?, run_id), error=?,
                    deliverables_json=? WHERE turn_id=?
            """, (state, state, session.request_id, state, trace_id, run_id, error,
                  json.dumps(deliverables, ensure_ascii=False), turn_id))

    def cancel_waiting(self, task_id, session):
        with self.db:
            self.db.execute("UPDATE tasks SET session=? WHERE task_id=?",
                            (session.model_dump_json(), task_id))
            self.db.execute("""
                UPDATE task_turns SET state='cancelled' WHERE turn_id=(
                    SELECT turn_id FROM task_turns WHERE task_id=? ORDER BY turn_no DESC LIMIT 1
                )
            """, (task_id,))

    def finish(self, task_id):
        with self.db:
            self.db.execute("UPDATE tasks SET closed=1 WHERE task_id=?", (task_id,))

    def snapshot(self, task_id, turn_id, result):
        """Copy explicitly returned files while the execution's write leases are held."""
        # Only accept identifiers generated by TaskManager for destination names.
        if len(task_id) != 8 or not all(c in "0123456789abcdef" for c in task_id):
            return ()
        if len(turn_id) != 32 or not all(c in "0123456789abcdef" for c in turn_id):
            return ()
        paths = result.outputs.get("files")
        if not isinstance(paths, list) or not paths:
            return ()
        written = set()
        for capability, output in committed_file_operations(result):
            if capability == "file.write_text@1.0.0" and type(output.get("bytes_written")) is int:
                written.add(output["path"])
        root = Path("/tmp").resolve()
        entries = []
        seen = set()
        for value in paths[:MAX_DELIVERY_FILES]:
            if not isinstance(value, str) or value in seen:
                continue
            seen.add(value)
            source = Path(value)
            name = source.name or "file"
            entry = {"kind": "file", "name": name, "available": False}
            try:
                if value in written and source.is_relative_to(root) and source.resolve() == source:
                    relative = Path(task_id) / turn_id / str(len(entries) + 1) / name
                    target = self.files / relative
                    if not target.is_file():
                        content = _read_delivery(root, source.relative_to(root))
                        if len(content) > MAX_FILE_BYTES:
                            raise OSError("delivery exceeds write limit")
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        _write_delivery(target, content)
                    entry.update(available=True, relative_path=str(relative), size_bytes=target.stat().st_size)
            except (OSError, ValueError, RuntimeError):
                pass
            entries.append(entry)
        if len(paths) > MAX_DELIVERY_FILES:
            entries.append({"kind": "file_limit"})
        return tuple(entries)


def _read_delivery(root, relative):
    """Open inside the permitted directory without following replaced path components."""
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(relative.name, flags, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("non-regular delivery")
            return stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(directory)


def _write_delivery(target, content):
    with NamedTemporaryFile(dir=target.parent, prefix=".delivery-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
