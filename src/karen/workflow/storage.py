"""Immutable definitions published through a small authoritative SQLite index."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .contracts import Definition


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def terms(text):
    return " ".join(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower()))


class Storage:
    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.root / "index.sqlite", timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("symlink_workflow_root")
        os.chmod(self.root, 0o700)
        index = self.root / "index.sqlite"
        if index.is_symlink():
            raise ValueError("symlink_workflow_index")
        fd = os.open(index, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT, version INTEGER, status TEXT, path TEXT, digest TEXT,
                    source_run_id TEXT, source_graph_hash TEXT, name TEXT,
                    created_at TEXT, uses INTEGER DEFAULT 0, last_used TEXT,
                    PRIMARY KEY(workflow_id,version), UNIQUE(source_run_id,source_graph_hash));
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(workflow_id UNINDEXED, text);
                CREATE TABLE IF NOT EXISTS outcomes (
                    request_id TEXT, run_id TEXT, workflow_id TEXT, version INTEGER,
                    result TEXT, PRIMARY KEY(request_id,run_id));
            """)

    def known_source(self, run_id, graph_hash):
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM workflows WHERE source_run_id=? AND source_graph_hash=?",
                    (run_id, graph_hash),
                ).fetchone()
                is not None
            )

    def save(self, definition, status):
        directory = self.root / "definitions" / definition.workflow_id
        if directory.parent.is_symlink():
            raise ValueError("symlink_definition")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(p.is_symlink() for p in (directory, directory.parent)):
            raise ValueError("symlink_definition")
        path = directory / f"{definition.version}.json"
        data = encode(definition.model_dump(mode="json", by_alias=True))
        digest = hashlib.sha256(data).hexdigest()
        # Never overwrite an immutable definition.
        temporary = directory / (uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic exclusive publication: never replace an existing version.
            os.link(temporary, path)
            temporary.unlink()
            parent_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            relative = str(path.relative_to(self.root))
            with self.connection() as db:
                db.execute(
                    "INSERT INTO workflows(workflow_id,version,status,path,digest,source_run_id,source_graph_hash,name,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        definition.workflow_id,
                        definition.version,
                        status,
                        relative,
                        digest,
                        definition.source["run_id"],
                        definition.source["graph_hash"],
                        definition.proposal.name,
                        definition.created_at.isoformat(),
                    ),
                )
                description = " ".join(
                    [definition.proposal.purpose, *definition.proposal.applicability]
                )
                db.execute(
                    "INSERT INTO search(workflow_id,text) VALUES(?,?)",
                    (definition.workflow_id, terms(description)),
                )
            return {"path": relative, "digest": digest, "status": status}
        finally:
            temporary.unlink(missing_ok=True)
            # On an index failure the complete file is an unpublished orphan.
            # Loading only through the index prevents its accidental activation.

    def load(self, workflow_id, version=None):
        with self.connection() as db:
            row = db.execute(
                'SELECT * FROM workflows WHERE workflow_id=? AND status="enabled" AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1',
                (workflow_id, version, version),
            ).fetchone()
        if row is None:
            raise ValueError("workflow_unavailable")
        path = self.root / row["path"]
        if not path.resolve().is_relative_to(self.root.resolve()) or any(
            p.is_symlink() for p in (path, path.parent, path.parent.parent)
        ):
            raise ValueError("invalid_definition_path")
        with path.open("rb") as handle:
            data = handle.read(512 * 1024 + 1)
        if len(data) > 512 * 1024 or hashlib.sha256(data).hexdigest() != row["digest"]:
            raise ValueError("definition_hash_mismatch")
        definition = Definition.model_validate_json(data)
        if definition.workflow_id != workflow_id or definition.version != row["version"]:
            raise ValueError("definition_identity_mismatch")
        return definition

    def candidates(self, text):
        tokens = list(dict.fromkeys(terms(text).split()))[:100]
        if not tokens:
            return []
        query = " OR ".join('"' + token + '"' for token in tokens)
        with self.connection() as db:
            rows = db.execute(
                "SELECT search.workflow_id FROM search JOIN workflows w ON w.workflow_id=search.workflow_id "
                "WHERE search MATCH ? AND w.status='enabled' ORDER BY search.rank LIMIT 5",
                (query,),
            ).fetchall()
            if not rows:
                # Bounded semantic fallback handles synonyms and language changes.
                rows = db.execute(
                    "SELECT workflow_id FROM workflows WHERE status='enabled' ORDER BY uses DESC,created_at DESC LIMIT 5"
                ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.load(row["workflow_id"]))
            except (ValueError, OSError):
                continue
        return results

    def outcome(self, outcome):
        with self.connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?)",
                (
                    outcome["request_id"],
                    outcome["run_id"],
                    outcome["workflow_id"],
                    outcome["version"],
                    json.dumps(outcome),
                ),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE workflows SET uses=uses+1,last_used=? WHERE workflow_id=? AND version=?",
                    (outcome["recorded_at"], outcome["workflow_id"], outcome["version"]),
                )
            if outcome.get("pause"):
                db.execute(
                    'UPDATE workflows SET status="paused" WHERE workflow_id=? AND version=?',
                    (outcome["workflow_id"], outcome["version"]),
                )

    def pause(self, workflow_id, version):
        with self.connection() as db:
            db.execute(
                'UPDATE workflows SET status="paused" WHERE workflow_id=? AND version=?',
                (workflow_id, version),
            )

    def status_counts(self):
        with self.connection() as db:
            return {
                row[0]: row[1]
                for row in db.execute("SELECT status,count(*) FROM workflows GROUP BY status")
            }
