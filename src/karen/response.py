"""Terminal presentation of execution results."""

import json

from dynamic_graph import RunResult


def _diagnostic_message(diagnostic) -> str:
    if diagnostic.code != "TOOL_FAILED":
        return diagnostic.message
    reason = diagnostic.details.get("reason_code")
    if reason == "TOOL_TRANSPORT_FAILED":
        return "工具服务连接或传输失败，请检查网络后重试。"
    if reason == "TOOL_RESPONSE_INVALID":
        return "工具服务返回了无法解析的数据。"
    status = diagnostic.details.get("http_status")
    if reason == "TOOL_HTTP_ERROR" and type(status) is int and 100 <= status <= 599:
        if status in {401, 403}:
            return f"工具服务拒绝授权（HTTP {status}），请检查对应服务的凭据或权限。"
        if status == 429:
            return "工具服务触发限流（HTTP 429），请稍后重试。"
        if status >= 500:
            return f"工具服务暂时不可用（HTTP {status}），请稍后重试。"
        return f"工具服务返回 HTTP {status}，本次调用未完成。"
    return diagnostic.message


def format_result(result: RunResult) -> str:
    lines = []
    if result.execution_status == "FAILED":
        lines.append("任务执行失败。")
    elif result.execution_status == "CANCELLED":
        lines.append("任务已取消。")
    elif not result.output_complete:
        lines.append("执行已结束，但结果不完整。")

    answer = result.outputs.get("answer")
    has_answer = isinstance(answer, str) and bool(answer.strip())
    if has_answer:
        if lines:
            lines.append("已产生的部分回答：")
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
            lines.append("来源：")
            lines.extend(f"- {item['source']}：{item['text']}" for item in value)
        elif (
            name == "limitations"
            and isinstance(value, list)
            and all(isinstance(item, dict) and "description" in item for item in value)
        ):
            lines.append("限制说明：")
            lines.extend(f"- {item['description']}" for item in value)
        else:
            text = (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            )
            lines.append(f"{name}：\n{text}")
    if not result.outputs and not lines:
        lines.append("执行已结束，没有返回内容。")
    for diagnostic in result.diagnostics:
        label = "提示" if diagnostic.severity == "warning" else "诊断"
        lines.append(f"{label}（{diagnostic.code}）：{_diagnostic_message(diagnostic)}")
    return "\n\n".join(lines)
