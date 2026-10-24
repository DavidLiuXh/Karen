"""Time-bounded enumeration uses persisted interactions, independent of semantic top-k."""

from datetime import datetime, timedelta, timezone

import pytest

from karen.context import ContextEvent, PersistenceError, TimeRange
from karen.context.storage import Storage


@pytest.fixture
def recorded(tmp_path):
    storage = Storage(tmp_path)
    storage.initialize()
    start = datetime.fromisoformat("2026-10-12T00:00:00+08:00")
    interval = TimeRange(start=start, end=start + timedelta(days=7))
    sequence = 0

    def append(request, kind="user_message", content="交流内容", *, when=None, **payload):
        nonlocal sequence
        sequence += 1
        event = ContextEvent(
            request_id=request, conversation_id="conversation", event_type=kind,
            timezone="Asia/Shanghai", occurred_at=when or start + timedelta(days=1),
            payload={"content": content, **payload},
        )
        storage.append(event, sequence, event.occurred_at)
        return event

    return storage, interval, append


def test_collection_includes_conversation_and_task_once_with_raw_sources(recorded):
    storage, interval, append = recorded
    append("before", when=interval.start - timedelta(microseconds=1))
    user = append("chat", content="聊聊我的阅读偏好", when=interval.start)
    reply = append("chat", "assistant_message", "你喜欢历史和考古。")
    append("chat", content="也喜欢运动。")  # Same request, including clarification turns.
    append("task", content="生成报告")
    for run in ("old", "latest"):
        append("task", "task_result", run_id=run, execution_status="COMPLETED", output_complete=True)
    append("task", "assistant_message", "报告已生成。")
    append("current", content="上周交流过什么？")
    append("end", when=interval.end)
    # Reopen the store: enumeration must work without extracted m1/m2 or live state.
    result = Storage(storage.root).collection(interval, None, exclude_request_id="current")
    assert result["total_matched"] == result["returned_count"] == 2
    assert result["complete"] and result["display_complete"]
    chat, task = result["items"]
    assert chat["request_id"] == "chat" and chat["kind"] == "conversation"
    assert chat["user_message_count"] == 2
    assert chat["user_input"] == user.payload["content"]
    assert chat["user_input_event_id"] == user.event_id
    assert chat["assistant_reply"] == reply.payload["content"]
    assert chat["occurred_at"] == interval.start.astimezone(timezone.utc).isoformat()
    assert task["request_id"] == "task" and task["kind"] == "task"
    assert task["run_id"] == "latest" and task["assistant_reply"] == "报告已生成。"
    assert all(item["request_id"] not in {"before", "end", "current"} for item in result["items"])


@pytest.mark.parametrize("target,status,expected", [
    ("interactions", None, {"chat", "success", "failure"}),
    ("tasks", None, {"success", "failure"}),
    ("interactions", "COMPLETED", {"success"}),
    ("tasks", "FAILED", {"failure"}),
])
def test_collection_task_filters_do_not_count_ordinary_dialogue(recorded, target, status, expected):
    storage, interval, append = recorded
    append("chat")
    for request, state in (("success", "COMPLETED"), ("failure", "FAILED")):
        append(request)
        append(request, "task_result", execution_status=state, run_id=request)
    result = storage.collection(interval, status, target=target)
    assert {item["request_id"] for item in result["items"]} == expected
    assert result["total_matched"] == len(expected)


def test_collection_counts_beyond_display_limit_and_marks_short_previews(recorded):
    storage, interval, append = recorded
    for index in range(57):
        append(f"chat-{index}", content="内容" * 150)
    result = storage.collection(interval, None)
    assert result["complete"] and not result["display_complete"]
    assert result["total_matched"] == 57 and result["returned_count"] == 50
    assert len(result["items"]) == 50
    assert all(item["user_input_truncated"] for item in result["items"])
    assert all(len(item["user_input"]) == 160 for item in result["items"])


@pytest.mark.parametrize("failure", [OSError, PersistenceError])
def test_missing_source_does_not_erase_count_or_claim_complete(recorded, monkeypatch, failure):
    storage, interval, append = recorded
    append("chat")

    def unavailable(*args, **kwargs):
        raise failure("SOURCE_UNAVAILABLE")

    monkeypatch.setattr(storage, "load_event", unavailable)
    result = storage.collection(interval, None)
    assert result["total_matched"] == 1 and not result["complete"]
    assert result["reason"] == "source_preview_unavailable"
    assert result["items"][0]["user_input_unavailable"]
    assert "user_input" not in result["items"][0]


def test_collection_does_not_count_an_out_of_range_task_result(recorded):
    storage, interval, append = recorded
    append("started")
    append("started", "task_result", when=interval.end, execution_status="COMPLETED")
    assert storage.collection(interval, None, target="tasks")["total_matched"] == 0
    result = storage.collection(interval, None)
    assert result["total_matched"] == 1
    assert result["items"][0]["kind"] == "conversation"
    assert "execution_status" not in result["items"][0]
