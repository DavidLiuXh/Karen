"""Independent task sessions, bounded admission, cancellation and turn events."""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from dynamic_graph import CancellationToken

from ..intent import IntentSession
from ..intent.recognizer import Message
from ..message import messages
from ..model.response import format_task_event
from ..observability import Observer
from .storage import TaskStorageError

DEFAULT_MAX_CONCURRENT_TASKS = 2


@dataclass(frozen=True)
class TaskRound:
    turn_id: str
    text: str
    received_at: datetime
    event: "TaskEvent"


@dataclass
class ManagedTask:
    task_id: str
    session: IntentSession | None
    title: str = ""
    current_input: str = ""
    closed: bool = False
    turns: list[TaskRound] = field(default_factory=list)
    state: str = "queued"
    turn: Any = None
    error: str | None = None
    turn_count: int = 0
    restored_execution: bool = False
    save_failed: bool = False
    runner: asyncio.Task | None = field(default=None, repr=False)
    token: CancellationToken = field(default_factory=CancellationToken, repr=False)


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    state: str
    turn: Any = None
    error: str | None = None
    source: Any = None
    turn_id: str | None = None
    deliverables: tuple = ()


class TaskManager:
    def __init__(self, agent, *, max_concurrent=DEFAULT_MAX_CONCURRENT_TASKS, observer=None, store=None):
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self.agent = agent
        self.observer = observer or Observer()
        self.tasks: dict[str, ManagedTask] = {}
        self.events = asyncio.Queue()
        self._slots = asyncio.Semaphore(max_concurrent)
        self._closed = False
        self.store = store
        if store is not None:
            for row in store.summaries():
                self.tasks[row["task_id"]] = ManagedTask(
                    row["task_id"], None, title=row["title"], closed=bool(row["closed"]),
                    state=row["state"], error=row["error"], turn_count=row["turn_no"],
                    restored_execution=row["state"] == "completed" and bool(row["run_id"]),
                )

    def session_for(self, task):
        if task.session is None:
            try:
                task.session = self.store.session(task.task_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise TaskStorageError("TASK_STORAGE_UNAVAILABLE") from exc
        return task.session

    def history(self, task_id):
        task = self.tasks[task_id]
        heading = messages.TASK_LIST_ITEM.format(
            task_id=task_id, title=task.title,
            state=messages.TASK_CLOSED_LABEL if task.closed else messages.TASK_STATE_LABELS[task.state],
        )
        parts = [heading]
        for row in self.store.history(task_id):
            entries = json.loads(row["deliverables_json"])
            answer = "\n".join(entry["text"] for entry in entries if entry["kind"] == "text")
            if row["state"] == "interrupted":
                answer = messages.TASK_INTERRUPTED if row["was_running"] else messages.TASK_INTERRUPTED_QUEUED
            elif row["state"] == "cancelled" and not answer:
                answer = messages.TASK_CANCELLED
            files = self.delivery_lines(entries)
            parts.append(messages.TASK_HISTORY_ROUND.format(
                number=row["turn_no"], state=messages.TASK_STATE_LABELS[row["state"]],
                text=row["text"], answer="\n".join(filter(None, (answer, *files))),
            ).rstrip())
        if task.save_failed:
            parts.append(messages.TASK_RESULT_NOT_SAVED)
        return "\n\n".join(parts)

    def delivery_lines(self, entries):
        lines = []
        for entry in entries:
            if entry["kind"] == "file":
                if entry["available"]:
                    path = self.store.files / entry["relative_path"]
                    if path.is_file() and not path.is_symlink():
                        lines.append(messages.TASK_DELIVERY_FILE.format(name=entry["name"], path=path))
                        continue
                lines.append(messages.TASK_DELIVERY_MISSING.format(name=entry["name"]))
            elif entry["kind"] == "file_limit":
                lines.append(messages.TASK_DELIVERY_LIMIT)
        return lines

    def restore_waiting(self, session):
        """Retain the existing Weixin single-session clarification checkpoint."""
        if not session.questions:
            raise ValueError("NO_PENDING_QUESTIONS")
        task_id = uuid4().hex[:8]
        task = ManagedTask(task_id, session.model_copy(deep=True), state="waiting")
        self.tasks[task_id] = task
        self._observe(task)
        return task

    def submit(self, text, session, *, task_id=None, received_at=None, source=None, routing=None):
        if self._closed:
            raise ValueError("TASK_MANAGER_CLOSED")
        if not text.strip():
            raise ValueError("EMPTY_TASK_INPUT")
        if sum(t.state == "queued" for t in self.tasks.values()) >= 32:
            raise ValueError("TASK_QUEUE_FULL")
        if task_id is None:
            task_id = uuid4().hex[:8]
            while task_id in self.tasks:
                task_id = uuid4().hex[:8]
            task = ManagedTask(
                task_id,
                session.new_request(),
                title=text.splitlines()[0][:80],
            )
        else:
            task = self.tasks[task_id]
            if task.save_failed:
                raise TaskStorageError("TASK_STORAGE_UNAVAILABLE")
            if (task.closed or task.state in {"queued", "running"}
                    or (task.runner is not None and not task.runner.done())):
                raise ValueError("TASK_NOT_WAITING")
        session = self.session_for(task)
        turn_id = uuid4().hex
        received_at = received_at or datetime.now(UTC)
        if self.store is not None:
            try:
                if task.state == "interrupted":
                    interrupted = []
                    for row in self.store.interrupted_suffix(task_id):
                        interrupted.extend((
                            Message(role="user", content=row["text"]),
                            Message(role="assistant", content=(
                                messages.TASK_INTERRUPTED if row["was_running"]
                                else messages.TASK_INTERRUPTED_QUEUED
                            )),
                        ))
                    session = session.model_copy(update={"messages": (*session.messages, *interrupted)})
                    # A route computed before restoring the interrupted inputs used less evidence.
                    routing = None
                self.store.enqueue(
                    task_id, task.title, session, turn_id, text, received_at, task.turn_count + 1,
                )
            except (OSError, sqlite3.Error) as exc:
                raise TaskStorageError("TASK_STORAGE_UNAVAILABLE") from exc
        self.tasks[task_id] = task
        task.state = "queued"
        task.token = CancellationToken()
        task.turn, task.error = None, None
        task.restored_execution = False
        task.current_input = text
        task.runner = asyncio.create_task(
            self._run(task, session, turn_id, text, received_at, source, routing)
        )

        def cancelled_before_start(future):
            if future.cancelled():
                if task.state != "interrupted":
                    task.state = "cancelled"
                event = TaskEvent(task.task_id, task.state, source=source, turn_id=turn_id)
                self._record_round(task, text, received_at, event, session)

        task.runner.add_done_callback(cancelled_before_start)
        self._observe(task)
        return task

    def _observe(self, task):
        self.observer.emit(
            "task.state",
            data={
                "task_id": task.task_id,
                "state": task.state,
                "request_id": task.session.request_id,
                "error_code": task.error,
                "closed": task.closed,
                "turn_count": task.turn_count,
            },
        )

    async def _run(self, task, session, turn_id, text, received_at, source, routing):
        try:
            async with self._slots:
                if self.store is not None:
                    try:
                        self.store.start(turn_id)
                    except (OSError, sqlite3.Error) as exc:
                        raise TaskStorageError("TASK_STORAGE_UNAVAILABLE") from exc
                task.state = "running"
                self._observe(task)
                with self.observer.span("task.turn", task_id=task.task_id, turn_id=turn_id):
                    turn = await self.agent.advance(
                        session, text, received_at=received_at, cancellation_token=task.token,
                        continue_task=bool(session.messages or session.questions),
                        task_id=task.task_id, turn_id=turn_id,
                        **({"routing": routing} if routing is not None else {}),
                    )
                task.turn = turn
                if task.token.cancelled:
                    if task.state != "interrupted":
                        task.state = "cancelled"
                elif turn.result is not None:
                    task.state = (
                        "completed"
                        if turn.result.execution_status == "COMPLETED"
                        and turn.result.output_complete
                        else "cancelled"
                        if turn.result.execution_status == "CANCELLED"
                        else "failed"
                    )
                else:
                    task.state = "completed" if turn.response is not None else "waiting"
        except asyncio.CancelledError:
            if task.state != "interrupted":
                task.state = "cancelled"
        except Exception as exc:
            task.state = "failed"
            task.error = str(exc) if isinstance(exc, TaskStorageError) else getattr(exc, "code", type(exc).__name__)
        finally:
            event = TaskEvent(task.task_id, task.state, task.turn, task.error, source, turn_id)
            self._record_round(task, text, received_at, event, session)

    def _record_round(self, task, text, received_at, event, starting_session):
        answer = format_task_event(event)
        session = task.session
        turn = event.turn
        result = turn.result if turn is not None else None
        files = getattr(turn, "deliverables", ()) if turn is not None else ()
        if event.state != "interrupted":
            session = turn.session if turn is not None else starting_session
            history_answer = answer
            if result is not None and result.artifacts:
                history_answer += "\n" + json.dumps({"artifacts": result.artifacts}, ensure_ascii=False)
            if files:
                history_answer += "\n" + "\n".join(self.delivery_lines(files))
            history = session.messages
            if turn is None:
                history = (*history, Message(role="user", content=text))
                session = session.model_copy(update={"reply": answer})
            if not history or history[-1].role != "assistant" or history[-1].content != history_answer:
                session = session.model_copy(update={
                    "messages": (*history, Message(role="assistant", content=history_answer)),
                })
        if self.store is not None:
            entries = files
            if event.state != "interrupted":
                entries = ({"kind": "text", "text": answer}, *files)
            try:
                self.store.settle(
                    task.task_id, event.turn_id, session, event.state,
                    trace_id=turn.trace_id if turn is not None else None,
                    run_id=result.run_id if result is not None else None, error=event.error,
                    deliverables=entries,
                )
            except (OSError, sqlite3.Error):
                task.save_failed = True
                task.state = "failed"
                task.error = "TASK_RESULT_NOT_SAVED"
                event = replace(event, state=task.state, error=task.error)
            else:
                event = replace(event, deliverables=entries)
        task.session = session
        task.turn_count += 1
        task.turns.append(TaskRound(event.turn_id, text, received_at, event))
        self._observe(task)
        self.events.put_nowait(event)

    def cancel(self, task_id, *, finish_memory=True):
        task = self.tasks[task_id]
        if task.closed or task.state in {"completed", "failed", "cancelled", "interrupted"}:
            return
        self.session_for(task)
        release_memory = finish_memory and (
            task.state == "waiting" or (task.state == "queued" and task.session.questions)
        )
        if task.state == "waiting":
            session = task.session.model_copy(
                update={
                    "clarification_items": (),
                    "workflow_pending": None,
                    "reply": messages.TASK_CANCELLED,
                    "displayed_question_ids": (),
                }
            )
            if self.store is not None:
                try:
                    self.store.cancel_waiting(task_id, session)
                except (OSError, sqlite3.Error) as exc:
                    raise TaskStorageError("TASK_STORAGE_UNAVAILABLE") from exc
            task.session = session
        if release_memory:
            self._finish_memory_request(task)
        task.token.cancel()
        task.state = "cancelled"
        if task.runner is not None and not task.runner.done():
            task.runner.cancel()
        else:
            self._observe(task)

    def finish(self, task_id):
        task = self.tasks[task_id]
        if task.save_failed:
            raise TaskStorageError("TASK_STORAGE_UNAVAILABLE")
        if task.state in {"queued", "running"} or (task.runner and not task.runner.done()):
            raise ValueError("TASK_NOT_WAITING")
        self.session_for(task)
        if self.store is not None:
            try:
                self.store.finish(task_id)
            except (OSError, sqlite3.Error) as exc:
                raise TaskStorageError("TASK_STORAGE_UNAVAILABLE") from exc
        if task.state == "waiting":
            self._finish_memory_request(task)
        task.closed = True
        self._observe(task)

    def _finish_memory_request(self, task):
        finish = getattr(self.agent, "finish_memory_request", None)
        if finish is not None:
            finish(task.session)

    async def drain(self):
        await asyncio.gather(
            *(t.runner for t in self.tasks.values() if t.runner), return_exceptions=True
        )

    async def close(self):
        self._closed = True
        for task in self.tasks.values():
            if self.store is None:
                self.cancel(task.task_id, finish_memory=False)
            elif task.state in {"queued", "running"} and task.runner is not None:
                task.state = "interrupted"
                task.token.cancel()
                task.runner.cancel()
        await self.drain()
