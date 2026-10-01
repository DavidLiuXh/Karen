import json
from zoneinfo import ZoneInfoNotFoundError

import pytest
from dynamic_graph import FakeModelClient, RunResult

from karen import IntentSession, TaskTurn, cli


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
    monkeypatch.setattr("builtins.input", lambda prompt: "写邮件")
    result = RunResult(
        run_id="test-run",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={"answer": "中文邮件草稿", "evidence": [], "limitations": []},
    )

    class CompletedAgent:
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
