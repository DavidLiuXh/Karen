"""Transactional inbox, conversation checkpoint and transport-only outbox."""

import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from uuid import uuid4

from ...intent import IntentSession
from .api import WeixinError
from .auth import acquire_lock, private_root


def text_items(text):
    """Keep each text request below 4 KB without breaking Unicode characters."""
    part, size = [], 0
    for character in text:
        length = len(character.encode())
        if size + length > 4000:
            yield {"type": 1, "text_item": {"text": "".join(part)}}
            part, size = [], 0
        part.append(character)
        size += length
    if part:
        yield {"type": 1, "text_item": {"text": "".join(part)}}


class WeixinStore:
    def __init__(self, root, credentials):
        root = private_root(root)
        self._lock = None
        self.db = None
        try:
            self._lock = acquire_lock(root)
            self.root = private_root(root / hashlib.sha256(credentials.account_id.encode()).hexdigest())
            path = self.root / "state.sqlite3"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                    message TEXT NOT NULL, received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    response TEXT, session TEXT, trace_id TEXT,
                    record_response INTEGER NOT NULL DEFAULT 0,
                    response_recorded INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status, id);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, inbox_id INTEGER NOT NULL,
                    client_id TEXT NOT NULL UNIQUE, item TEXT, file_path TEXT, file_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, id);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL
                );
            """)
            self._set("owner", credentials.owner_id, initialize=True)
            if self._get("owner") != credentials.owner_id:
                raise WeixinError("WEIXIN_OWNER_CHANGED")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _get(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key, value, *, initialize=False):
        command = "INSERT OR IGNORE" if initialize else "INSERT OR REPLACE"
        self.db.execute(f"{command} INTO state(key, value) VALUES (?, ?)", (key, value))
        if initialize:
            self.db.commit()

    @property
    def cursor(self):
        return self._get("cursor") or ""

    def session(self, initial):
        value = self._get("session")
        return IntentSession.model_validate_json(value) if value else initial

    def accept(self, batch, owner):
        messages = batch.get("msgs", [])
        cursor = batch.get("get_updates_buf")
        if not isinstance(messages, list) or len(messages) > 200:
            raise WeixinError("WEIXIN_BATCH_INVALID")
        if cursor is not None and not isinstance(cursor, str):
            raise WeixinError("WEIXIN_CURSOR_INVALID")
        accepted, ignored = 0, 0
        with self.db:
            if self.db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0] > 1000:
                raise WeixinError("WEIXIN_INBOX_FULL", retryable=True)
            for message in messages:
                if not isinstance(message, dict) or (
                    message.get("from_user_id") != owner or message.get("group_id")
                    or message.get("message_type") != 1 or message.get("message_state") != 2
                    or not message.get("context_token")
                    or not isinstance(message.get("context_token"), str)
                    or not isinstance(message.get("message_id"), (int, str))
                    or isinstance(message.get("message_id"), bool)
                    or not str(message.get("message_id", ""))
                    or not isinstance(message.get("item_list"), list)
                    or not message["item_list"] or len(message["item_list"]) > 20
                ):
                    ignored += 1
                    continue
                received_at = datetime.now(UTC)
                milliseconds = message.get("create_time_ms")
                if type(milliseconds) is int and 0 < milliseconds < 253402300799000:
                    received_at = datetime.fromtimestamp(milliseconds / 1000, UTC)
                result = self.db.execute(
                    "INSERT OR IGNORE INTO inbox(message_id, message, received_at) VALUES (?, ?, ?)",
                    (str(message["message_id"]), json.dumps(message, ensure_ascii=False),
                     received_at.isoformat()),
                )
                accepted += result.rowcount
            if cursor:
                self._set("cursor", cursor)
        return accepted, ignored

    def next_input(self):
        with self.db:
            row = self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE inbox SET status='running' WHERE id=?", (row["id"],))
        return row

    def recover(self, initial):
        # Execution cannot be made exactly-once across process death. Fail closed instead of replay.
        rows = self.db.execute("SELECT * FROM inbox WHERE status='running' ORDER BY id").fetchall()
        for row in rows:
            self.finish(row, self.session(initial),
                        "上次处理因进程中断而停止，无法确认任务是否完成。为避免重复操作，"
                        "我没有自动重做；请先核对结果，再决定是否重新发起任务。", record=False)
        return len(rows)

    def prepare_file(self, run_id, artifact_id, path, name):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?)",
                            (artifact_id, run_id, path, name))

    def files_for(self, run_id):
        return self.db.execute("SELECT * FROM files WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def finish(self, row, session, text, *, files=(), trace_id=None, record=True):
        serialized = session.model_dump_json()
        with self.db:
            self._set("session", serialized)
            self.db.execute(
                "UPDATE inbox SET status='done', response=?, session=?, trace_id=?, record_response=? WHERE id=?",
                (text, serialized, trace_id, int(record and bool(text)), row["id"]),
            )
            for item in text_items(text):
                self.db.execute(
                    "INSERT INTO outbox(inbox_id, client_id, item) VALUES (?, ?, ?)",
                    (row["id"], uuid4().hex, json.dumps(item, ensure_ascii=False)),
                )
            for file in files:
                self.db.execute(
                    "INSERT INTO outbox(inbox_id, client_id, file_path, file_name) VALUES (?, ?, ?, ?)",
                    (row["id"], uuid4().hex, file["path"], file["name"]),
                )

    def next_output(self):
        # Preserve chunk/file ordering even while the head is backing off.
        row = self.db.execute("""
            SELECT o.*, i.message, i.trace_id FROM outbox o JOIN inbox i ON i.id=o.inbox_id
            WHERE o.status='pending' AND NOT EXISTS (
                SELECT 1 FROM outbox earlier WHERE earlier.inbox_id=o.inbox_id
                AND earlier.id<o.id AND earlier.status!='sent'
            ) ORDER BY o.id LIMIT 1
        """).fetchone()
        return row if row and row["next_attempt"] <= time.time() else None

    def progress(self, row):
        with self.db:
            self.db.execute(
                "INSERT INTO outbox(inbox_id, client_id, item) VALUES (?, ?, ?)",
                (row["id"], uuid4().hex, json.dumps({
                    "type": 1, "text_item": {"text": "已收到，任务仍在处理中，完成后我会回复。"},
                }, ensure_ascii=False)),
            )

    def uploaded(self, row, item):
        with self.db:
            self.db.execute("UPDATE outbox SET item=? WHERE id=?", (json.dumps(item), row["id"]))

    def sent(self, row):
        with self.db:
            self.db.execute("UPDATE outbox SET status='sent', error_code=NULL WHERE id=?", (row["id"],))

    def failed(self, row, error):
        attempts = row["attempts"] + 1
        status = "pending" if error.retryable and attempts < 5 else "failed"
        with self.db:
            self.db.execute("""
                UPDATE outbox SET status=?, attempts=?, next_attempt=?, error_code=? WHERE id=?
            """, (status, attempts, time.time() + min(30, 2 ** attempts), error.code, row["id"]))
        return status

    def delivered_responses(self):
        return self.db.execute("""
            SELECT i.* FROM inbox i WHERE i.record_response=1 AND i.response_recorded=0
            AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.inbox_id=i.id
                            AND o.file_path IS NULL AND o.status!='sent')
        """).fetchall()

    def recorded(self, row):
        with self.db:
            self.db.execute("UPDATE inbox SET response_recorded=1 WHERE id=?", (row["id"],))

    def retry_failed(self, context_token):
        with self.db:
            rows = self.db.execute("""
                SELECT DISTINCT i.id, i.message FROM inbox i JOIN outbox o ON o.inbox_id=i.id
                WHERE o.status='failed'
            """).fetchall()
            for row in rows:
                message = json.loads(row["message"])
                message["context_token"] = context_token
                self.db.execute("UPDATE inbox SET message=? WHERE id=?", (json.dumps(message), row["id"]))
            return self.db.execute("""
                UPDATE outbox SET status='pending', attempts=0, next_attempt=0 WHERE status='failed'
            """).rowcount

    def counts(self):
        result = {}
        for table in ("inbox", "outbox"):
            result[table] = {row[0]: row[1] for row in self.db.execute(
                f"SELECT status, count(*) FROM {table} GROUP BY status"
            )}
        return result
