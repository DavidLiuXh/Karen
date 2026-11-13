"""Rich presentation for Markdown replies in the interactive terminal."""

import re

from rich.markdown import Heading, Markdown, Paragraph
from rich.panel import Panel
from rich.theme import Theme

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
        yield from super().__rich_console__(console, options)


class ReplyParagraph(Paragraph):
    @classmethod
    def create(cls, markdown, token):
        paragraph = super().create(markdown, token)
        paragraph.top_level = token.level == 0
        paragraph.allow_panel = markdown.has_multiple_blocks
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
        if kind == "conclusion" and self.allow_panel and console.width >= 48:
            yield Panel(self.text, border_style="cyan", expand=False)
        else:
            yield from super().__rich_console__(console, options)


class ReplyMarkdown(Markdown):
    elements = {
        **Markdown.elements, "paragraph_open": ReplyParagraph, "heading_open": ReplyHeading,
    }

    def __init__(self, markup):
        super().__init__(markup)
        self.has_multiple_blocks = sum(
            token.level == 0 and token.nesting == 1 for token in self.parsed
        ) > 1
