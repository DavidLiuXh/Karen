import pytest
from dynamic_graph import FakeModelClient

from karen import cli


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
        "github.trending@1.0.0",
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
