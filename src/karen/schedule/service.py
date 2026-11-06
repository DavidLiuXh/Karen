"""Durable schedules feeding the existing in-process task manager."""

import asyncio
import fcntl
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dynamic_graph import ModelRequest
from dynamic_graph.models.client import ModelCallError
from pydantic import ValidationError

from ..message import messages
from . import prompts
from .contracts import ScheduleCommand, ScheduledTask


class ScheduledTasks:
    def __init__(self, *, root_dir, manager, session, model, submit=None, clock=None):
        self.root = Path(root_dir)
        self.manager, self.session, self.model = manager, session.new_request(), model
        self.observer = manager.observer
        self.submit = submit or self._submit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.entries = {}
        self._origins = {}
        self.db = None
        self._lock = None
        self.runner = None

    async def start(self):
        if self.db is not None:
            raise RuntimeError("Schedules already started")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink():
                raise OSError("Schedule directory cannot be a symlink")
            os.chmod(self.root, 0o700)
            self._lock = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / "schedules.sqlite3"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.entries = {
                entry.schedule_id: entry for (value,) in self.db.execute("SELECT value FROM schedules")
                for entry in [ScheduledTask.model_validate_json(value)]
            }
            now = self.clock()
            for entry in self.entries.values():
                if entry.last_state in {"queued", "running", "waiting"}:
                    entry.last_state = "interrupted"
                if entry.next_run is not None and entry.next_run <= now:
                    entry.next_run = entry.rule.next_after(now, previous=entry.next_run)
                    entry.last_skip = "missed"
                self._save(entry)
            self.runner = asyncio.create_task(self._run(), name="karen-schedules")
        except BaseException:
            self.close()
            raise

    async def stop(self):
        if self.runner is not None:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def _save(self, entry):
        # Publish in-memory changes only after durable commit in command handlers.
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO schedules VALUES (?, ?)",
                            (entry.schedule_id, entry.model_dump_json()))

    def _submit(self, entry, now):
        session = self.session.new_request().model_copy(update={"timezone": entry.rule.timezone})
        return self.manager.submit(entry.prompt, session, received_at=now)

    def _busy(self, entry):
        task = self.manager.tasks.get(entry.last_task_id)
        return task is not None and not task.closed and task.state in {"queued", "running", "waiting"}

    def _launch(self, entry, now):
        task = self.submit(entry, now)
        entry.last_task_id, entry.last_state = task.task_id, task.state
        entry.last_skip = None
        self._save(entry)
        self._origins[task.task_id] = entry.model_copy(deep=True)
        self.observer.emit("schedule.triggered", data={
            "schedule_id": entry.schedule_id, "task_id": task.task_id,
        })
        return task

    def tick(self):
        now = self.clock()
        for entry in tuple(self.entries.values()):
            if entry.next_run is None or entry.next_run > now:
                continue
            # Claim before submission: a process death must never replay uncertain work.
            entry.next_run = entry.rule.next_after(now, previous=entry.next_run)
            self._save(entry)
            if self._busy(entry):
                entry.last_skip = "busy"
                self._save(entry)
                self.observer.emit("schedule.skipped", data={
                    "schedule_id": entry.schedule_id, "reason": "busy",
                })
                continue
            try:
                self._launch(entry, now)
            except ValueError as exc:
                entry.last_task_id = None
                entry.last_state = "rejected"
                self._save(entry)
                self.observer.emit("schedule.skipped", status="failed", data={
                    "schedule_id": entry.schedule_id, "reason": str(exc),
                })

    async def _run(self):
        while True:
            self.tick()
            await asyncio.sleep(1)

    def record_event(self, event):
        origin = self.for_task(event.task_id)
        entry = self.entries.get(origin.schedule_id) if origin is not None else None
        if entry is not None and entry.last_task_id == event.task_id:
            entry.last_state = event.state
            self._save(entry)

    def for_task(self, task_id):
        return self._origins.get(task_id)

    async def _parse(self, text):
        tokens = text.split()
        if not tokens or tokens == ["list"]:
            return ScheduleCommand(action="list")
        if len(tokens) == 2 and tokens[0] in {"delete", "run"}:
            return ScheduleCommand(action=tokens[0], target=tokens[1])
        now = self.clock()
        explicit_ids = {entry.schedule_id for entry in self.entries.values() if entry.schedule_id in text}
        data = {
            "user_input": text,
            "current_datetime": now.astimezone(ZoneInfo(self.session.timezone)).isoformat(),
            "default_timezone": self.session.timezone,
            "schedules": [{
                "id": e.schedule_id, "name": e.name, "rule": e.rule.model_dump(mode="json"),
                **({"prompt": e.prompt} if (e.schedule_id in explicit_ids or (
                    not explicit_ids and e.name.casefold() in text.casefold())) else {}),
            } for e in self.entries.values()],
        }
        if len(json.dumps(data, ensure_ascii=False).encode()) > 256 * 1024:
            raise ValueError("SCHEDULE_CONTEXT_TOO_LARGE")
        # A single bounded repair for invalid JSON/schema or truncation; no command
        # is applied until the complete response has passed validation.
        async with asyncio.timeout(60):
            deadline = asyncio.get_running_loop().time() + 60
            for attempt in range(2):
                try:
                    response = await self.model.generate(ModelRequest(
                        role="schedule_command", system_instruction=prompts.SYSTEM,
                        task_instruction=prompts.PARSE, input_data=data,
                        output_schema=ScheduleCommand.model_json_schema(),
                        timeout_seconds=max(0.01, deadline - asyncio.get_running_loop().time()),
                        max_output_tokens=4096 * (attempt + 1),
                    ))
                    command = ScheduleCommand.model_validate(response.payload)
                    if (command.action in {"update", "delete", "run"}
                            and command.target.casefold() not in text.casefold()):
                        raise ValueError("SCHEDULE_EXPLICIT_TARGET_REQUIRED")
                    return command
                except (ValidationError, ModelCallError) as exc:
                    if attempt or (isinstance(exc, ModelCallError) and exc.code not in {
                        "MODEL_RESPONSE_INVALID", "MODEL_RESPONSE_TRUNCATED",
                    }):
                        raise
                    data = {**data, "repair": "上次 JSON 或字段校验失败，请重新返回完整有效的命令。"}

    def _target(self, target):
        if target in self.entries:
            return self.entries[target]
        matches = [entry for entry in self.entries.values() if entry.name.casefold() == target.casefold()]
        if len(matches) != 1:
            raise ValueError("SCHEDULE_TARGET_AMBIGUOUS" if matches else "SCHEDULE_NOT_FOUND")
        return matches[0]

    def _describe(self, entry):
        rule = entry.rule
        if rule.kind == "once":
            timing = rule.at.astimezone(ZoneInfo(rule.timezone)).isoformat(timespec="minutes")
        elif rule.kind == "interval":
            timing = messages.SCHEDULE_INTERVAL.format(minutes=rule.interval_seconds / 60)
        elif rule.kind == "weekly":
            timing = messages.SCHEDULE_WEEKLY.format(day="一二三四五六日"[rule.weekday], time=rule.time)
        else:
            timing = messages.SCHEDULE_DAILY.format(time=rule.time)
        next_time = (entry.next_run.astimezone(ZoneInfo(rule.timezone)).isoformat(timespec="seconds")
                     if entry.next_run else messages.SCHEDULE_NO_NEXT)
        active = self.manager.tasks.get(entry.last_task_id)
        state = active.state if active else entry.last_state
        state = messages.TASK_STATE_LABELS.get(state, messages.SCHEDULE_STATE_LABELS.get(state, state))
        return messages.SCHEDULE_ITEM.format(
            schedule_id=entry.schedule_id, name=entry.name, timing=timing,
            timezone=rule.timezone, next_time=next_time, state=state,
            task_id=entry.last_task_id or "—", prompt=entry.prompt,
            skipped=messages.SCHEDULE_SKIP_LABELS.get(entry.last_skip, "—"),
        )

    async def handle(self, text):
        parts = text.strip().split(maxsplit=1)
        if not parts or parts[0] != "/schedule":
            return None
        body = parts[1] if len(parts) > 1 else ""
        if self.db is None:
            return messages.SCHEDULE_UNAVAILABLE
        if len(body.encode()) > 16384:
            return messages.SCHEDULE_FAILED.format(code="SCHEDULE_INPUT_TOO_LARGE")
        try:
            parsed = await self._parse(body.strip())
            return self.apply(parsed)
        except (ValidationError, ModelCallError, TimeoutError, ValueError, ZoneInfoNotFoundError) as exc:
            code = exc.code if isinstance(exc, ModelCallError) else (
                str(exc) if type(exc) is ValueError else type(exc).__name__)
            return messages.SCHEDULE_ERRORS.get(code, messages.SCHEDULE_FAILED.format(code=code))

    def apply(self, command):
        now = self.clock()
        if command.action == "clarify":
            return messages.SCHEDULE_CLARIFY.format(question=command.question)
        if command.action == "list":
            return "\n\n".join(self._describe(e) for e in self.entries.values()) or messages.SCHEDULE_EMPTY
        if command.action == "create":
            if len(self.entries) >= 100:
                raise ValueError("SCHEDULE_LIMIT")
            next_run = command.rule.next_after(now)
            if next_run is None:
                raise ValueError("SCHEDULE_PAST_TIME")
            entry = ScheduledTask(schedule_id=uuid4().hex[:8], name=command.name,
                                  prompt=command.prompt, rule=command.rule, next_run=next_run)
            while entry.schedule_id in self.entries:
                entry.schedule_id = uuid4().hex[:8]
            self._save(entry)
            self.entries[entry.schedule_id] = entry
            self.observer.emit("schedule.created", data={"schedule_id": entry.schedule_id})
            return messages.SCHEDULE_CREATED + "\n" + self._describe(entry)
        entry = self._target(command.target)
        if command.action == "delete":
            with self.db:
                self.db.execute("DELETE FROM schedules WHERE id=?", (entry.schedule_id,))
            del self.entries[entry.schedule_id]
            self.observer.emit("schedule.deleted", data={"schedule_id": entry.schedule_id})
            return messages.SCHEDULE_DELETED.format(schedule_id=entry.schedule_id)
        if command.action == "run":
            if self._busy(entry):
                raise ValueError("SCHEDULE_BUSY")
            task = self._launch(entry, now)
            return messages.SCHEDULE_TRIGGERED.format(schedule_id=entry.schedule_id, task_id=task.task_id)
        updated = entry.model_copy(deep=True)
        for key in ("name", "prompt", "rule"):
            value = getattr(command, key)
            if value is not None:
                setattr(updated, key, value)
        if command.rule is not None:
            updated.next_run = command.rule.next_after(now)
            if updated.next_run is None:
                raise ValueError("SCHEDULE_PAST_TIME")
            updated.last_skip = None
        self._save(updated)
        self.entries[entry.schedule_id] = updated
        self.observer.emit("schedule.updated", data={"schedule_id": entry.schedule_id})
        return messages.SCHEDULE_UPDATED + "\n" + self._describe(updated)
