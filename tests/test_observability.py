"""Observable behavior and failure boundaries; no real API or user directories."""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from uuid import uuid4

import pytest
from dynamic_graph import FakeModelClient, ModelRequest
from dynamic_graph.models.client import ModelCallError, ModelResponse

from karen.observability import ObservedModel, Observer
from karen.observability.checks import check_trace
from karen.observability.viewer import TraceStore, create_server


def request():
    return ModelRequest(
        role="intent",
        system_instruction="instruction",
        task_instruction="task",
        input_data={"text": "请写邮件"},
        output_schema={"type": "object"},
    )


def read_events(root):
    return [
        json.loads(line)
        for path in (root / "traces").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


async def test_weixin_channel_events_are_visible_without_an_agent_task_and_redacted(tmp_path):
    observer = Observer(tmp_path)
    await observer.start()
    with observer.span("weixin.delivery"):
        observer.emit("weixin.delivery_failed", status="error", data={
            "error_code": "WEIXIN_NETWORK_FAILED", "context_token": "secret",
        })
    await observer.close()
    listing = TraceStore(tmp_path).tasks()
    assert listing["tasks"] == []
    event = listing["channel_events"][0]
    assert event["event_type"] == "weixin.delivery_failed"
    assert event["data"]["context_token"] == "[REDACTED]"
    assert TraceStore(tmp_path).trace(event["trace_id"])["events"]


async def test_concurrent_turns_keep_causal_identity_and_actual_usage(tmp_path):
    observer = Observer(tmp_path)
    await observer.start()

    class Backend:
        metadata = {"model": "synthetic"}

        async def generate(self, value):
            await asyncio.sleep(0)
            return ModelResponse(
                {"answer": "ok"},
                usage={"input_tokens": 12, "output_tokens": 4},
                provider_request_id="provider-1",
            )

    model = ObservedModel(Backend(), observer)
    identities = {}

    async def turn(name):
        trace_id = uuid4().hex
        identities[name] = trace_id
        with observer.span("turn", trace_id=trace_id, request_id=name):
            response = await model.generate(request())
            assert response.payload == {"answer": "ok"}
        assert observer.context() == {}

    await asyncio.gather(turn("task-A"), turn("task-B"))
    await observer.close()
    events = read_events(tmp_path)
    for name, trace_id in identities.items():
        own = [e for e in events if e.get("request_id") == name]
        assert {e["trace_id"] for e in own} == {trace_id}
        root = next(e for e in own if e["stage"] == "turn" and e["event_type"] == "span.started")
        model_start = next(
            e for e in own if e["stage"] == "model.call" and e["event_type"] == "span.started"
        )
        assert model_start["parent_span_id"] == root["span_id"]
        end = next(
            e for e in own if e["stage"] == "model.call" and e["event_type"] == "span.finished"
        )
        assert end["data"]["usage"] == {"input_tokens": 12, "output_tokens": 4}
        assert end["duration_ms"] >= 0


async def test_redaction_precedes_clipping_and_does_not_change_model_data(tmp_path):
    secret = "a-known-credential"
    observer = Observer(tmp_path, sensitive_values=(secret,))
    backend = FakeModelClient([ModelResponse({"text": secret})])
    original = request()
    original.input_data["password"] = "short-secret"
    original.input_data["text"] = secret + " api_key=embedded-secret " + "长" * 9000
    await observer.start()
    with observer.span("turn"):
        response = await ObservedModel(backend, observer).generate(original)
    await observer.close()
    raw = "".join(p.read_text() for p in (tmp_path / "traces").glob("*.jsonl"))
    assert secret not in raw and "short-secret" not in raw and "embedded-secret" not in raw
    assert backend.requests[0] is original and response.payload["text"] == secret
    events = read_events(tmp_path)
    record = next(e for e in events if e["event_type"] == "model.request")
    assert record["data"]["input"]["text"]["truncated"]
    assert record["data"]["input"]["password"] == "[REDACTED]"
    assert all(p.stat().st_mode & 0o077 == 0 for p in (tmp_path / "traces").iterdir())


async def test_model_failure_and_cancellation_preserve_original_exception(tmp_path):
    observer = Observer(tmp_path)
    await observer.start()
    failure = ModelCallError("AUTH_FAILED", "do not log this secret", retryable=False)
    with observer.span("turn"):
        with pytest.raises(ModelCallError) as raised:
            await ObservedModel(FakeModelClient([failure]), observer).generate(request())
        assert raised.value is failure

        class Blocked:
            async def generate(self, value):
                await asyncio.Event().wait()

        call = asyncio.create_task(ObservedModel(Blocked(), observer).generate(request()))
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    await observer.close()
    events = read_events(tmp_path)
    assert any(e["status"] == "cancelled" for e in events)
    assert any(
        e["event_type"] == "model.error" and e["data"]["error_code"] == "AUTH_FAILED"
        for e in events
    )
    assert "do not log this secret" not in json.dumps(events)


async def test_model_error_records_safe_provider_cause_codes(tmp_path):
    observer = Observer(tmp_path)
    await observer.start()
    failure = ModelCallError(
        "MODEL_UNAVAILABLE", "credential secret", retryable=True, details={"http_status": 503}
    )
    cause = OSError(61, "provider body secret")
    failure.__cause__ = cause
    with observer.span("turn"):
        with pytest.raises(ModelCallError) as raised:
            await ObservedModel(FakeModelClient([failure]), observer).generate(request())
        assert raised.value is failure
    await observer.close()
    events = read_events(tmp_path)
    diagnostic = next(e["data"] for e in events if e["event_type"] == "model.error")
    assert diagnostic["http_status"] == 503 and diagnostic["errno"] == 61
    assert diagnostic["cause_types"] == ["ConnectionRefusedError"]
    assert "credential secret" not in json.dumps(
        events
    ) and "provider body secret" not in json.dumps(events)


async def test_queue_overflow_does_not_wait_and_is_reported(tmp_path, caplog):
    observer = Observer(tmp_path)
    await observer.start()
    with observer.span("turn"):
        for i in range(600):
            observer.emit("sample", data={"i": i})
    assert observer.dropped > 0
    await asyncio.wait_for(observer.close(), 5)
    health = json.loads(next((tmp_path / "traces").glob("*.health.json")).read_text())
    assert health["dropped"] > 0
    assert "observation gap" in caplog.text


async def test_disk_error_and_summary_error_do_not_fail_business_function(tmp_path, monkeypatch):
    observer = Observer(tmp_path)
    await observer.start()

    def fail(*args):
        raise OSError("disk offline")

    monkeypatch.setattr(observer, "_append", fail)

    async def business(state):
        return {"answer": "completed"}

    def invalid_summary(result):
        raise ValueError("invalid summary")

    assert await observer.node("business", business, invalid_summary)({}) == {"answer": "completed"}
    await observer.close()
    assert observer.write_failures > 0
    assert TraceStore(tmp_path).tasks()["coverage"]["gaps"]


async def test_storage_initialization_failure_leaves_observer_disabled(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_text("existing file")
    observer = Observer(root)
    await observer.start()
    assert await ObservedModel(FakeModelClient([{"answer": "ok"}]), observer).generate(
        request()
    ) == ModelResponse({"answer": "ok"})
    await observer.close()
    assert observer.write_failures == 1


def test_partial_tail_and_corrupt_line_do_not_invent_finished_state(tmp_path):
    path = tmp_path / "traces"
    path.mkdir()
    trace_id = uuid4().hex
    start = {
        "trace_id": trace_id,
        "span_id": "span",
        "stage": "turn",
        "event_type": "span.started",
        "request_id": "task",
        "timestamp_utc": "2026-10-03T01:00:00+00:00",
    }
    (path / f"{trace_id}.jsonl").write_text(json.dumps(start) + '\n{bad}\n{"event_type":')
    store = TraceStore(tmp_path)
    tasks = store.tasks()["tasks"]
    assert tasks[0]["status"] == "nonterminal"
    detail = store.trace(trace_id)
    assert detail["coverage"]["invalid_lines"] == 1
    assert detail["coverage"]["unfinished_tail"]
    assert all(check["status"] == "unknown" for check in detail["checks"])


def test_input_list_waits_for_input_record_but_preserves_failed_input(tmp_path):
    directory = tmp_path / "traces"
    directory.mkdir()
    trace_id = uuid4().hex
    path = directory / f"{trace_id}.jsonl"
    start = {"trace_id": trace_id, "stage": "input", "event_type": "span.started",
             "timestamp_utc": "2026-10-07T00:00:00Z", "data": {}}
    path.write_text(json.dumps(start) + "\n")
    store = TraceStore(tmp_path)
    assert store.tasks()["tasks"] == []
    received = {**start, "event_type": "input.received", "data": {"text": "完整用户输入"}}
    with path.open("a") as handle:
        handle.write(json.dumps(received) + "\n")
    assert store.tasks()["tasks"][0]["input"] == "完整用户输入"
    finished = {**start, "event_type": "span.finished", "status": "failed"}
    path.write_text(json.dumps(start) + "\n" + json.dumps(finished) + "\n")
    assert store.tasks()["tasks"][0]["status"] == "failed"


def test_viewer_refuses_arbitrary_paths_and_symlink_records(tmp_path):
    store = TraceStore(tmp_path)
    with pytest.raises(ValueError):
        store.trace("../../secret")
    traces = tmp_path / "traces"
    traces.mkdir()
    outside = tmp_path / "sensitive.json"
    outside.write_text('{"secret": "never show"}')
    trace_id = uuid4().hex
    (traces / f"{trace_id}.jsonl").symlink_to(outside)
    assert store.tasks()["tasks"] == []
    assert store.trace(trace_id)["coverage"]["unavailable"]


async def test_long_trace_list_reads_terminal_tail(tmp_path):
    observer = Observer(tmp_path)
    await observer.start()
    trace_id = uuid4().hex
    with observer.span("turn", trace_id=trace_id, request_id="long") as end:
        for i in range(40):
            observer.emit("sample", data={"text": "x" * 7000})
            await asyncio.sleep(0)
        end["outcome"] = "COMPLETED"
    await observer.close()
    task = TraceStore(tmp_path).tasks()["tasks"][0]
    assert task["outcome"] == "COMPLETED"
    assert task["coverage"]["partial"]


def test_recorded_behavior_checks_do_not_claim_business_acceptance():
    events = [
        {
            "stage": "turn",
            "trace_id": "a",
            "event_type": "span.finished",
            "data": {"outcome": "needs_clarification"},
        },
        {
            "event_type": "execution.started",
            "trace_id": "b",
            "data": {
                "goal": {"context": {"memory": {"history": {"status": "none", "messages": []}}}}
            },
        },
        {
            "event_type": "memory.recalled",
            "data": {
                "degradations": ["RERANK_FAILED_FUSION_ORDER"],
                "m1": [{"relevance": "unverified", "rerank_rank": None}],
                "m2": [],
            },
        },
    ]
    events.append({"event_type": "intent.routed", "trace_id": "c", "data": {"route": "respond"}})
    assert [c["status"] for c in check_trace(events, {})] == ["pass"] * 4
    events[1]["trace_id"] = "a"
    events[1]["data"]["goal"]["context"]["memory"]["history"]["messages"] = [
        {"content": "unrelated"}
    ]
    events[2]["data"]["m1"][0]["relevance"] = "relevant"
    assert [c["status"] for c in check_trace(events, {})] == ["fail", "fail", "fail", "pass"]
    assert [c["status"] for c in check_trace([], {"partial": True})] == ["unknown"] * 4

    events.append({"event_type": "execution.started", "trace_id": "c", "data": {}})
    assert check_trace(events, {})[-1]["status"] == "fail"


async def test_incomplete_output_is_not_shown_as_successful_completion(tmp_path):
    from dynamic_graph import EngineConfig, ExecutionPolicy, RunResult

    from karen import IntentRecognizer, IntentSession, Karen

    class IncompleteEngine:
        config = EngineConfig(runs_dir=tmp_path / "runs")

        async def run(self, **kwargs):
            return RunResult(
                run_id=uuid4().hex,
                request_id=kwargs["goal"].request_id,
                execution_status="COMPLETED",
                output_complete=False,
                outputs={"answer": "partial"},
            )

    observer = Observer(tmp_path / "observability")
    await observer.start()
    from intent_helpers import TaskIntentModel

    model = TaskIntentModel(
        [
            {
                "decision": {
                    "outcome": "ready",
                    "goal": {"objective": "example", "success_criteria": ["完整回答"]},
                }
            }
        ]
    )
    agent = Karen(
        intent=IntentRecognizer(model, observer=observer),
        engine=IncompleteEngine(),
        observer=observer,
    )
    try:
        turn = await agent.advance(
            IntentSession(timezone="UTC"), "example", policy=ExecutionPolicy()
        )
        assert turn.result.execution_status == "COMPLETED" and not turn.result.output_complete
    finally:
        await observer.close()
    assert TraceStore(observer.root_dir).tasks()["tasks"][0]["outcome"] == "INCOMPLETE"


def test_readonly_http_server_and_host_validation(tmp_path):
    server = create_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/") as response:
            page = response.read().decode()
            assert "运行观测" in page
            assert "textContent" in page and "innerHTML" not in page
            assert response.headers["Cache-Control"] == "no-store"
        with opener.open(base + "/api/tasks") as response:
            assert json.load(response)["tasks"] == []
        for path, headers, expected in [
            ("/api/trace?id=../../secret", {}, 400),
            ("/", {"Host": "malicious.example"}, 403),
            ("/unknown", {}, 404),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(urllib.request.Request(base + path, headers=headers))
            assert error.value.code == expected
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(urllib.request.Request(base + "/api/tasks", data=b"{}"))
        assert error.value.code == 501
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
