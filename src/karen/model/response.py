"""User-facing execution results shared by terminal and messaging channels."""

import json

from dynamic_graph import RunResult

from .. import messages


def _diagnostic_message(diagnostic) -> str:
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
        lines.append(answer)
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
