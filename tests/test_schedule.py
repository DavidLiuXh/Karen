"""Observable scheduling, isolated execution and channel delivery boundaries."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dynamic_graph import FakeModelClient
from pydantic import ValidationError

from karen import IntentSession, TaskTurn
from karen.schedule import ScheduledTasks
from karen.schedule.contracts import ScheduleCommand, ScheduleRule
from karen.task import TaskManager
from karen.task.console import run_console


class Agent:
    def __init__(self):
        self.inputs = []
        self.release = asyncio.Event()

    async def advance(self, session, text, **kwargs):
        self.inputs.append((session, text, kwargs))
        await self.release.wait()
        return TaskTurn(session.model_copy(update={"reply": "done"}))

    def record_response(self, *args, **kwargs):
        return ()


def daily(**kwargs):
    return ScheduleRule(kind="daily", timezone="Asia/Shanghai", time="12:00", **kwargs)


def create(service, **kwargs):
    service.apply(ScheduleCommand(
        action="create", name="GitHub趋势", prompt="生成当天GitHub趋势的精美HTML页面",
        rule=kwargs.pop("rule", daily()), **kwargs,
    ))
    return list(service.entries.values())[-1]


@pytest.fixture
async def runtime(tmp_path):
    now = [datetime(2026, 11, 4, 3, 59, tzinfo=UTC)]
    agent = Agent()
    manager = TaskManager(agent)
    model = FakeModelClient()
    service = ScheduledTasks(root_dir=tmp_path / "schedules", manager=manager,
                             session=IntentSession(timezone="Asia/Shanghai"), model=model,
                             clock=lambda: now[0])
    await service.start()
    # Advance fake time explicitly so tests do not depend on the polling interval.
    await service.stop()
    try:
        yield service, manager, agent, now
    finally:
        await service.stop()
        await manager.close()
        service.close()


def test_calendar_rules_use_timezone_and_skip_dst_gap():
    now = datetime(2026, 11, 4, 3, 59, tzinfo=UTC)
    assert daily().next_after(now) == datetime(2026, 11, 4, 4, tzinfo=UTC)
    assert daily().next_after(now + timedelta(minutes=1)) == datetime(2026, 11, 5, 4, tzinfo=UTC)
    weekly = ScheduleRule(kind="weekly", timezone="Asia/Shanghai", time="12:00", weekday=0)
    assert weekly.next_after(now) == datetime(2026, 11, 9, 4, tzinfo=UTC)
    dst = ScheduleRule(kind="daily", timezone="America/New_York", time="02:30")
    assert dst.next_after(datetime(2026, 3, 8, 5, tzinfo=UTC)) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    weekly_gap = ScheduleRule(kind="weekly", timezone="America/New_York", time="02:30", weekday=6)
    assert weekly_gap.next_after(datetime(2026, 3, 2, 5, tzinfo=UTC)) == datetime(2026, 3, 15, 6, 30, tzinfo=UTC)
    fold = ScheduleRule(kind="daily", timezone="America/New_York", time="01:30")
    assert fold.next_after(datetime(2026, 11, 1, 5, 31, tzinfo=UTC)) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize("fields", [
    {"kind": "interval", "interval_seconds": 0},
    {"kind": "interval", "interval_seconds": True},
    {"kind": "interval", "interval_seconds": 120, "time": "12:00"},
    {"kind": "daily", "time": "24:00"},
    {"kind": "weekly", "time": "12:00", "weekday": 7},
    {"kind": "once", "at": "2026-11-04T12:00:00"},
])
def test_invalid_rules_are_rejected(fields):
    with pytest.raises(ValidationError):
        ScheduleRule(timezone="Asia/Shanghai", **fields)


async def test_daily_trigger_is_independent_and_uses_execution_date(runtime):
    service, manager, agent, now = runtime
    entry = create(service)
    assert not manager.tasks
    now[0] = datetime(2026, 11, 4, 4, tzinfo=UTC)
    service.tick()
    service.tick()
    await asyncio.sleep(0)
    assert len(manager.tasks) == 1
    session, text, kwargs = agent.inputs[0]
    assert text == entry.prompt and "当天" in text
    assert kwargs["received_at"] == now[0] and session.timezone == "Asia/Shanghai"
    assert not session.messages and not session.questions
    assert entry.next_run == datetime(2026, 11, 5, 4, tzinfo=UTC)
    agent.release.set()
    await manager.drain()
    event = await manager.events.get()
    service.record_event(event)
    assert entry.last_state == "completed"


async def test_interval_manual_run_overlap_and_original_alignment(runtime):
    service, manager, agent, now = runtime
    entry = create(service, rule=ScheduleRule(kind="interval", timezone="Asia/Shanghai", interval_seconds=7200))
    due = now[0] + timedelta(hours=2)
    assert entry.next_run == due
    response = await service.handle(f"/schedule run {entry.schedule_id}")
    assert entry.last_task_id in response and entry.next_run == due
    assert "上一轮" in await service.handle(f"/schedule run {entry.schedule_id}")
    now[0] = due
    service.tick()
    assert len(manager.tasks) == 1 and entry.last_skip == "busy"
    assert entry.next_run == due + timedelta(hours=2)
    agent.release.set()
    await manager.drain()
    now[0] = due + timedelta(hours=7, minutes=3)
    service.tick()
    assert len(manager.tasks) == 2
    assert entry.next_run == due + timedelta(hours=8)


async def test_update_delete_exact_names_and_no_execution_side_effect(runtime):
    service, manager, agent, now = runtime
    first = create(service)
    create(service)
    assert "同名" in await service.handle("/schedule delete GitHub趋势")
    due = first.next_run
    service.apply(ScheduleCommand(action="update", target=first.schedule_id, name="新闻"))
    updated = service.entries[first.schedule_id]
    assert updated.next_run == due and updated.prompt == first.prompt
    service.apply(ScheduleCommand(action="update", target="新闻", rule=ScheduleRule(
        kind="daily", timezone="Asia/Shanghai", time="13:00")))
    assert service.entries[first.schedule_id].next_run == datetime(2026, 11, 4, 5, tzinfo=UTC)
    assert not manager.tasks
    service.apply(ScheduleCommand(action="run", target=first.schedule_id))
    running = service.entries[first.schedule_id].last_task_id
    service.apply(ScheduleCommand(action="delete", target="新闻"))
    assert first.schedule_id not in service.entries and not manager.tasks[running].token.cancelled


async def test_restart_skips_missed_runs_and_never_replays_interrupted_work(runtime):
    service, manager, agent, now = runtime
    daily_entry = create(service)
    interval = create(service, rule=ScheduleRule(kind="interval", timezone="UTC", interval_seconds=7200))
    once = create(service, rule=ScheduleRule(kind="once", timezone="UTC", at=now[0] + timedelta(hours=1)))
    service.apply(ScheduleCommand(action="run", target=daily_entry.schedule_id))
    service.close()
    await manager.close()
    now[0] += timedelta(days=2)
    other_manager = TaskManager(Agent())
    restored = ScheduledTasks(root_dir=service.root, manager=other_manager,
                              session=IntentSession(timezone="UTC"), model=service.model,
                              clock=lambda: now[0])
    try:
        await restored.start()
        await restored.stop()
        assert len(restored.entries) == 3 and not other_manager.tasks
        assert restored.entries[daily_entry.schedule_id].last_state == "interrupted"
        assert restored.entries[daily_entry.schedule_id].last_skip == "missed"
        assert restored.entries[interval.schedule_id].next_run == now[0] + timedelta(hours=2)
        assert restored.entries[once.schedule_id].next_run is None
        assert "离线" in restored.apply(ScheduleCommand(action="list"))
    finally:
        await other_manager.close()
        restored.close()


async def test_one_time_runs_once_and_past_time_is_rejected(runtime):
    service, manager, agent, now = runtime
    entry = create(service, rule=ScheduleRule(kind="once", timezone="UTC", at=now[0] + timedelta(minutes=1)))
    now[0] += timedelta(minutes=1)
    service.tick()
    service.tick()
    assert len(manager.tasks) == 1 and entry.next_run is None
    with pytest.raises(ValueError, match="PAST_TIME"):
        service.apply(ScheduleCommand(action="update", target=entry.schedule_id, rule=entry.rule))


async def test_full_queue_rejects_occurrence_without_busy_loop_retry(runtime):
    service, manager, agent, now = runtime
    entry = create(service)
    for i in range(32):
        manager.submit(str(i), IntentSession())
    now[0] = entry.next_run
    service.tick()
    assert entry.last_state == "rejected" and entry.next_run > now[0]
    assert "未接收执行" in service.apply(ScheduleCommand(action="list"))
    service.tick()
    assert len(manager.tasks) == 32


async def test_schedule_storage_is_private_exclusive_and_survives_delete(runtime):
    service, manager, agent, now = runtime
    entry = create(service)
    assert service.root.stat().st_mode & 0o777 == 0o700
    assert (service.root / "schedules.sqlite3").stat().st_mode & 0o777 == 0o600
    other = ScheduledTasks(root_dir=service.root, manager=manager, session=service.session, model=service.model)
    with pytest.raises(OSError):
        await other.start()
    service.apply(ScheduleCommand(action="delete", target=entry.schedule_id))
    assert service.db.execute("SELECT count(*) FROM schedules").fetchone()[0] == 0


async def test_natural_language_commands_are_validated_and_only_apply_requested_fields(runtime):
    service, manager, agent, now = runtime
    responses = [
        {"action": "create", "name": "GitHub趋势", "prompt": "生成当天GitHub趋势的精美HTML页面",
         "rule": daily().model_dump(mode="json")},
        {"action": "update", "target": "GitHub趋势", "rule": {
            "kind": "daily", "timezone": "Asia/Shanghai", "time": "13:00"}},
        {"action": "run", "target": "GitHub趋势"},
        {"action": "delete", "target": "GitHub趋势"},
    ]
    requests = []

    class Model:
        async def generate(self, request):
            requests.append(request)
            return SimpleNamespace(payload=responses.pop(0))

    service.model = Model()
    assert "已保存" in await service.handle("/schedule 每天中午12点生成github趋势的精美页面")
    entry = list(service.entries.values())[0]
    assert "已更新" in await service.handle("/schedule 把GitHub趋势改到每天下午一点")
    assert service.entries[entry.schedule_id].prompt == entry.prompt
    assert "已触发" in await service.handle("/schedule 立即运行GitHub趋势")
    assert "已删除" in await service.handle("/schedule 删除GitHub趋势")
    assert not service.entries and len(manager.tasks) == 1
    assert requests[0].input_data["current_datetime"].startswith("2026-11-04T11:59")
    assert requests[1].input_data["schedules"][0]["id"] == entry.schedule_id
    assert requests[1].input_data["schedules"][0]["prompt"] == entry.prompt
    assert requests[0].role == "schedule_command" and requests[0].max_output_tokens == 4096


async def test_invalid_model_response_and_ambiguous_input_cannot_mutate_schedules(runtime):
    service, manager, agent, now = runtime
    class Model:
        async def generate(self, request):
            return SimpleNamespace(payload={"action": "delete"})

    service.model = Model()
    create(service)
    assert "未完成" in await service.handle("/schedule 删除刚才那个")
    assert len(service.entries) == 1 and not manager.tasks
    assert await service.handle("ordinary task") is None


async def test_model_cannot_guess_a_deletion_target_from_an_ambiguous_description(runtime):
    service, manager, agent, now = runtime
    entry = create(service)

    class Model:
        async def generate(self, request):
            return SimpleNamespace(payload={"action": "delete", "target": entry.schedule_id})

    service.model = Model()
    assert "完整任务名称或编号" in await service.handle("/schedule 删除刚才那个任务")
    assert len(service.entries) == 1


async def test_delayed_events_keep_their_origin_after_another_run_or_deletion(runtime):
    service, manager, agent, now = runtime
    entry = create(service)
    service.apply(ScheduleCommand(action="run", target=entry.schedule_id))
    first_id = entry.last_task_id
    agent.release.set()
    await manager.drain()
    service.apply(ScheduleCommand(action="run", target=entry.schedule_id))
    second_id = entry.last_task_id
    first_event = await manager.events.get()
    service.record_event(first_event)
    assert first_event.task_id == first_id and entry.last_state == "queued"
    assert service.for_task(first_id).schedule_id == entry.schedule_id
    service.apply(ScheduleCommand(action="delete", target=entry.schedule_id))
    assert service.for_task(second_id).schedule_id == entry.schedule_id


async def test_scheduled_clarification_blocks_overlap_until_same_execution_is_answered(runtime):
    from karen.intent.recognizer import ClarificationItem

    service, manager, agent, now = runtime

    async def clarify(session, text, **kwargs):
        if not session.questions:
            return TaskTurn(session.model_copy(update={
                "clarification_items": (ClarificationItem(question_id="q", text="保存到哪里？", asked_after=1),),
            }))
        return TaskTurn(session.model_copy(update={"reply": "done"}))

    agent.advance = clarify
    entry = create(service)
    service.apply(ScheduleCommand(action="run", target=entry.schedule_id))
    task_id = entry.last_task_id
    await manager.drain()
    event = await manager.events.get()
    service.record_event(event)
    assert entry.last_state == "waiting"
    now[0] = entry.next_run
    service.tick()
    assert entry.last_skip == "busy" and len(manager.tasks) == 1
    manager.submit("/tmp/github.html", service.session, task_id=task_id)
    await manager.drain()
    event = await manager.events.get()
    service.record_event(event)
    assert event.task_id == task_id and entry.last_state == "completed"


async def test_real_transport_uses_schedule_budget_and_one_truncation_repair(runtime, monkeypatch):
    from functools import partial

    import httpx
    from langchain_deepseek import ChatDeepSeek

    import karen.model.models as models

    service, manager, agent, now = runtime
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    payload = {"action": "create", "name": "趋势", "prompt": "生成当天GitHub趋势的精美HTML页面",
               "rule": daily().model_dump(mode="json")}

    async def send(request):
        calls.append(json.loads(request.content))
        truncated = len(calls) == 1
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "length" if truncated else "stop",
                         "message": {"role": "assistant", "content": '{"action":' if truncated else json.dumps(payload)}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        monkeypatch.setattr(models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http))
        service.model = models.deepseek_client(thinking=False)
        assert "已保存" in await service.handle("/schedule 每天12点生成趋势页面")
    assert len(service.entries) == 1 and not manager.tasks
    assert [call["max_tokens"] for call in calls] == [4096, 8192]
    assert all(call["thinking"] == {"type": "disabled"} for call in calls)
    assert all(call["model"] == "deepseek-flash" for call in calls)
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)


async def test_cli_startup_assembles_persistent_schedules_in_actual_timezone(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    from karen import cli

    class Memory:
        async def start(self):
            pass

        async def close(self):
            pass

    payload = {"action": "create", "name": "GitHub趋势", "prompt": "生成当天GitHub趋势的精美HTML页面",
               "rule": daily().model_dump(mode="json")}
    client = FakeModelClient([payload])
    lines = iter(["/schedule 每天中午12点生成GitHub趋势的精美页面", "/schedule list"])

    async def read_input():
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "create_memory", lambda *args, **kwargs: Memory())
    monkeypatch.setattr(cli, "create_workflows", lambda *args: None)
    monkeypatch.setattr(cli, "create_observer", lambda: cli.Observer())
    monkeypatch.setattr(cli, "deepseek_client", lambda **kwargs: client)
    monkeypatch.setattr(cli, "read_input", read_input)
    assert await cli.converse(timezone="Asia/Shanghai") == 0
    assert (tmp_path / ".Karne/schedules/cli/schedules.sqlite3").exists()
    assert client.requests[0].input_data["default_timezone"] == "Asia/Shanghai"
    output = capsys.readouterr().out
    assert "已保存定时任务" in output and "12:00（Asia/Shanghai）" in output


@pytest.mark.parametrize("json_output", [True, False])
async def test_console_management_bypasses_task_focus_and_delivers_scheduled_results(tmp_path, json_output):
    agent = Agent()
    agent.release.set()
    manager = TaskManager(agent)
    now = datetime(2026, 11, 4, 3, tzinfo=UTC)
    response = {"action": "create", "name": "GitHub趋势", "prompt": "生成当天GitHub趋势页面",
                "rule": daily().model_dump(mode="json")}

    class Model:
        async def generate(self, request):
            return SimpleNamespace(payload=response)

    service = ScheduledTasks(root_dir=tmp_path, manager=manager, session=IntentSession(),
                             model=Model(), clock=lambda: now)
    displayed = []
    count = 0

    async def read_input():
        nonlocal count
        count += 1
        if count == 1:
            return "/schedule 每天中午十二点生成GitHub趋势页面"
        if count == 2:
            return "/schedule run " + next(iter(service.entries))
        async with asyncio.timeout(2):
            while not any("done" in text for text in displayed):
                await asyncio.sleep(0)
        raise EOFError

    try:
        assert await run_console(manager, service.session, read_input=read_input,
                                 display=lambda text, **kwargs: displayed.append(text),
                                 schedules=service, json_output=json_output) == 0
        assert len(agent.inputs) == 1
        if json_output:
            tasks = [json.loads(s) for s in displayed if json.loads(s)["type"] == "task"]
            assert len(tasks) == 1 and tasks[0]["schedule_id"] in service.entries
        else:
            assert any("定时任务" in s and "done" in s for s in displayed)
        assert next(iter(service.entries.values())).last_state == "completed"
    finally:
        service.close()
