import asyncio
import json
import time
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, ModelBindings
from dynamic_graph.execution.errors import ToolCallError
from intent_helpers import TaskIntentModel
from test_execution import graph_response, ready

from karen import IntentRecognizer, IntentSession, Karen
from karen.cli import register_gmail_tools
from karen.task.resources import ExecutionResources
from karen.tools.gmail import GmailClient, GmailCredentials
from karen.tools.gmail_tools import gmail_tools


async def test_registered_tool_executes_through_engine(tmp_path):
    response = graph_response()
    node = response["graph"]["nodes"][0]
    node.pop("instruction")
    node.pop("model_role")
    node.update(kind="tool", capability={"name": "gmail.get_profile", "version": "1.0.0"})
    node["output_schema"] = {
        "type": "object",
        "properties": {"json": {"type": "string"}},
        "required": ["json"],
        "additionalProperties": False,
    }
    node["writes"][0]["output_pointer"] = "/json"
    planner = FakeModelClient([response])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(planner, FakeModelClient())
    )
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "test-access-secret"})
        return httpx.Response(200, json={"emailAddress": "synthetic@example.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GmailClient(http, GmailCredentials("id", "test-secret", "test-refresh"))
        for definition in gmail_tools(client):
            engine.register_tool(definition)
        assert requests == []
        agent = Karen(intent=IntentRecognizer(TaskIntentModel([ready()])), engine=engine)
        turn = await agent.advance(IntentSession(), "读取邮箱资料")
    assert turn.result.execution_status == "COMPLETED"
    assert json.loads(turn.result.outputs["draft"]) == {"emailAddress": "synthetic@example.test"}
    policy = json.loads(next(tmp_path.glob("*/policy.json")).read_text())
    assert "gmail.get_profile@1.0.0" in policy["allowed_tools"]
    assert policy["allowed_side_effect_tools"] == []
    assert len(requests) == 2
    for path in tmp_path.rglob("*.json"):
        content = path.read_text()
        assert "test-refresh" not in content
        assert "test-access-secret" not in content


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (404, "GMAIL_HISTORY_EXPIRED", False),
        (429, "GMAIL_API_FAILED", True),
        (503, "GMAIL_API_FAILED", True),
        (403, "GMAIL_API_FAILED", False),
    ],
)
async def test_tool_preserves_error_classification(status, code, retryable):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(status, json={"error": "private-body"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tools = gmail_tools(GmailClient(http, GmailCredentials("id", "secret", "refresh")))
        with pytest.raises(ToolCallError) as failed:
            await tools[3].handler(
                {"start_history_id": "123"}, SimpleNamespace(deadline=time.monotonic() + 10)
            )
    assert failed.value.code == code
    assert failed.value.retryable is retryable
    assert "private" not in str(failed.value)


async def test_tool_deadline_covers_refresh_and_read():
    async def handle(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"access_token": "token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tool = gmail_tools(GmailClient(http, GmailCredentials("id", "secret", "refresh")))[0]
        with pytest.raises(TimeoutError):
            await tool.handler({}, SimpleNamespace(deadline=time.monotonic() + 0.01))


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_cli_registration_is_conditional_and_offline(monkeypatch, capsys, count):
    names = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    for index, name in enumerate(names):
        monkeypatch.delenv(name, raising=False)
        if index < count:
            monkeypatch.setenv(name, "synthetic-value")
    engine = DynamicGraphEngine(models=ModelBindings(FakeModelClient(), FakeModelClient()))
    async with AsyncExitStack() as resources:
        await register_gmail_tools(engine, resources, ExecutionResources())
        tools = engine.list_capabilities(kind="tool")
        assert len(tools) == (4 if count == 3 else 0)
        assert all(tool.to_dict()["read_only"] for tool in tools)
        assert "synthetic-value" not in str([tool.to_dict() for tool in tools])
    assert bool(capsys.readouterr().err) == (count == 1)
