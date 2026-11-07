"""Independent task sessions, bounded admission, cancellation and turn events."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from dynamic_graph import CancellationToken

from ..intent import IntentSession
from ..intent.recognizer import Message
from ..message import messages
from ..model.response import format_task_event
from ..observability import Observer

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
    session: IntentSession
    title: str = ""
    current_input: str = ""
    closed: bool = False
    turns: list[TaskRound] = field(default_factory=list)
    state: str = "queued"
    turn: Any = None
    error: str | None = None
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


class TaskManager:
    def __init__(self, agent, *, max_concurrent=DEFAULT_MAX_CONCURRENT_TASKS, observer=None):
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self.agent = agent
        self.observer = observer or Observer()
        self.tasks: dict[str, ManagedTask] = {}
        self.events = asyncio.Queue()
        self._slots = asyncio.Semaphore(max_concurrent)
        self._closed = False

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
            self.tasks[task_id] = task
        else:
            task = self.tasks[task_id]
            if (task.closed or task.state in {"queued", "running"}
                    or (task.runner is not None and not task.runner.done())):
                raise ValueError("TASK_NOT_WAITING")
            task.state = "queued"
            task.token = CancellationToken()
            task.turn, task.error = None, None
        task.current_input = text
        task.runner = asyncio.create_task(
            self._run(task, text, received_at or datetime.now(UTC), source, routing)
        )

        def cancelled_before_start(future):
            if future.cancelled():
                event = TaskEvent(task.task_id, "cancelled", source=source, turn_id=uuid4().hex)
                self._record_round(task, text, received_at or datetime.now(UTC), event)

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
                "turn_count": len(task.turns),
            },
        )

    async def _run(self, task, text, received_at, source, routing):
        turn_id = uuid4().hex
        try:
            async with self._slots:
                task.state = "running"
                self._observe(task)
                with self.observer.span("task.turn", task_id=task.task_id, turn_id=turn_id):
                    turn = await self.agent.advance(
                        task.session, text, received_at=received_at, cancellation_token=task.token,
                        continue_task=bool(task.session.messages or task.session.questions),
                        task_id=task.task_id, turn_id=turn_id,
                        **({"routing": routing} if routing is not None else {}),
                    )
                task.session, task.turn = turn.session, turn
                if task.token.cancelled:
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
            task.state = "cancelled"
        except Exception as exc:
            task.state = "failed"
            task.error = getattr(exc, "code", type(exc).__name__)
        finally:
            event = TaskEvent(task.task_id, task.state, task.turn, task.error, source, turn_id)
            self._record_round(task, text, received_at, event)

    def _record_round(self, task, text, received_at, event):
        task.turns.append(TaskRound(event.turn_id, text, received_at, event))
        answer = format_task_event(event)
        if task.turn is not None:
            if task.turn.result is not None and task.turn.result.artifacts:
                answer += "\n" + json.dumps({"artifacts": task.turn.result.artifacts}, ensure_ascii=False)
            history = task.session.messages
            if not history or history[-1].role != "assistant" or history[-1].content != answer:
                task.session = task.session.model_copy(update={
                    "messages": (*history, Message(role="assistant", content=answer)),
                })
        else:
            task.session = task.session.model_copy(update={
                "messages": (*task.session.messages, Message(role="user", content=text),
                             Message(role="assistant", content=answer)),
                "reply": answer,
            })
        self._observe(task)
        self.events.put_nowait(event)

    def cancel(self, task_id):
        task = self.tasks[task_id]
        if task.closed or task.state in {"completed", "failed", "cancelled"}:
            return
        task.token.cancel()
        if task.state == "waiting":
            task.session = task.session.model_copy(
                update={
                    "clarification_items": (),
                    "workflow_pending": None,
                    "reply": messages.TASK_CANCELLED,
                    "displayed_question_ids": (),
                }
            )
        task.state = "cancelled"
        if task.runner is not None and not task.runner.done():
            task.runner.cancel()
        else:
            self._observe(task)

    def finish(self, task_id):
        task = self.tasks[task_id]
        if task.state in {"queued", "running"}:
            raise ValueError("TASK_NOT_WAITING")
        task.closed = True
        self._observe(task)

    async def drain(self):
        await asyncio.gather(
            *(t.runner for t in self.tasks.values() if t.runner), return_exceptions=True
        )

    async def close(self):
        self._closed = True
        for task_id in self.tasks:
            self.cancel(task_id)
        await self.drain()
