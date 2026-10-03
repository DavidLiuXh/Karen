"""Read-only loopback inspector for Karen traces and existing engine records."""

from __future__ import annotations

import argparse
import json
import os
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .checks import check_trace
from .recorder import sanitize

IDENTIFIER = re.compile(r"[a-f0-9]{32}")


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
        tasks, gaps, total_bytes = [], [], 0
        for path in paths:
            if total_bytes >= 8 * 1024 * 1024:
                limited = True
                break
            events, coverage = self._events(path, 128 * 1024)
            if coverage["partial"]:
                tail, _ = self._events(path, 64 * 1024, tail=True)
                events.extend(tail)
            total_bytes += min(path.stat().st_size, 128 * 1024)
            roots = [e for e in events if e.get("stage") == "turn"]
            started = next((e for e in roots if e.get("event_type") == "span.started"), None)
            if not started:
                continue
            finished = next(
                (e for e in reversed(roots) if e.get("event_type") == "span.finished"), None
            )
            request = next((e for e in events if e.get("event_type") == "turn.input"), {})
            tasks.append(
                {
                    "trace_id": path.stem,
                    "request_id": started.get("request_id"),
                    "conversation_id": started.get("conversation_id"),
                    "turn_id": started.get("turn_id"),
                    "timestamp_utc": started["timestamp_utc"],
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
        return {"tasks": tasks, "coverage": {"partial": limited, "gaps": gaps}}

    def trace(self, trace_id):
        if not IDENTIFIER.fullmatch(trace_id):
            raise ValueError("INVALID_TRACE_ID")
        path = self.root_dir / "traces" / f"{trace_id}.jsonl"
        events, coverage = self._events(path)
        root = next((e for e in events if e.get("stage") == "turn"), None)
        if root is None:
            return {"events": events, "coverage": coverage, "runs": []}
        request_id = root.get("request_id")
        paths, partial = self._files()
        scanned = 0
        for other in paths:
            if other == path:
                continue
            if scanned >= 8 * 1024 * 1024:
                partial = True
                break
            initial, _ = self._events(other, 16 * 1024)
            if initial and initial[0].get("request_id") == request_id:
                extra, state = self._events(other, 512 * 1024)
                scanned += min(other.stat().st_size, 512 * 1024)
                # Include all clarification turns and their independently timed background work.
                events.extend(extra)
                coverage["partial"] |= state["partial"]
                coverage["invalid_lines"] += state["invalid_lines"]
                coverage["unfinished_tail"] |= state.get("unfinished_tail", False)
            else:
                scanned += min(other.stat().st_size, 16 * 1024)
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


def create_server(root_dir, *, port=8765):
    store = TraceStore(root_dir)
    asset = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
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


def main():
    parser = argparse.ArgumentParser(description="Karen 本地运行观测页面（只读）")
    parser.add_argument("--root", type=Path, default=Path.home() / ".Karne" / "observability")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="用本机浏览器打开页面")
    args = parser.parse_args()
    with create_server(args.root, port=args.port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Karen 观测页面：{url}", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
