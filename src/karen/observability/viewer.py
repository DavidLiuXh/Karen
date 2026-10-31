"""Read-only loopback inspector for Karen traces and existing engine records."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import messages
from .checks import check_trace
from .recorder import sanitize

IDENTIFIER = re.compile(r"[a-f0-9]{32}")
DEFAULT_PORT = 8765


class TraceStore:
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir).expanduser().absolute()
        self.runs_dir = self.root_dir.parent / "runs"

    def _read(self, path, limit, *, tail=False):
        # Only observation files and the sibling engine recording directory are readable.
        path = Path(path)
        resolved = path.resolve()
        allowed = next(
            (
                root
                for root in (self.root_dir, self.runs_dir)
                if resolved.is_relative_to(root.resolve())
            ),
            None,
        )
        if allowed is None:
            raise ValueError("OUTSIDE_OBSERVATION_ROOT")
        if any(
            p.is_symlink()
            for p in [path, *path.parents]
            if p == allowed or p.is_relative_to(allowed)
        ):
            raise ValueError("SYMLINK_RECORD")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if tail:
                size = os.fstat(handle.fileno()).st_size
                if size > limit:
                    handle.seek(size - limit)
                    data = handle.read(limit).split(b"\n", 1)
                    return data[1] if len(data) == 2 else b"", True
            data = handle.read(limit + 1)
        return data[:limit], len(data) > limit

    def _json(self, path, limit=1024 * 1024):
        try:
            raw, partial = self._read(path, limit)
            if partial:
                return {"unavailable": "file_budget_limit", "partial": True}
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError("Invalid document")
            return sanitize(document)
        except (OSError, ValueError):
            return {"unavailable": "missing_or_invalid_record"}

    def _events(self, path, limit=2 * 1024 * 1024, *, tail=False):
        try:
            raw, partial = self._read(path, limit, tail=tail)
        except (OSError, ValueError):
            return [], {"unavailable": True, "partial": False, "invalid_lines": 0}
        events, invalid, unfinished = [], 0, False
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith(b"\n") and index == len(lines) - 1:
                unfinished = True
                continue  # A concurrent append or interrupted final line is not a terminal event.
            try:
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or not isinstance(event.get("event_type"), str)
                    or not isinstance(event.get("timestamp_utc"), str)
                    or ("data" in event and not isinstance(event["data"], dict))
                ):
                    raise ValueError("Invalid event")
                events.append(sanitize(event))
            except (ValueError, UnicodeDecodeError):
                invalid += 1
        return events, {"partial": partial, "invalid_lines": invalid, "unfinished_tail": unfinished}

    def _files(self):
        directory = self.root_dir / "traces"
        if not directory.exists() or directory.is_symlink():
            return [], False
        paths = [
            p
            for p in directory.glob("*.jsonl")
            if IDENTIFIER.fullmatch(p.stem) and not p.is_symlink()
        ]
        paths.sort(key=lambda p: p.stat().st_mtime if not p.is_symlink() else 0, reverse=True)
        return paths[:300], len(paths) > 300

    def tasks(self):
        paths, limited = self._files()
        tasks, gaps, channel_events, workflow_events, total_bytes = [], [], [], [], 0
        for path in paths:
            if total_bytes >= 8 * 1024 * 1024:
                limited = True
                break
            events, coverage = self._events(path, 128 * 1024)
            if coverage["partial"]:
                tail, _ = self._events(path, 64 * 1024, tail=True)
                events.extend(tail)
            total_bytes += min(path.stat().st_size, 128 * 1024)
            channel_events.extend(e for e in events if e["event_type"].startswith("weixin."))
            workflow_events.extend(
                e
                for e in events
                if e.get("stage", "").startswith("workflow.")
                or e["event_type"].startswith("workflow.")
            )
            roots = [e for e in events if e.get("stage") in {"input", "turn"}]
            started = next(
                (
                    e
                    for e in roots
                    if e.get("stage") == "turn" and e.get("event_type") == "span.started"
                ),
                None,
            )
            started = started or next(
                (e for e in roots if e.get("event_type") == "span.started"), None
            )
            if not started:
                continue
            finished = next(
                (e for e in reversed(roots) if e.get("event_type") == "span.finished"), None
            )
            request = next(
                (e for e in events if e.get("event_type") in {"turn.input", "input.received"}), {}
            )
            if (
                not request
                and not finished
                and started["stage"] == "input"
                and not coverage["invalid_lines"]
                and not coverage["unfinished_tail"]
            ):
                # The asynchronous writer can expose span.started before input.received.
                # Wait for the input record instead of publishing a transient blank task.
                continue
            tasks.append(
                {
                    "trace_id": path.stem,
                    "request_id": started.get("request_id"),
                    "conversation_id": started.get("conversation_id"),
                    "turn_id": started.get("turn_id"),
                    "timestamp_utc": roots[0]["timestamp_utc"],
                    "input": request.get("data", {}).get("text", ""),
                    "status": finished.get("status") if finished else "nonterminal",
                    "outcome": finished.get("data", {}).get("outcome") if finished else None,
                    "duration_ms": finished.get("duration_ms") if finished else None,
                    "stage": events[-1].get("stage") if events else None,
                    "degraded": any(e.get("status") == "degraded" for e in events),
                    "coverage": coverage,
                }
            )
        directory = self.root_dir / "traces"
        for path in list(directory.glob("*.health.json"))[:300] if directory.exists() else []:
            health = self._json(path, 4096)
            if health.get("dropped") or health.get("write_failures"):
                gaps.append(health)
        tasks.sort(key=lambda t: t["timestamp_utc"], reverse=True)
        channel_events.sort(key=lambda e: e["timestamp_utc"], reverse=True)
        return {
            "tasks": tasks,
            "channel_events": channel_events[:20],
            "workflows": self.workflows(workflow_events),
            "coverage": {"partial": limited, "gaps": gaps},
        }

    def workflows(self, events):
        jobs = {}
        for event in sorted(events, key=lambda e: (e["timestamp_utc"], e.get("seq", 0))):
            data = event.get("data", {})
            job_id = event.get("job_id") or data.get("job_id")
            if not job_id:
                continue
            job = jobs.setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "source_trace_id": event.get("source_trace_id") or event.get("trace_id"),
                },
            )
            job["timestamp_utc"] = event["timestamp_utc"]
            job["process_session_id"] = event.get("process_session_id")
            if event.get("stage") == "workflow.job":
                job["trace_id"] = event.get("trace_id")
                if event["event_type"] == "span.finished":
                    job["state"] = data.get(
                        "state", "cancelled" if event.get("status") == "cancelled" else "failed"
                    )
                    job["duration_ms"] = event.get("duration_ms")
            for field in ("state", "pid", "queue_ms", "reason"):
                if field in data:
                    job[field] = data[field]
        for job in jobs.values():
            if job.get("state") in {"queued", "running"}:
                try:
                    pid = int(job.get("pid", 0))
                    if pid <= 0:
                        raise ValueError("unknown_process")
                    session = job.get("process_session_id", "")
                    if (
                        IDENTIFIER.fullmatch(session or "")
                        and (self.root_dir / "traces" / f"{session}.health.json").exists()
                    ):
                        raise ValueError("closed_process")
                    os.kill(pid, 0)
                except (OSError, ValueError):
                    job["state"] = "interrupted_unknown"
        root = self.root_dir.parent / "workflows"
        index = root / "index.sqlite"
        definitions, coverage = [], {}
        if index.exists():
            try:
                if root.is_symlink() or index.is_symlink():
                    raise ValueError("SYMLINK_WORKFLOW_INDEX")
                with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
                    db.row_factory = sqlite3.Row
                    definitions = [
                        dict(row)
                        for row in db.execute(
                            "SELECT workflow_id,version,status,name,created_at,uses,last_used,source_run_id "
                            "FROM workflows ORDER BY created_at DESC LIMIT 101"
                        )
                    ]
                coverage["partial"] = len(definitions) > 100
                definitions = sanitize(definitions[:100])
            except (OSError, ValueError, sqlite3.Error):
                coverage["unavailable"] = True
        return {
            "jobs": list(reversed(list(jobs.values())))[:100],
            "definitions": definitions,
            "coverage": coverage,
        }

    def trace(self, trace_id):
        if not IDENTIFIER.fullmatch(trace_id):
            raise ValueError("INVALID_TRACE_ID")
        path = self.root_dir / "traces" / f"{trace_id}.jsonl"
        events, coverage = self._events(path)
        root = next((e for e in events if e.get("stage") == "turn"), None)
        if root is None:
            return {
                "trace_id": trace_id,
                "request_id": None,
                "events": events,
                "coverage": coverage,
                "runs": [],
                "checks": check_trace(events, coverage),
            }
        request_id = root.get("request_id")
        paths, partial = self._files()
        scanned = 0
        for other in paths:
            if other == path:
                continue
            if scanned >= 8 * 1024 * 1024:
                partial = True
                break
            initial, _ = self._events(other, 128 * 1024)
            other_root = next((e for e in initial if e.get("stage") == "turn"), None)
            if other_root is None and initial and initial[0].get("stage") != "input":
                other_root = initial[0]
            if other_root and (
                other_root.get("request_id") == request_id
                or other_root.get("source_trace_id") == trace_id
            ):
                extra, state = self._events(other, 512 * 1024)
                scanned += min(other.stat().st_size, 512 * 1024)
                # Include all clarification turns and their independently timed background work.
                events.extend(extra)
                coverage["partial"] |= state["partial"]
                coverage["invalid_lines"] += state["invalid_lines"]
                coverage["unfinished_tail"] |= state.get("unfinished_tail", False)
            else:
                scanned += min(other.stat().st_size, 128 * 1024)
        coverage["partial"] |= partial
        sessions = {e.get("process_session_id") for e in events}
        coverage["process_health"] = [
            self._json(self.root_dir / "traces" / f"{sid}.health.json", 4096)
            for sid in sessions
            if isinstance(sid, str) and IDENTIFIER.fullmatch(sid)
        ]
        coverage["dropped"] = max(
            (e.get("coverage", {}).get("dropped", 0) for e in events), default=0
        )
        coverage["write_failures"] = max(
            (e.get("coverage", {}).get("write_failures", 0) for e in events), default=0
        )
        runs, engine_coverage = self._runs(request_id)
        coverage["engine"] = engine_coverage
        for run in runs:
            for event in run.pop("events", []):
                events.append(
                    {
                        **event,
                        "origin": "engine",
                        "stage": "execution." + event.get("phase", "unknown"),
                        "data": {
                            k: v
                            for k, v in event.items()
                            if k not in {"timestamp_utc", "event_type"}
                        },
                    }
                )
        events.sort(key=lambda e: (e.get("timestamp_utc", ""), e.get("seq", 0)))
        return {
            "trace_id": trace_id,
            "request_id": request_id,
            "events": events,
            "coverage": coverage,
            "runs": runs,
            "checks": check_trace(events, coverage),
        }

    def _runs(self, request_id):
        directory = self.runs_dir
        if not directory.exists() or directory.is_symlink():
            return [], {"available": False, "reason": "observation_runs_directory_unavailable"}
        paths = [
            p for p in directory.iterdir() if IDENTIFIER.fullmatch(p.name) and not p.is_symlink()
        ]
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        found = []
        scanned, read_bytes, limited = 0, 0, len(paths) > 300
        for path in paths[:300]:
            if read_bytes >= 8 * 1024 * 1024 or len(found) >= 8:
                limited = True
                break
            scanned += 1
            try:
                read_bytes += min((path / "goal.json").stat().st_size, 1024 * 1024)
            except OSError:
                pass
            goal = self._json(path / "goal.json")
            if goal.get("request_id") != request_id:
                continue
            manifest = self._json(path / "manifest.json")
            events, coverage = self._events(path / "events.jsonl")
            graph = self._json(path / "graph.json")
            # Only outputs actually referenced by recorded node-return events are read.
            artifacts = {}
            for event in events:
                ref = event.get("payload_ref")
                if isinstance(ref, str) and re.fullmatch(r"artifacts/[A-Za-z0-9_.-]+\.json", ref):
                    if len(artifacts) < 30:
                        artifacts[ref] = self._json(path / ref, 64 * 1024)
            found.append(
                {
                    "run_id": path.name,
                    "manifest": manifest,
                    "events": events,
                    "goal": goal,
                    "graph": graph,
                    "policy": self._json(path / "policy.json", 64 * 1024),
                    "result": self._json(path / "result.json")
                    if manifest.get("terminal")
                    else None,
                    "artifacts": artifacts,
                    "coverage": coverage,
                }
            )
        return found, {
            "available": True,
            "partial": limited,
            "scanned_runs": scanned,
        }


def create_server(root_dir, *, port=DEFAULT_PORT):
    store = TraceStore(root_dir)
    asset = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # Browsers can cancel polling requests during refresh or navigation.
                # Cover headers and error responses as well as successful bodies.
                self.close_connection = True

        def do_GET(self):
            host = self.headers.get("Host", "")
            if host not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/":
                    body, mime = asset, "text/html; charset=utf-8"
                elif url.path == "/api/tasks":
                    body, mime = (
                        json.dumps(store.tasks(), ensure_ascii=False).encode(),
                        "application/json; charset=utf-8",
                    )
                elif url.path == "/api/trace":
                    query = parse_qs(url.query)
                    body, mime = (
                        json.dumps(
                            store.trace(query.get("id", [""])[0]), ensure_ascii=False
                        ).encode(),
                        "application/json; charset=utf-8",
                    )
                else:
                    self.send_error(404)
                    return
            except ValueError:
                self.send_error(400)
                return
            except OSError:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


@contextmanager
def background_server(root_dir, *, port=DEFAULT_PORT):
    """Serve alongside the conversation and release the listener on every exit path."""
    with create_server(root_dir, port=port) as server:
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="karen-observation-server",
            daemon=True,
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=messages.OBSERVATION_CLI_DESCRIPTION)
    parser.add_argument("--root", type=Path, default=Path.home() / ".Karne" / "observability")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help=messages.OBSERVATION_OPEN_HELP)
    args = parser.parse_args()
    with create_server(args.root, port=args.port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(messages.OBSERVATION_URL.format(url=url), flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
