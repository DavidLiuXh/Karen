import json
from zoneinfo import ZoneInfoNotFoundError

import pytest
from dynamic_graph import FakeModelClient, RunResult

from karen import IntentSession, TaskTurn, cli


class MemoryStub:
    async def start(self):
        pass

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def local_memory_only(monkeypatch):
    monkeypatch.setattr(cli, "create_memory", lambda model, **kwargs: MemoryStub())
    monkeypatch.setattr(cli, "create_observer", lambda: cli.Observer())


class DisplayAgent:
    def record_response(self, session, text, **kwargs):
        return ()


@pytest.mark.parametrize("api_key", [None, "test-key"])
async def test_cli_registers_and_authorizes_all_available_tools(monkeypatch, capsys, api_key):
    if api_key is None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TAVILY_API_KEY", api_key)
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    agents = []
    actual_karen = cli.Karen

    def capture_agent(**kwargs):
        agent = actual_karen(**kwargs)
        agents.append(agent)
        return agent

    def end_input(prompt):
        raise EOFError

    monkeypatch.setattr(cli, "Karen", capture_agent)
    monkeypatch.setattr("builtins.input", end_input)
    assert await cli.converse() == 0
    from karen.capabilities import all_capabilities_policy

    policy = all_capabilities_policy(agents[0].engine)
    expected_tools = {
        "file.read_text@1.0.0",
        "file.write_text@1.0.0",
        "browser.open_local_page@1.0.0",
        "web.fetch@1.0.0",
    }
    if api_key:
        expected_tools.add("tavily.search@1.0.0")
    assert set(policy.allowed_tools) == expected_tools
    assert set(policy.allowed_side_effect_tools) == {
        "file.write_text@1.0.0",
        "browser.open_local_page@1.0.0",
    }
    assert len(policy.allowed_reducers) == 3
    output = capsys.readouterr().out
    assert ("未配置 TAVILY_API_KEY" in output) == (api_key is None)
    assert "test-key" not in output


def test_readable_result_displays_answer_sources_and_limitations():
    result = RunResult(
        run_id="test-run",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={
            "answer": "HTML 已保存到 /tmp/github.html，并已请求浏览器打开。",
            "evidence": [{"source": "https://github.com/trending", "text": "共 15 个仓库"}],
            "limitations": [{"description": "仅确认打开请求已发出。"}],
        },
    )
    displayed = cli.format_result(result)
    assert "HTML 已保存到 /tmp/github.html" in displayed
    assert "https://github.com/trending：共 15 个仓库" in displayed
    assert "仅确认打开请求已发出。" in displayed
    assert "execution_status" not in displayed and "test-run" not in displayed


@pytest.mark.parametrize(
    "status,complete,expected",
    [
        ("FAILED", False, "任务执行失败"),
        ("CANCELLED", False, "任务已取消"),
        ("COMPLETED", False, "执行已结束，但结果不完整"),
    ],
)
def test_unsuccessful_result_labels_partial_answer_and_diagnostics(status, complete, expected):
    result = RunResult(
        run_id="test-run",
        execution_status=status,
        output_complete=complete,
        outputs={"answer": "已完成部分抓取。"},
        diagnostics=[{"code": "TOOL_FAILED", "phase": "execution", "message": "文件写入失败。"}],
    )
    displayed = cli.format_result(result)
    assert displayed.startswith(expected)
    assert "部分回答" in displayed
    assert "已完成部分抓取。" in displayed
    assert "TOOL_FAILED" in displayed and "文件写入失败。" in displayed


def test_custom_result_fields_are_preserved():
    result = RunResult(
        run_id="test-run",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={"draft": "中文邮件草稿", "repositories": [{"name": "example/repo"}]},
    )
    displayed = cli.format_result(result)
    assert "draft：\n中文邮件草稿" in displayed
    assert "repositories" in displayed and "example/repo" in displayed


@pytest.mark.parametrize("json_output", [False, True])
async def test_conversation_displays_readable_answer_or_full_json(monkeypatch, capsys, json_output):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    inputs = iter(["写邮件", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    result = RunResult(
        run_id="test-run",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={"answer": "中文邮件草稿", "evidence": [], "limitations": []},
    )

    class CompletedAgent(DisplayAgent):
        async def advance(self, session, user_input):
            assert session.timezone == "America/New_York"
            return TaskTurn(IntentSession(), result)

    monkeypatch.setattr(cli, "Karen", lambda **kwargs: CompletedAgent())
    assert await cli.converse(json_output=json_output, timezone="America/New_York") == 0
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output) == result.model_dump(mode="json")
    else:
        assert output == "Karen：中文邮件草稿\n"


async def test_invalid_cli_timezone_stops_before_model_initialization(capsys):
    assert await cli.converse(timezone="Not/A_Timezone") == 1
    assert "--timezone" in capsys.readouterr().out


async def test_failed_timezone_detection_requires_explicit_user_timezone(monkeypatch, capsys):
    def unavailable_timezone(**kwargs):
        raise ZoneInfoNotFoundError("local timezone unavailable")

    monkeypatch.setattr(cli, "IntentSession", unavailable_timezone)
    assert await cli.converse() == 1
    assert "请使用 --timezone" in capsys.readouterr().out


@pytest.mark.parametrize("first_status", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_cli_continues_after_each_task(monkeypatch, capsys, first_status):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    inputs = iter(["第一项任务", "第二项任务", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    received = []

    class TwoTaskAgent(DisplayAgent):
        async def advance(self, session, user_input):
            received.append(user_input)
            first = len(received) == 1
            return TaskTurn(
                session,
                RunResult(
                    run_id="test-run",
                    execution_status=first_status if first else "COMPLETED",
                    output_complete=first_status == "COMPLETED" if first else True,
                    outputs={"answer": "第一项任务结果" if first else "第二项任务结果"},
                ),
            )

    monkeypatch.setattr(cli, "Karen", lambda **kwargs: TwoTaskAgent())
    assert await cli.converse() == 0
    assert received == ["第一项任务", "第二项任务"]
    output = capsys.readouterr().out
    assert "第一项任务结果" in output and "第二项任务结果" in output


async def test_cli_returns_last_task_failure_code_on_exit(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    inputs = iter(["执行任务", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    class FailedAgent(DisplayAgent):
        async def advance(self, session, user_input):
            return TaskTurn(session, RunResult(run_id="test-run", execution_status="FAILED"))

    monkeypatch.setattr(cli, "Karen", lambda **kwargs: FailedAgent())
    assert await cli.converse() == 1


async def test_cancelled_console_read_does_not_join_blocked_input(monkeypatch):
    import asyncio
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocked_input(prompt):
        started.set()
        release.wait()
        return "late input"

    monkeypatch.setattr("builtins.input", blocked_input)
    reading = asyncio.create_task(cli.read_input())
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reading, 0.2)
        assert not release.is_set()
    finally:
        release.set()
