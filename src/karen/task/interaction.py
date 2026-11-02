"""Explicit task addressing shared by CLI and Weixin; never guess an input owner."""

from ..message import messages


class TaskInteraction:
    def __init__(self, manager, session):
        self.manager, self.session = manager, session
        self.focus = None
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
        if command == "/use":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            self.focus = rest
            if self.pending:
                value, timestamp, origin = self.pending
                try:
                    task = self.manager.submit(
                        value, self.session, task_id=rest, received_at=timestamp, source=origin
                    )
                except ValueError:
                    return messages.TASK_NOT_WAITING
                self.pending = None
                self.submitted_source = origin
                return messages.TASK_ACCEPTED.format(task_id=task.task_id)
            return messages.TASK_FOCUSED.format(task_id=rest)
        if command == "/cancel":
            if rest not in self.manager.tasks:
                return messages.TASK_UNKNOWN
            self.manager.cancel(rest)
            return messages.TASK_CANCEL_REQUESTED.format(task_id=rest)
        if command == "/help":
            return messages.TASK_HELP
        target = None
        if command == "/reply":
            target, _, text = rest.partition(" ")
            if target not in self.manager.tasks:
                return messages.TASK_UNKNOWN
        elif command == "/new":
            if not rest and self.pending is not None:
                text, received_at, source = self.pending
                self.pending = None
            else:
                text = rest
        elif self.focus is not None:
            target = self.focus
        elif any(t.state == "waiting" for t in self.manager.tasks.values()):
            if self.pending is not None:
                return messages.TASK_SELECT_PENDING
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
        self.submitted_source = source
        return messages.TASK_ACCEPTED.format(task_id=task.task_id)
