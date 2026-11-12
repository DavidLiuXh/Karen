"""User-facing execution results shared by terminal and messaging channels."""

import json
import re

from dynamic_graph import RunResult

from ..message import messages


# Inline numbered prose starts at a line boundary or after a colon. Later
# entries follow a semicolon. Preserve code spans and fenced blocks.
_INLINE_ITEM = re.compile(
    r"`+[^`\n]*`+|(?P<lead>^|[：:；;])[ \t]*(?P<number>[1-9][0-9]*)[）)]",
    re.MULTILINE,
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _format_numbered_paragraph(text: str) -> str:
    breaks, items = [], []
    for match in _INLINE_ITEM.finditer(text):
        lead, number = match["lead"], match["number"]
        if number == "1" and lead in {"", "：", ":"}:
            items = [match]
        elif items and lead in {"；", ";"} and number == str(len(items) + 1):
            items.append(match)
            if len(items) == 2 and items[0]["lead"]:
                breaks.append(items[0])
            breaks.append(match)
    for match in reversed(breaks):
        text = text[:match.end("lead")] + "\n\n" + text[match.start("number"):]
    return text


def _format_inline_items(text: str) -> str:
    sections, paragraph = [], []
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if not (fence or marker) and line.strip() and not line.startswith(("    ", "\t")):
            paragraph.append(line)
            continue
        sections.append(_format_numbered_paragraph("".join(paragraph)))
        paragraph.clear()
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token.startswith(fence) and not line[marker.end():].strip():
                fence = ""
        sections.append(line)
    sections.append(_format_numbered_paragraph("".join(paragraph)))
    return "".join(sections)


def _diagnostic_message(diagnostic) -> str:
    if diagnostic.code == "MODEL_INPUT_TOO_LARGE":
        return messages.MODEL_INPUT_TOO_LARGE
    if diagnostic.code != "TOOL_FAILED":
        return diagnostic.message
    reason = diagnostic.details.get("reason_code")
    if reason == "TOOL_TRANSPORT_FAILED":
        return messages.TOOL_TRANSPORT_FAILED
    if reason == "TOOL_RESPONSE_INVALID":
        return messages.TOOL_RESPONSE_INVALID
    status = diagnostic.details.get("http_status")
    if reason == "TOOL_HTTP_ERROR" and type(status) is int and 100 <= status <= 599:
        if status in {401, 403}:
            return messages.TOOL_UNAUTHORIZED.format(status=status)
        if status == 429:
            return messages.TOOL_RATE_LIMITED
        if status >= 500:
            return messages.TOOL_UNAVAILABLE.format(status=status)
        return messages.TOOL_HTTP_FAILED.format(status=status)
    return diagnostic.message


def format_result(result: RunResult) -> str:
    lines = []
    if result.execution_status == "FAILED":
        lines.append(messages.TASK_FAILED)
    elif result.execution_status == "CANCELLED":
        lines.append(messages.TASK_CANCELLED)
    elif not result.output_complete:
        lines.append(messages.TASK_INCOMPLETE)

    answer = result.outputs.get("answer")
    has_answer = isinstance(answer, str) and bool(answer.strip())
    if has_answer:
        if lines:
            lines.append(messages.PARTIAL_ANSWER)
        lines.append(_format_inline_items(answer))
    for name, value in result.outputs.items():
        if has_answer and name in {"answer", "evidence", "limitations"}:
            continue
        if name in {"evidence", "limitations"} and value == []:
            continue
        if (
            name == "evidence"
            and isinstance(value, list)
            and all(
                isinstance(item, dict) and "source" in item and "text" in item for item in value
            )
        ):
            lines.append(messages.SOURCES_LABEL)
            lines.extend(messages.SOURCE_ITEM.format(source=item['source'], text=item['text']) for item in value)
        elif (
            name == "limitations"
            and isinstance(value, list)
            and all(isinstance(item, dict) and "description" in item for item in value)
        ):
            lines.append(messages.LIMITATIONS_LABEL)
            lines.extend(messages.LIMITATION_ITEM.format(description=item['description']) for item in value)
        else:
            text = (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            )
            lines.append(messages.NAMED_OUTPUT.format(name=name, text=text))
    if not result.outputs and not lines:
        lines.append(messages.EMPTY_RESULT)
    unsuccessful = result.execution_status != "COMPLETED" or not result.output_complete
    for diagnostic in result.diagnostics:
        # Full diagnostics remain on RunResult and in observation records. Routine
        # graph advice and recovered errors do not belong in the delivered answer.
        actionable = diagnostic.action != "NONE"
        if not actionable and not (unsuccessful and diagnostic.severity == "error"):
            continue
        label = messages.WARNING_LABEL if diagnostic.severity == "warning" else messages.DIAGNOSTIC_LABEL
        lines.append(messages.DIAGNOSTIC_ITEM.format(label=label, code=diagnostic.code, message=_diagnostic_message(diagnostic)))
    return "\n\n".join(lines)


def format_task_event(event, *, json_output=False):
    if event.state == "cancelled" and (event.turn is None or event.turn.result is None):
        return messages.TASK_CANCELLED
    if event.error:
        return messages.TASK_ERROR.format(code=event.error)
    turn = event.turn
    if turn is None:
        return messages.TASK_ERROR.format(code="NO_RESULT")
    if turn.result is not None:
        return turn.result.model_dump_json(indent=2) if json_output else format_result(turn.result)
    if turn.response is not None:
        return turn.response if json_output else _format_inline_items(turn.response)
    return "\n".join(turn.session.questions)
