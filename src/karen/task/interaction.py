"""Task addressing and contextual follow-up routing shared by CLI and Weixin."""

import sqlite3
from datetime import UTC, datetime

from dynamic_graph.models.client import ModelCallError

from ..intent.recognizer import Message
from ..message import messages
from .storage import TaskStorageError


class TaskInteraction:
    def __init__(self, manager, session):
        self.manager, self.session = manager, session
        self.focus = None
        self.explicit_focus = False
        self.pending = None
        self.submitted_source = None
        self.submitted_task_id = None

    async def accept_input(self, text, *, received_at=None, source=None):
        """Resolve ordinary follow-ups before admitting work to a task."""
        self.submitted_source = None
        self.submitted_task_id = None
        received_at = received_at or datetime.now(UTC)
        routing = None
        if (text.strip() and not text.lstrip().startswith("/") and self.pending is None
                and self.focus is not None and not self.explicit_focus):
            task = self.manager.tasks[self.focus]
            if task.closed:
                self.focus = None
            elif task.state not in {"waiting", "interrupted"}:
                try:
                    session = self.manager.session_for(task)
                except TaskStorageError:
                    return messages.TASK_STORAGE_UNAVAILABLE
                if task.state in {"queued", "running"}:
                    session = session.model_copy(update={"messages": (
                        *session.messages, Message(role="user", content=task.current_input),
                    )})
                try:
                    routing = await self.manager.agent.intent.classify(session, text)
                except (ModelCallError, ValueError, TimeoutError):
                    return messages.TASK_ROUTING_FAILED
                if routing.task_relation == "new":
                    return self.accept("/new " + text, received_at=received_at, source=source, routing=routing)
        return self.accept(text, received_at=received_at, source=source, routing=routing)

    def accept(self, text, *, received_at=None, source=None, routing=None):
        self.submitted_source = None
        self.submitted_task_id = None
        text = text.strip()
        if not text:
            return messages.TASK_INPUT_REJECTED.format(code="EMPTY_TASK_INPUT")
        parts = text.split(maxsplit=1)
        command = parts[0]
        rest = parts[1] if len(parts) == 2 else ""
        if command == "/workflow":
            args = rest.split()
            if len(args) != 2 or args[0] != "extract":
                return messages.WORKFLOW_EXTRACT_USAGE
            task = self.manager.tasks.get(args[1])
            if task is None:
                return messages.TASK_UNKNOWN
            if task.restored_execution:
                return messages.WORKFLOW_EXTRACT_RESTORED
            if task.state != "completed" or task.turn is None:
                return messages.WORKFLOW_EXTRACT_NO_RUN
            receipt = self.manager.agent.extract_workflow(task.turn)
            if receipt.get("reason") == "unavailable":
                return messages.WORKFLOW_EXTRACT_UNAVAILABLE
            if receipt.get("reason") == "not_completed":
                return messages.WORKFLOW_EXTRACT_NO_RUN
            if not receipt["accepted"]:
                return messages.WORKFLOW_EXTRACT_REJECTED.format(reason=receipt["reason"])
            return messages.WORKFLOW_EXTRACT_QUEUED.format(job_id=receipt["job_id"])
        if command == "/tasks":
            if rest not in {"", "列表", "list"}:
                return messages.TASK_LIST_USAGE
            return (
                "\n".join(
                    messages.TASK_LIST_ITEM.format(task_id=t.task_id, state=messages.TASK_CLOSED_LABEL if t.closed else messages.TASK_STATE_LABELS[t.state], title=t.title)
                    for t in self.manager.tasks.values()
                )
                or messages.TASK_LIST_EMPTY
            )
        if command == "/task":
            if self.manager.store is None:
                return messages.TASK_HISTORY_UNAVAILABLE
            if len(rest.split()) != 1:
                return messages.TASK_HISTORY_USAGE
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            try:
                return self.manager.history(rest)
            except (OSError, sqlite3.Error):
                return messages.TASK_STORAGE_UNAVAILABLE
        target = None
        consume_pending = False
        if command == "/use":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            if self.pending is None:
                if self.manager.tasks[rest].closed:
                    return messages.TASK_NOT_WAITING
                self.focus = rest
                self.explicit_focus = True
                return messages.TASK_FOCUSED.format(task_id=rest)
            target = rest
            text, received_at, source = self.pending
            consume_pending = True
        elif command == "/finish":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            try:
                self.manager.finish(rest)
            except TaskStorageError:
                return messages.TASK_STORAGE_UNAVAILABLE
            except ValueError:
                return messages.TASK_NOT_WAITING
            if self.focus == rest:
                self.focus = None
            return messages.TASK_FINISHED.format(task_id=rest)
        elif command == "/cancel":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            try:
                self.manager.cancel(rest)
            except TaskStorageError:
                return messages.TASK_STORAGE_UNAVAILABLE
            if self.focus == rest:
                self.focus = None
            return messages.TASK_CANCEL_REQUESTED.format(task_id=rest)
        elif command == "/help":
            return messages.TASK_HELP + (messages.TASK_HISTORY_HELP if self.manager.store else "")
        elif command == "/reply":
            target, _, text = rest.partition(" ")
            if target not in self.manager.tasks:
                return messages.TASK_UNKNOWN
        elif command == "/new":
            if not rest and self.pending is not None:
                text, received_at, source = self.pending
                consume_pending = True
            else:
                text = rest
        else:
            if self.pending is not None:
                return messages.TASK_SELECT_PENDING
            if self.focus is not None:
                target = self.focus
            elif any(t.state == "waiting" and not t.closed for t in self.manager.tasks.values()):
                self.pending = (text, received_at, source)
                return messages.TASK_SELECT_OWNER
        try:
            task = self.manager.submit(
                text, self.session, task_id=target, received_at=received_at, source=source, routing=routing
            )
        except KeyError:
            return messages.TASK_UNKNOWN
        except TaskStorageError:
            return messages.TASK_STORAGE_UNAVAILABLE
        except ValueError as exc:
            return (
                messages.TASK_NOT_WAITING
                if str(exc) == "TASK_NOT_WAITING"
                else messages.TASK_INPUT_REJECTED.format(code=str(exc))
            )
        if consume_pending:
            self.pending = None
        self.focus = task.task_id
        self.explicit_focus = False
        self.submitted_source = source
        self.submitted_task_id = task.task_id
        return messages.TASK_ACCEPTED.format(task_id=task.task_id)
