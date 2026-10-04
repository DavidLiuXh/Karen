import errno
import json
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
from contextlib import contextmanager
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


def test_readable_result_does_not_append_internal_evidence_and_limitations():
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
    assert displayed == result.outputs["answer"]
    assert result.outputs["evidence"] and result.outputs["limitations"]
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


@pytest.mark.parametrize(
    "keystrokes",
    [
        "明天北京是否还有大错".encode() + b"\x7f" + "风".encode(),
        "明天北海".encode() + b"\x08" + "京是否还有大风".encode(),
        "明天北错错".encode() + b"\x7f\x7f" + "京是否还有大风".encode(),
        "明天北海是否还有大风".encode() + b"\x1b[D" * 7 + b"\x1b[3~" + "京".encode(),
    ],
)
def test_real_console_submits_edited_chinese_text(keystrokes):
    import os
    import pty
    import select
    import subprocess
    import sys
    import time

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import asyncio, json; from karen.cli import read_input; "
            "print('RESULT=' + json.dumps(asyncio.run(read_input())), flush=True)",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm", "LC_ALL": "en_US.UTF-8", "INPUTRC": os.devnull},
    )
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 30

    def read_until(marker):
        nonlocal output
        while marker not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([master], [], [], remaining)[0], output
            output += os.read(master, 65536)

    try:
        read_until("你：".encode())
        deadline = time.monotonic() + 10  # Editing has its own budget after interpreter startup.
        os.write(master, keystrokes + b"\r")
        read_until(b"RESULT=")
        while b"\n" not in output.split(b"RESULT=", 1)[1]:
            assert select.select([master], [], [], 1)[0], output
            output += os.read(master, 65536)
        submitted = json.loads(output.split(b"RESULT=", 1)[1].splitlines()[0])
        assert submitted == "明天北京是否还有大风"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)


def test_piped_input_remains_supported():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, json; from karen.cli import read_input; "
            "print('RESULT=' + json.dumps(asyncio.run(read_input())))",
        ],
        input="明天北京是否还有大风\n",
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout.split("RESULT=", 1)[1]) == "明天北京是否还有大风"


@pytest.fixture
def local_cli_script(tmp_path):
    """Exercise the real CLI lifecycle without models, memory data or external calls."""
    script = tmp_path / "console_cli.py"
    script.write_text(
        """
import asyncio
import json
import os
import sys
import termios
from pathlib import Path
from dynamic_graph import FakeModelClient
from karen import cli

def terminal_state():
    attrs = termios.tcgetattr(sys.stdin)
    return [*attrs[:6], [c[0] if isinstance(c, bytes) else c for c in attrs[6]]]

before = terminal_state()

class Memory:
    async def start(self):
        pass

    async def close(self):
        await asyncio.sleep(0.05)
        Path(os.environ['KAREN_TEST_PERSISTED']).write_text(json.dumps({'before': before, 'after': terminal_state()}))
        print('PERSISTENCE_COMPLETE', flush=True)

class WaitingAgent:
    async def advance(self, session, user_input):
        print('TASK_STARTED', flush=True)
        await asyncio.Event().wait()

cli.create_memory = lambda model, **kwargs: Memory()
cli.create_observer = lambda: cli.Observer()
cli.deepseek_client = FakeModelClient
cli.Karen = lambda **kwargs: WaitingAgent()
cli.main()
"""
    )
    return script, tmp_path / "persisted.txt"


@contextmanager
def terminal_command(command, *, extra_env):
    master, slave = pty.openpty()
    # Claim the controlling terminal so writing Ctrl+C exercises a real terminal signal.
    wrapper = (
        "import fcntl, os, sys, termios; "
        "fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", wrapper, *command],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={
            **os.environ,
            "TERM": "xterm",
            "LC_ALL": "en_US.UTF-8",
            "INPUTRC": os.devnull,
            **extra_env,
        },
    )
    os.close(slave)
    pending = b""

    def read_until(marker):
        nonlocal pending
        deadline = time.monotonic() + 10
        while marker not in pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([master], [], [], remaining)[0], pending
            pending += os.read(master, 65536)
        end = pending.index(marker) + len(marker)
        received, pending = pending[:end], pending[end:]
        return received

    def wait_for_exit():
        nonlocal pending
        deadline = time.monotonic() + 5
        while process.poll() is None:
            assert time.monotonic() < deadline, pending
            # Drain the PTY: macOS can block shell exit while terminal output is pending.
            if select.select([master], [], [], 0.05)[0]:
                try:
                    pending += os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
        return process.returncode

    try:
        yield process, master, read_until, wait_for_exit
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
        process.wait(timeout=5)


@pytest.mark.parametrize("exit_method", ["ctrl_c", "sigint", "task_ctrl_c", "eof", "exit"])
def test_cli_exit_restores_terminal_and_waits_for_persistence(local_cli_script, exit_method):
    script, persisted = local_cli_script
    with terminal_command(
        [sys.executable, "-u", str(script), "--timezone", "Asia/Shanghai"],
        extra_env={"KAREN_TEST_PERSISTED": str(persisted)},
    ) as (process, master, read_until, wait_for_exit):
        read_until("你：".encode())
        if exit_method == "task_ctrl_c":
            os.write(master, "执行测试任务\r".encode())
            read_until(b"TASK_STARTED")
        elif exit_method in {"ctrl_c", "sigint"}:
            os.write(master, "未提交的中文输入".encode())
        if exit_method == "sigint":
            process.send_signal(signal.SIGINT)
        elif exit_method == "eof":
            os.write(master, b"\x04")
        elif exit_method == "exit":
            os.write(master, b"/exit\r")
        else:
            os.write(master, b"\x03")
        read_until(b"PERSISTENCE_COMPLETE")
        assert wait_for_exit() == (0 if exit_method in {"eof", "exit"} else 130)
        state = json.loads(persisted.read_text())
        # macOS sets PENDIN when changing terminal modes; it is a kernel replay flag.
        for attrs in state.values():
            attrs[3] &= ~getattr(termios, "PENDIN", 0)
        assert state["after"] == state["before"]


def test_shell_accepts_another_command_after_karen_ctrl_c(local_cli_script):
    script, persisted = local_cli_script
    with terminal_command(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        extra_env={"KAREN_TEST_PERSISTED": str(persisted), "PS1": "SHELL_READY> "},
    ) as (_, master, read_until, wait_for_exit):
        read_until(b"SHELL_READY> ")
        command = shlex.join([sys.executable, "-u", str(script), "--timezone", "Asia/Shanghai"])
        os.write(master, command.encode() + b"\r")
        read_until("你：".encode())
        os.write(master, "未提交的任务".encode() + b"\x03")
        read_until(b"PERSISTENCE_COMPLETE")
        read_until(b"SHELL_READY> ")
        # Split the marker so echoed command text cannot masquerade as execution.
        os.write(master, b"printf 'SHELL_%s\\n' INPUT_WORKS\r")
        read_until(b"SHELL_INPUT_WORKS\r\n")
        assert json.loads(persisted.read_text())["after"][3] & termios.ECHO
        read_until(b"SHELL_READY> ")
        os.write(master, b"exit\r")
        read_until(b"exit\r\n")
        assert wait_for_exit() == 0


def test_answer_preserves_requested_sources_and_material_limitations():
    answer = "来源：[天气预报](https://example.com/weather)。仅有上午预报，傍晚风力还无法确定。"
    result = RunResult(
        run_id="sources",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={
            "answer": answer,
            "evidence": [{"source": "internal-event-id", "text": "raw"}],
            "limitations": [{"description": "INTERNAL_BUDGET_CODE"}],
        },
    )
    assert cli.format_result(result) == answer


def test_evidence_and_limitations_remain_visible_without_an_answer():
    result = RunResult(
        run_id="fallback",
        execution_status="COMPLETED",
        output_complete=True,
        outputs={
            "evidence": [{"source": "https://example.com", "text": "找到部分资料"}],
            "limitations": [{"description": "仍缺傍晚预报"}],
        },
    )
    text = cli.format_result(result)
    assert "https://example.com" in text and "仍缺傍晚预报" in text


@pytest.mark.parametrize("json_output", [False, True])
async def test_cli_displays_direct_reply_without_execution_result(monkeypatch, capsys, json_output):
    from karen.intent import InputRouting

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    inputs = iter(["我住在北京", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    route = InputRouting(
        input_types=["information"], handling="respond", task_relation="new", reason="个人事实告知"
    )
    displayed = []

    class DirectAgent:
        async def advance(self, session, user_input):
            return TaskTurn(
                IntentSession(timezone="UTC", routing=route, reply="好的，了解了，你住在北京。")
            )

        def record_response(self, session, text, **kwargs):
            displayed.append(text)
            return ()

    monkeypatch.setattr(cli, "Karen", lambda **kwargs: DirectAgent())
    assert await cli.converse(json_output=json_output, timezone="UTC") == 0
    output = capsys.readouterr().out
    if json_output:
        data = json.loads(output)
        assert data["outcome"] == "replied" and data["answer"] == "好的，了解了，你住在北京。"
        assert "execution_status" not in data and "run_id" not in data
    else:
        assert output == "Karen：好的，了解了，你住在北京。\n"
    assert len(displayed) == 1 and "好的，了解了" in displayed[0]
