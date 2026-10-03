"""Deterministic checks of recorded behavior, separate from business acceptance."""


def has_truncation(value):
    if isinstance(value, dict):
        return (
            value.get("truncated") is True
            or "_truncated_fields" in value
            or any(has_truncation(v) for v in value.values())
        )
    if isinstance(value, list):
        return any(has_truncation(v) for v in value)
    return False


def check_trace(events, coverage):
    incomplete = bool(
        coverage.get("partial")
        or coverage.get("invalid_lines")
        or coverage.get("unfinished_tail")
        or coverage.get("dropped")
        or coverage.get("write_failures")
        or any(
            h.get("dropped") or h.get("write_failures") for h in coverage.get("process_health", [])
        )
    )
    clarification, history, fallback = [], [], []
    for event in events:
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        if (
            event.get("stage") == "turn"
            and event.get("event_type") == "span.finished"
            and data.get("outcome") == "needs_clarification"
        ):
            clarification.append(
                not any(
                    e.get("trace_id") == event.get("trace_id")
                    and e.get("event_type") == "execution.started"
                    for e in events
                )
            )
        if event.get("event_type") == "execution.started":
            memory = data.get("goal", {}).get("context", {}).get("memory")
            if isinstance(memory, dict):
                if has_truncation(memory):
                    history.append(None)
                else:
                    h = memory.get("history", {})
                    history.append(not h.get("messages") or h.get("status") == "selected")
        if event.get(
            "event_type"
        ) == "memory.recalled" and "RERANK_FAILED_FUSION_ORDER" in data.get("degradations", []):
            if has_truncation(data):
                fallback.append(None)
            else:
                hits = [*data.get("m1", []), *data.get("m2", [])]
                fallback.append(
                    all(
                        h.get("relevance") == "unverified" and h.get("rerank_rank") is None
                        for h in hits
                    )
                )
    results = []
    for label, values in [
        ("澄清轮未启动执行", clarification),
        ("历史仅在关联确认后注入", history),
        ("精排失败未冒充精排成功", fallback),
    ]:
        status = (
            "fail"
            if False in values
            else "unknown"
            if incomplete or None in values
            else "pass"
            if values
            else "not_applicable"
        )
        results.append({"label": label, "status": status})
    return results
