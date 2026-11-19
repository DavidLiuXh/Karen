"""Rich presentation for Markdown replies in the interactive terminal."""

import re
from pathlib import Path

from rich.console import Group
from rich.markdown import Heading, Markdown, Paragraph
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..message import messages
from ..message.messages import TERMINAL_SECTION_LABELS

TERMINAL_THEME = Theme({
    "markdown.item.number": "bold cyan",
    "markdown.item.bullet": "bold cyan",
    "markdown.h2": "bold cyan",
    "markdown.h3": "bold cyan",
    "markdown.table.header": "bold cyan",
    "markdown.table.border": "dim",
    "reply.conclusion": "bold cyan",
    "reply.evidence": "bold cyan",
    "reply.caution": "bold yellow",
})

_SECTION = re.compile(
    r"^(" + "|".join(map(re.escape, TERMINAL_SECTION_LABELS)) + r")[：:]"
)


class ReplyHeading(Heading):
    def __rich_console__(self, console, options):
        kind = TERMINAL_SECTION_LABELS.get(self.text.plain.rstrip("：:"))
        if kind:
            self.text.stylize(f"reply.{kind}")
        self.text.stylize("bold cyan" if self.tag in {"h1", "h2"} else "bold")
        yield self.text


class ReplyParagraph(Paragraph):
    @classmethod
    def create(cls, markdown, token):
        paragraph = super().create(markdown, token)
        paragraph.top_level = token.level == 0
        return paragraph

    def __rich_console__(self, console, options):
        if not self.top_level:
            yield from super().__rich_console__(console, options)
            return

        match = _SECTION.match(self.text.plain)
        if match is None:
            yield from super().__rich_console__(console, options)
            return

        kind = TERMINAL_SECTION_LABELS[match[1]]
        style = f"reply.{kind}"
        self.text.stylize(style, 0, match.end())
        yield from super().__rich_console__(console, options)


class ReplyMarkdown(Markdown):
    elements = {
        **Markdown.elements, "paragraph_open": ReplyParagraph, "heading_open": ReplyHeading,
    }


READING_WIDTH = 100
STATE_STYLES = {
    "queued": "cyan", "running": "cyan", "waiting": "yellow",
    "completed": "green", "failed": "red", "cancelled": "dim", "interrupted": "yellow",
}


def render_reply(console, text, *, task_id=None, title=None, state=None, deliveries=(), schedule=None):
    """One stable transcript block; dynamic controls belong to prompt_toolkit."""
    width = min(console.width, READING_WIDTH)
    header = Table.grid(expand=True, padding=(0, 1))
    header.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    header.add_column(justify="right", no_wrap=True)
    identity = Text()
    identity.append(messages.CLI_REPLY_TITLE, style="bold magenta")
    if title or task_id:
        identity.append(" · " + (title or task_id).splitlines()[0], style="dim")
    header.add_row(identity, Text(messages.TASK_STATE_LABELS[state], style=STATE_STYLES[state]) if state else Text())
    parts = [header]
    if task_id:
        parts.append(Text(messages.CLI_TASK_REFERENCE.format(task_id=task_id), style="dim"))
    if schedule:
        parts.append(Text(schedule, style="dim"))
    parts.extend([Rule(style="dim"), ReplyMarkdown(text)])
    if deliveries:
        parts.extend([Text(""), Text(messages.CLI_DELIVERIES, style="bold cyan")])
        for item in deliveries:
            if item["kind"] == "file" and item["available"]:
                parts.append(Text(item["name"], style="bold"))
                path = Text(item["path"], style="dim", overflow="fold")
                path.stylize("link " + Path(item["path"]).as_uri())
                parts.append(path)
            elif item["kind"] == "file":
                parts.append(Text(messages.TASK_DELIVERY_MISSING.format(name=item["name"]), style="yellow"))
            elif item["kind"] == "file_limit":
                parts.append(Text(messages.TASK_DELIVERY_LIMIT, style="yellow"))
    console.print()
    console.print(Padding(Group(*parts), (0, 2)), width=width)
    console.print()


def render_notice(console, text):
    """Notifications have less visual weight than assistant replies."""
    console.print(Padding(Text(text, style="dim"), (0, 2)), width=min(console.width, READING_WIDTH))
