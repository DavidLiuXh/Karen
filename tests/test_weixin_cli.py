import sqlite3
import sys
from pathlib import Path

import pytest
from dynamic_graph import FakeModelClient

from karen import cli
from karen.capabilities import all_capabilities_policy
from karen.channels import weixin
from karen.channels.weixin.api import API_URL
from karen.channels.weixin.auth import Credentials


def test_wechat_flag_selects_channel_without_changing_cli_defaults(monkeypatch):
    calls = []

    async def converse(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "converse", converse)
    monkeypatch.setattr(sys, "argv", ["karen", "--wechat", "--observe", "--timezone", "UTC"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert calls == [{"json_output": False, "timezone": "UTC", "observe_port": 8765, "wechat": True}]


@pytest.mark.parametrize("flags", [
    ["--wechat", "--json"], ["--wechat-login", "--wechat"],
    ["--wechat-login", "--observe"], ["--wechat-login", "--json"],
])
def test_invalid_channel_flags_are_rejected(monkeypatch, flags):
    monkeypatch.setattr(sys, "argv", ["karen", *flags])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2


def test_login_command_does_not_initialize_llm(monkeypatch):
    calls = []

    async def bind(**kwargs):
        calls.append(kwargs)

    def unexpected_model(**kwargs):
        pytest.fail("QR binding must work without a model key")

    monkeypatch.setattr(cli, "bind_weixin", bind)
    monkeypatch.setattr(cli, "deepseek_client", unexpected_model)
    monkeypatch.setattr(sys, "argv", ["karen", "--wechat-login"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert calls == [{"force": True}]


async def test_wechat_runtime_uses_existing_agent_memory_and_observer(tmp_path, monkeypatch):
    closed, channels = [], []
    credentials = Credentials("secret", "bot", "owner", API_URL)
    observer = cli.Observer(tmp_path / "observability")

    async def bind():
        return credentials

    class Memory:
        async def start(self):
            pass

        async def close(self):
            closed.append(True)

    memory = Memory()

    class Channel:
        def __init__(self, **kwargs):
            channels.append(kwargs)

        async def run(self):
            value = channels[-1]
            assert value["agent"].memory is memory
            assert value["agent"].observer is observer
            assert value["initial_session"].timezone == "Asia/Shanghai"
            policy = all_capabilities_policy(value["agent"].engine)
            assert "weixin.prepare_file@1.0.0" in policy.allowed_tools
            assert "weixin.prepare_file@1.0.0" in policy.allowed_side_effect_tools

    monkeypatch.setattr(cli, "bind_weixin", bind)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "create_observer", lambda: observer)
    monkeypatch.setattr(cli, "create_memory", lambda *args, **kwargs: memory)
    monkeypatch.setattr(cli, "deepseek_client", lambda **kwargs: FakeModelClient())
    monkeypatch.setattr(weixin, "WeixinChannel", Channel)
    assert await cli.converse(wechat=True, timezone="Asia/Shanghai") == 0
    assert closed == [True]
    assert len(channels) == 1
    assert channels[0]["store"].db is None


async def test_binding_persists_credentials_and_reuses_them(tmp_path, monkeypatch):
    calls = []
    credentials = Credentials("secret", "bot", "owner", API_URL)

    async def login(http, **kwargs):
        calls.append(kwargs)
        return credentials

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(weixin, "login", login)
    assert await cli.bind_weixin() == credentials
    assert await cli.bind_weixin() == credentials
    assert len(calls) == 1
    assert await cli.bind_weixin(force=True) == credentials
    assert calls[-1]["previous"] == credentials


def test_expired_session_is_reported_with_rebinding_command(monkeypatch, capsys):
    async def converse(**kwargs):
        raise weixin.WeixinError("WEIXIN_SESSION_EXPIRED")

    monkeypatch.setattr(cli, "converse", converse)
    monkeypatch.setattr(sys, "argv", ["karen", "--wechat"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "--wechat-login" in capsys.readouterr().err


def test_channel_storage_failure_stops_with_safe_diagnostic(monkeypatch, capsys):
    async def converse(**kwargs):
        raise sqlite3.DatabaseError("private provider/database details")

    monkeypatch.setattr(cli, "converse", converse)
    monkeypatch.setattr(sys, "argv", ["karen", "--wechat"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    output = capsys.readouterr().err
    assert "WEIXIN_STORAGE_FAILED" in output
    assert "private provider/database details" not in output
