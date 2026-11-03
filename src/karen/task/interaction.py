"""Explicit task addressing shared by CLI and Weixin; never guess an input owner."""

from ..message import messages


class TaskInteraction:
    def __init__(self, manager, session):
        self.manager, self.session = manager, session
        self.focus = None
        self._completed_selected = False
        self.pending = None
        self.submitted_source = None

    def accept(self, text, *, received_at=None, source=None):
        self.submitted_source = None
        text = text.strip()
        if not text:
            return messages.TASK_INPUT_REJECTED.format(code="EMPTY_TASK_INPUT")
        if text == "/tasks":
            return (
                "\n".join(
                    messages.TASK_LIST_ITEM.format(task_id=t.task_id, state=messages.TASK_STATE_LABELS[t.state], title=t.title)
                    for t in self.manager.tasks.values()
                )
                or messages.TASK_LIST_EMPTY
            )
        command, _, rest = text.partition(" ")
        target = None
        consume_pending = False
        if command == "/use":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            if self.pending is None:
                if self.manager.tasks[rest].state in {"failed", "cancelled"}:
                    return messages.TASK_NOT_WAITING
                self.focus = rest
                # Explicit selection can address a completed task once. A stale
                # focus alone must never turn later clarification into new work.
                self._completed_selected = self.manager.tasks[rest].state == "completed"
                return messages.TASK_FOCUSED.format(task_id=rest)
            target = rest
            text, received_at, source = self.pending
            consume_pending = True
        elif command == "/cancel":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            self.manager.cancel(rest)
            if self.focus == rest:
                self.focus = None
                self._completed_selected = False
            return messages.TASK_CANCEL_REQUESTED.format(task_id=rest)
        elif command == "/help":
            return messages.TASK_HELP
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
                focused = self.manager.tasks[self.focus]
                if (
                    focused.state in {"completed", "failed", "cancelled"}
                    and not self._completed_selected
                ):
                    self.focus = None
            if self.focus is not None:
                target = self.focus
            elif any(t.state == "waiting" for t in self.manager.tasks.values()):
                self.pending = (text, received_at, source)
                return messages.TASK_SELECT_OWNER
        try:
            task = self.manager.submit(
                text, self.session, task_id=target, received_at=received_at, source=source
            )
        except KeyError:
            return messages.TASK_UNKNOWN
        except ValueError as exc:
            return (
                messages.TASK_NOT_WAITING
                if str(exc) == "TASK_NOT_WAITING"
                else messages.TASK_INPUT_REJECTED.format(code=str(exc))
            )
        if consume_pending:
            self.pending = None
        if target is not None or command == "/new":
            # The manager may create a successor for a completed target. Follow
            # the actual accepted task, never the old requested identifier.
            self.focus = task.task_id
        self._completed_selected = False
        self.submitted_source = source
        return messages.TASK_ACCEPTED.format(task_id=task.task_id)
