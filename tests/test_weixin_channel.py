import asyncio
import base64
import json
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from dynamic_graph import (
    CancellationToken,
    DynamicGraphEngine,
    EngineConfig,
    FakeModelClient,
    ModelBindings,
)
from dynamic_graph.contracts import CallContext
from dynamic_graph.models.client import ModelCallError
from dynamic_graph.tools import file_write_text_tool
from intent_helpers import TaskIntentModel
from test_execution import graph_response, ready

from karen import IntentRecognizer, IntentSession, Karen, TaskTurn
from karen.channels.weixin import ILinkClient, WeixinChannel, WeixinError, WeixinStore, media
from karen.channels.weixin.api import API_URL
from karen.channels.weixin.auth import Credentials
from karen.channels.weixin.media import prepare_file_tool
from karen.channels.weixin.storage import text_items
from karen.observability import Observer


def incoming(text="hello", *, identifier=1, owner="owner", **changes):
    return {"message_id": identifier, "from_user_id": owner, "message_type": 1,
            "message_state": 2, "context_token": f"context-{identifier}",
            "item_list": [{"type": 1, "text_item": {"text": text}}], **changes}


@pytest.fixture
def credentials():
    return Credentials("fake-bearer", "bot-account", "owner", API_URL)


@pytest.fixture
def store(tmp_path, credentials):
    value = WeixinStore(tmp_path / "weixin", credentials)
    try:
        yield value
    finally:
        value.close()


class Client:
    def __init__(self):
        self.batches = asyncio.Queue()
        self.sent = []
        self.send_attempts = []
        self.cursors = []
        self.fail_send = 0

    async def updates(self, cursor):
        self.cursors.append(cursor)
        batch = await self.batches.get()
        if isinstance(batch, Exception):
            raise batch
        return batch

    async def send(self, peer, item, **kwargs):
        self.send_attempts.append((peer, item, kwargs))
        if self.fail_send:
            self.fail_send -= 1
            raise WeixinError("WEIXIN_NETWORK_FAILED", retryable=True)
        self.sent.append((peer, item, kwargs))
        return {"ret": 0}

    async def notify(self, **kwargs):
        pass

    async def typing(self, *args, **kwargs):
        pass


class Agent:
    def __init__(self):
        self.inputs, self.displayed = [], []
        self.wait = None
        self.error = None

    async def advance(self, session, text, **kwargs):
        self.inputs.append((session, text))
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        return TaskTurn(session.model_copy(update={"reply": "已完成"}), trace_id="task-trace")

    def record_response(self, session, text, **kwargs):
        self.displayed.append((session, text, kwargs))
        return ()


def channel(store, credentials, *, agent=None, client=None, observer=None):
    return WeixinChannel(agent=agent or Agent(), client=client or Client(), store=store,
                         credentials=credentials, initial_session=IntentSession(timezone="Asia/Shanghai"),
                         observer=observer or Observer())


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def running(value):
    task = asyncio.create_task(value.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_inbox_deduplicates_and_persists_cursor_atomically(store):
    batch = {"msgs": [incoming(), incoming(), incoming(owner="stranger"),
                       incoming(identifier=2, group_id="group"),
                       incoming(identifier=3, message_type=2),
                       incoming(identifier=4, message_state=1),
                       incoming(identifier=5, context_token="")], "get_updates_buf": "next"}
    assert store.accept(batch, "owner") == (1, 5)
    assert store.cursor == "next"
    assert store.accept(batch, "owner") == (0, 5)
    store.db.execute("""
        CREATE TRIGGER reject_insert BEFORE INSERT ON inbox
        BEGIN SELECT RAISE(ABORT, 'test transaction failure'); END
    """)
    with pytest.raises(Exception, match="test transaction failure"):
        store.accept({"msgs": [incoming(identifier=9)], "get_updates_buf": "wrong"}, "owner")
    assert store.cursor == "next"


def test_private_state_and_exclusive_process_lock(tmp_path, credentials, store):
    assert (store.root / "state.sqlite3").stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(WeixinError, match="CHANNEL_IN_USE"):
        WeixinStore(tmp_path / "weixin", credentials)


def test_restart_preserves_clarification_and_never_reexecutes_uncertain_task(tmp_path, credentials):
    root = tmp_path / "state"
    session = IntentSession(timezone="UTC", user_context={"clarification-test": "retained"})
    store = WeixinStore(root, credentials)
    store.accept({"msgs": [incoming(identifier=1), incoming(identifier=2)],
                  "get_updates_buf": "cursor"}, "owner")
    first = store.next_input()
    store.finish(first, session, "请补充地点", record=False)
    store.next_input()  # Simulated death after work started, before a result was checkpointed.
    store.close()
    store = WeixinStore(root, credentials)
    try:
        assert store.cursor == "cursor"
        assert store.session(IntentSession()).conversation_id == session.conversation_id
        assert store.session(IntentSession()).user_context == session.user_context
        assert store.recover(session) == 1
        assert store.next_input() is None
        responses = [r[0] for r in store.db.execute("SELECT response FROM inbox ORDER BY id")]
        assert responses[0] == "请补充地点"
        assert "没有自动重做" in responses[1]
        assert store.recover(session) == 0
    finally:
        store.close()


def test_text_chunks_preserve_unicode_and_delivery_order(store):
    text = "Hello北京🙂\n" * 2000
    items = list(text_items(text))
    assert "".join(x["text_item"]["text"] for x in items) == text
    assert all(len(x["text_item"]["text"].encode()) <= 4000 for x in items)
    store.accept({"msgs": [incoming()]}, "owner")
    store.finish(store.next_input(), IntentSession(), text)
    first = store.next_output()
    store.failed(first, WeixinError("NETWORK", retryable=True))
    assert store.next_output() is None  # Later chunks cannot overtake the backing-off head.
    store.db.execute("UPDATE outbox SET status='failed' WHERE id=?", (first["id"],))
    store.db.commit()
    assert store.next_output() is None  # A permanent failure also blocks that response's suffix.


def test_delivered_text_is_recorded_even_if_its_file_is_still_pending(store):
    store.accept({"msgs": [incoming()]}, "owner")
    store.finish(store.next_input(), IntentSession(), "已准备文件", files=[{
        "path": "/private/snapshot", "name": "file.txt",
    }])
    assert store.delivered_responses() == []
    store.sent(store.next_output())
    assert len(store.delivered_responses()) == 1
    assert store.next_output()["file_path"] == "/private/snapshot"


async def test_channel_replies_once_and_excludes_unknown_users_from_agent(store, credentials):
    value = channel(store, credentials)
    await value.client.batches.put({"msgs": [incoming(), incoming(), incoming(owner="stranger")],
                                    "get_updates_buf": "next"})
    async with running(value):
        await until(lambda: value.agent.displayed)
        assert len(value.agent.inputs) == 1
        assert len(value.client.sent) == 1
        assert value.client.sent[0][0] == "owner"
        assert value.client.sent[0][2]["context_token"] == "context-1"
        assert store.cursor == "next"
        assert store.counts()["outbox"] == {"sent": 1}


async def test_outbox_retry_does_not_repeat_agent_execution(store, credentials):
    value = channel(store, credentials)
    value.client.fail_send = 1
    await value.client.batches.put({"msgs": [incoming()]})
    async with running(value):
        await until(lambda: value.client.send_attempts)
        assert not value.agent.displayed
        store.db.execute("UPDATE outbox SET next_attempt=0")
        store.db.commit()
        await until(lambda: value.agent.displayed)
        assert len(value.agent.inputs) == 1
        assert len(value.client.send_attempts) == 2
        assert value.client.send_attempts[0][2]["client_id"] == value.client.send_attempts[1][2]["client_id"]


async def test_restart_sends_durable_result_without_running_task(tmp_path, credentials):
    root = tmp_path / "channel"
    store = WeixinStore(root, credentials)
    store.accept({"msgs": [incoming()]}, "owner")
    store.finish(store.next_input(), IntentSession(), "持久化结果")
    client_id = store.next_output()["client_id"]
    store.close()
    store = WeixinStore(root, credentials)
    try:
        value = channel(store, credentials)
        async with running(value):
            await until(lambda: value.agent.displayed)
        assert value.agent.inputs == []
        assert value.client.sent[0][1]["text_item"]["text"] == "持久化结果"
        assert value.client.sent[0][2]["client_id"] == client_id
    finally:
        store.close()


async def test_poll_recovers_same_cursor_and_stops_on_expired_auth(store, credentials, monkeypatch):
    value = channel(store, credentials)
    store.accept({"msgs": [], "get_updates_buf": "original"}, "owner")

    async def delay(_):
        pass

    monkeypatch.setattr("karen.channels.weixin.service.retry_delay", delay)
    await value.client.batches.put(WeixinError("WEIXIN_NETWORK_FAILED", retryable=True))
    await value.client.batches.put({"msgs": [], "get_updates_buf": "updated"})
    await value.client.batches.put(WeixinError("WEIXIN_SESSION_EXPIRED"))
    with pytest.raises(WeixinError, match="SESSION_EXPIRED"):
        await value.run()
    assert value.client.cursors == ["original", "original", "updated"]


async def test_poll_and_delivery_continue_during_slow_execution(store, credentials):
    value = channel(store, credentials)
    value.agent.wait = asyncio.Event()
    await value.client.batches.put({"msgs": [incoming()], "get_updates_buf": "one"})
    async with running(value):
        await until(lambda: value.agent.inputs)
        await value.client.batches.put({"msgs": [incoming(identifier=2)], "get_updates_buf": "two"})
        await until(lambda: store.cursor == "two")
        assert len(value.agent.inputs) == 1
        # Progress messages use the transport outbox while the agent is occupied.
        row = store.db.execute("SELECT * FROM inbox WHERE id=1").fetchone()
        store.progress(row)
        await until(lambda: value.client.sent)
        assert not value.agent.displayed
        value.agent.wait.set()
        await until(lambda: len(value.agent.displayed) == 2)
        assert len(value.agent.inputs) == 2


async def test_model_failure_does_not_lose_pending_session(store, credentials):
    value = channel(store, credentials)
    original_id = value.session.request_id
    value.agent.error = ModelCallError("MODEL_UNAVAILABLE", "sensitive-provider-message")
    await value.client.batches.put({"msgs": [incoming()]})
    async with running(value):
        await until(lambda: value.client.sent)
    assert value.session.request_id == original_id
    assert "MODEL_UNAVAILABLE" in value.client.sent[0][1]["text_item"]["text"]
    assert "sensitive-provider-message" not in value.client.sent[0][1]["text_item"]["text"]
    assert not value.agent.displayed


async def test_cancelled_process_leaves_uncertain_execution_for_recovery(store, credentials):
    value = channel(store, credentials)
    value.agent.wait = asyncio.Event()
    await value.client.batches.put({"msgs": [incoming()]})
    async with running(value):
        await until(lambda: value.agent.inputs)
    assert store.counts()["inbox"] == {"running": 1}
    assert store.recover(value.session) == 1
    assert len(value.agent.inputs) == 1


async def test_retry_command_only_requeues_failed_deliveries(store, credentials):
    store.accept({"msgs": [incoming()]}, "owner")
    store.finish(store.next_input(), IntentSession(), "previous-result")
    row = store.next_output()
    store.failed(row, WeixinError("WEIXIN_API_REJECTED"))
    value = channel(store, credentials)
    await value.client.batches.put({"msgs": [incoming("/retry", identifier=2)]})
    async with running(value):
        await until(lambda: len(value.client.sent) == 2)
    assert value.agent.inputs == []
    assert value.client.sent[0][2]["client_id"] == row["client_id"]
    assert value.client.sent[0][2]["context_token"] == "context-2"


async def test_real_karen_clarification_survives_restart_and_executes_once(tmp_path, credentials):
    model = TaskIntentModel([
        {"decision": {"outcome": "needs_clarification", "questions": ["给谁写？"]}}, ready(),
    ])
    executor = FakeModelClient([graph_response(), {"draft": "客户您好，设计已经完成。"}])
    observer = Observer(tmp_path / "observability")
    await observer.start()
    engine = DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"),
                                models=ModelBindings(executor, executor))
    captured = []

    class Memory:
        @asynccontextmanager
        async def foreground(self):
            yield

        def submit(self, event):
            captured.append(event)
            return SimpleNamespace(event_id=f"event-{len(captured)}")

        async def recall(self, query):
            return None

    agent = Karen(intent=IntentRecognizer(model, observer=observer), engine=engine,
                  memory=Memory(), observer=observer)
    root = tmp_path / "channel"
    store = WeixinStore(root, credentials)
    try:
        first = channel(store, credentials, agent=agent, observer=observer)
        received_at = datetime(2026, 10, 7, 15, 59, tzinfo=UTC)
        await first.client.batches.put({"msgs": [incoming(
            "写邮件", create_time_ms=int(received_at.timestamp() * 1000),
        )]})
        async with running(first):
            await until(lambda: first.client.sent)
        assert first.client.sent[0][1]["text_item"]["text"] == "给谁写？"
        assert first.session.reference_time_utc == received_at
        assert first.session.user_context["reply_channel"] == "weixin"
        request_id = first.session.request_id
        assert executor.requests == []
        store.close()
        store = WeixinStore(root, credentials)
        second = channel(store, credentials, agent=agent, observer=observer)
        await second.client.batches.put({"msgs": [incoming("给客户", identifier=2)]})
        async with running(second):
            await until(lambda: store.delivered_responses() == [] and second.client.sent)
        assert second.session.request_id == request_id
        assert second.session.reference_time_utc == received_at
        assert second.session.goal is not None
        assert len(executor.requests) == 2
        assert "客户您好" in second.client.sent[0][1]["text_item"]["text"]
        assert [e.payload["content"] for e in captured if e.event_type == "user_message"] == ["写邮件", "给客户"]
        assert next(e for e in captured if e.event_type == "user_message").occurred_at == received_at
        assert any(e.event_type == "task_result" for e in captured)
        assert any(e.event_type == "assistant_message" and "客户您好" in e.payload["content"] for e in captured)
    finally:
        store.close()
        await observer.close()
    traces = "\n".join(p.read_text() for p in (observer.root_dir / "traces").glob("*.jsonl"))
    assert '"event_type": "weixin.sent"' in traces
    assert "context-1" not in traces and "fake-bearer" not in traces


@pytest.mark.parametrize("item,code", [
    ({"type": 1, "text_item": []}, "MESSAGE_INVALID"),
    ({"type": 2, "image_item": {}}, "MESSAGE_TYPE_UNSUPPORTED"),
    ({"type": 1, "text_item": {"text": ""}}, "MESSAGE_EMPTY"),
    ({"type": 1, "text_item": {"text": "a" * (64 * 1024 + 1)}}, "MESSAGE_TOO_LARGE"),
])
async def test_invalid_message_is_reported_without_entering_agent(store, credentials, item, code):
    value = channel(store, credentials)
    await value.client.batches.put({"msgs": [incoming(item_list=[item])]})
    async with running(value):
        await until(lambda: value.client.sent)
    assert not value.agent.inputs
    assert code in value.client.sent[0][1]["text_item"]["text"]


async def test_received_file_and_caption_enter_the_normal_input_pipeline(store, credentials):
    key = b"0123456789abcdef"
    item = {"type": 4, "file_item": {"file_name": "notes.txt", "len": "5", "media": {
        "aes_key": base64.b64encode(key).decode(), "encrypt_query_param": "download",
    }}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=media.encrypt(b"notes", key))
    )) as http:
        value = channel(store, credentials, client=ILinkClient(http, token="fake-bearer"))
        message = incoming("请整理这个文件")
        message["item_list"].append(item)
        text = await value.user_input(message)
    assert text.startswith("请整理这个文件")
    attachments = json.loads(text[text.index("[{"):])
    path = Path(attachments[0]["path"])
    try:
        assert path.read_text() == "notes"
        assert path.resolve().is_relative_to(Path("/tmp").resolve())
        assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        path.unlink()
        path.parent.rmdir()


async def test_real_engine_produces_and_prepares_weixin_file(store, credentials, tmp_path):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        target = str(Path(directory) / "report.html")
        write_tool, prepare_tool = file_write_text_tool(), prepare_file_tool(store)
        response = graph_response()
        graph = response["graph"]
        graph["state_fields"]["path"] = graph["state_fields"]["draft"].copy()
        graph["state_fields"]["attachment"] = graph["state_fields"]["draft"].copy()
        write = {
            "id": "produce", "kind": "tool",
            "capability": {"name": write_tool.name, "version": write_tool.version},
            "input_schema": write_tool.input_schema, "output_schema": write_tool.output_schema,
            "input_bindings": {"path": {"literal": target},
                               "content": {"literal": "<html>report</html>"}},
            "writes": [{"field": "path", "output_pointer": "/path"}], "depends_on": [],
        }
        prepare = {
            "id": "prepare", "kind": "tool",
            "capability": {"name": prepare_tool.name, "version": prepare_tool.version},
            "input_schema": prepare_tool.input_schema, "output_schema": prepare_tool.output_schema,
            "input_bindings": {"path": {"source": "state", "field": "path", "pointer": ""}},
            "writes": [{"field": "attachment", "output_pointer": "/attachment_id"}],
            "depends_on": ["produce"],
        }
        graph["nodes"][0]["depends_on"] = ["prepare"]
        graph["nodes"] = [write, prepare, *graph["nodes"]]
        model = FakeModelClient([response, {"draft": "文件已准备，正在发送。"}])
        engine = DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"),
                                    models=ModelBindings(model, model))
        engine.register_tool(write_tool)
        engine.register_tool(prepare_tool)
        goal = ready()
        goal["decision"]["goal"]["objective"] = "生成 HTML 文件并通过微信交付"
        agent = Karen(intent=IntentRecognizer(TaskIntentModel([goal])), engine=engine)
        value = channel(store, credentials, agent=agent)
        await value.client.batches.put({"msgs": [incoming("生成 HTML 文件并通过微信发给我")]})
        # Run processing without transport; verify the real run ID is used for file handoff.
        task = asyncio.create_task(value.process())
        try:
            # Poll is deliberately absent so acceptance is explicit.
            store.accept(await value.client.batches.get(), "owner")
            await until(lambda: store.counts()["inbox"].get("done") == 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        files = store.db.execute("SELECT * FROM outbox WHERE file_path IS NOT NULL").fetchall()
        assert len(files) == 1
        assert Path(files[0]["file_path"]).read_text() == "<html>report</html>"
        assert Path(target).read_text() == "<html>report</html>"


async def test_file_tool_snapshot_is_delivered_and_not_reuploaded_on_send_retry(store, credentials):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        source = Path(directory) / "report.html"
        source.write_text("<html>original</html>")
        tool = prepare_file_tool(store)
        context = CallContext("run-1", "prepare", 1, time.monotonic() + 10, CancellationToken())
        receipt = await tool.handler({"path": str(source)}, context)
        await tool.handler({"path": str(source)}, context)
        assert receipt["prepared"]
        source.write_text("changed after preparation")
    files = store.files_for("run-1")
    assert len(files) == 1
    assert Path(files[0]["path"]).read_text() == "<html>original</html>"
    requests = []
    attempts = 0

    def handle(r):
        nonlocal attempts
        requests.append(r)
        if r.url.path.endswith("getuploadurl"):
            return httpx.Response(200, json={"ret": 0, "upload_param": "upload"})
        if r.url.path.endswith("upload"):
            return httpx.Response(200, headers={"x-encrypted-param": "download"})
        if r.url.path.endswith("sendmessage"):
            attempts += 1
            return httpx.Response(500 if attempts == 1 else 200, json={"ret": 0})
        return httpx.Response(200, json={"ret": 0})

    store.accept({"msgs": [incoming()]}, "owner")
    store.finish(store.next_input(), IntentSession(), "", files=files)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        value = channel(store, credentials, client=ILinkClient(http, token="fake-bearer"))
        task = asyncio.create_task(value.deliver())
        try:
            await until(lambda: attempts == 1)
            store.db.execute("UPDATE outbox SET next_attempt=0")
            store.db.commit()
            await until(lambda: attempts == 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert sum(r.url.path.endswith("getuploadurl") for r in requests) == 1
    assert len(value.agent.inputs) == 0
    sends = [json.loads(r.content)["msg"] for r in requests if r.url.path.endswith("sendmessage")]
    assert sends[0] == sends[1]
