"""Observable routing behavior, with no real user directory or external models."""

import asyncio
from datetime import UTC, datetime

import pytest
from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings
from dynamic_graph.models.client import ModelCallError
from intent_helpers import ClarityAwareModel as FakeModelClient
from pydantic import ValidationError
from test_context import LocalEmbeddings, MemoryModel, event
from test_execution import graph_response, ready
from test_personal_facts import HobbyModel

from karen import IntentRecognizer, IntentSession, Karen
from karen.context import ContextMemory
from karen.intent import InputRouting
from karen.intent.recognizer import Message
from karen.observability import ObservedModel, Observer
from karen.observability.viewer import TraceStore


def routing(handling="assess", *, types=None, relation="new"):
    return {
        "input_types": types or ["task_request"],
        "handling": handling,
        "task_relation": relation,
        "reason": "本轮明确请求的处理方式",
    }


def reply(text):
    return {"decision": {"outcome": "reply", "answer": text}}


def clarify(text):
    return {"decision": {"outcome": "needs_clarification", "questions": [text]}}


def agent_for(tmp_path, responses, *, memory=None, observer=None, executor=None):
    model = FakeModelClient(responses)
    if observer:
        observed = ObservedModel(model, observer)
    else:
        observed = model
    executor = executor or FakeModelClient()
    agent = Karen(
        intent=IntentRecognizer(observed, observer=observer),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(executor, executor),
        ),
        memory=memory,
        observer=observer,
    )
    return agent, model, executor


@pytest.mark.parametrize(
    "text,kind",
    [("我目前住在北京。", "information"), ("你好", "conversation"), ("什么是高内聚？", "question")],
)
async def test_direct_routes_do_not_generate_goals_or_call_engine(tmp_path, text, kind):
    agent, model, executor = agent_for(
        tmp_path, [routing("respond", types=[kind]), reply("自然回应")]
    )
    original = IntentSession(timezone="Asia/Shanghai")
    turn = await agent.advance(original, text)
    assert turn.response == "自然回应" and turn.result is None
    assert turn.session.goal is None and turn.session.questions == () and turn.session.completed
    assert [r.role for r in model.requests] == (
        ["intent_router", "intent_clarity", "intent_response"]
        if kind == "question"
        else ["intent_router", "intent_response"]
    )
    assert executor.requests == [] and not (tmp_path / "runs").exists()
    assert original.messages == () and original.reply is None


async def test_information_is_written_async_and_not_recorded_as_a_task(tmp_path):
    backend = MemoryModel()
    backend.slow_extract = asyncio.Event()
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=backend, embeddings=LocalEmbeddings()
    )
    await memory.start()
    agent, _, executor = agent_for(
        tmp_path,
        [routing("respond", types=["information"]), reply("好的，了解了，你目前住在北京。")],
        memory=memory,
    )
    try:
        turn = await asyncio.wait_for(
            agent.advance(IntentSession(timezone="Asia/Shanghai"), "我目前住在北京。"), 2
        )
        agent.record_response(turn.session, turn.response)
        await memory._queue.join()
        rows = memory.storage.event_rows(request_id=turn.session.request_id)
        assert {r["event_type"] for r in rows} == {"user_message", "assistant_message"}
        assert executor.requests == []
        backend.slow_extract.set()
        await memory.flush()
        _, stored, _ = memory.storage.snapshot()
        facts = [m for m in stored.values() if m.layer == "m1"]
        assert len(facts) == 1 and facts[0].value == "北京"
        assert all(s.source_role == "user" for s in facts[0].sources)
    finally:
        backend.slow_extract.set()
        await memory.close()


async def test_personal_question_answers_from_recalled_m1_without_execution(tmp_path):
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=HobbyModel(), embeddings=LocalEmbeddings()
    )
    await memory.start()
    try:
        for text in ["我喜欢历史和考古", "我喜欢骑行"]:
            await memory.flush(memory.submit(event(text, request=text)))
        agent, model, executor = agent_for(
            tmp_path,
            [routing("respond", types=["question"]), reply("我记得你喜欢历史和考古，也喜欢骑行。")],
            memory=memory,
        )
        turn = await agent.advance(IntentSession(timezone="Asia/Shanghai"), "我有哪些爱好？")
        assert turn.response and turn.result is None and turn.session.goal is None
        recalled = model.requests[-1].input_data["memory"]
        assert len(recalled["m1"]) == 2 and recalled["collection"] is None
        assert recalled["history"]["messages"] == [] and executor.requests == []
    finally:
        await memory.close()


async def test_mixed_information_and_task_keep_both_parts_in_goal_and_memory(tmp_path):
    backend = MemoryModel()
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=backend, embeddings=LocalEmbeddings()
    )
    await memory.start()
    request = "我搬到上海了，帮我写一段中文介绍。"
    executor = FakeModelClient([graph_response(), {"draft": "上海介绍"}])
    agent, _, _ = agent_for(
        tmp_path,
        [routing(types=["information", "task_request"]), ready()],
        memory=memory,
        executor=executor,
    )
    try:
        turn = await agent.advance(IntentSession(timezone="Asia/Shanghai"), request)
        assert turn.result.execution_status == "COMPLETED"
        assert turn.session.goal.context["conversation"][0]["content"] == request
        assert turn.session.routing.input_types == ["information", "task_request"]
        await memory.flush()
        _, stored, _ = memory.storage.snapshot()
        assert any(m.layer == "m1" and m.value == "上海" for m in stored.values())
    finally:
        await memory.close()


async def test_clarification_followup_and_correction_keep_task_identity(tmp_path):
    model = FakeModelClient(
        [
            routing(),
            clarify("给谁写？"),
            routing(types=["task_control"], relation="continue"),
            clarify("写什么进度？"),
            routing(types=["task_control"], relation="continue"),
            ready(),
        ]
    )
    intent = IntentRecognizer(model)
    original = IntentSession(timezone="UTC", reference_time_utc=datetime(2026, 10, 4, tzinfo=UTC))
    first = await intent.advance(original, "写邮件")
    second = await intent.advance(first, "给甲客户，改用中文")
    final = await intent.advance(second, "设计完成了")
    assert (
        final.request_id == original.request_id
        and final.reference_time_utc == original.reference_time_utc
    )
    assert final.goal and len(final.goal.context["conversation"]) == 5
    classifiers = [r for r in model.requests if r.role == "intent_router"]
    assert classifiers[1].input_data["pending_task"]["questions"] == ["给谁写？"]


@pytest.mark.parametrize("rerank_fails", [False, True])
async def test_resource_clarification_chain_executes_with_pending_writes_and_old_history(
    tmp_path, rerank_fails
):
    from dynamic_graph.models.client import ModelResponse

    class ClarificationMemoryModel(MemoryModel):
        async def generate(self, request):
            response = await super().generate(request)
            if request.role == "memory_query" and request.input_data["current_task_messages"]:
                return ModelResponse({**response.payload, "dialogue_dependency": "current_task"})
            return response

    backend = ClarificationMemoryModel()
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=backend, embeddings=LocalEmbeddings()
    )
    await memory.start()
    try:
        await memory.flush(memory.submit(event("我住在北京", request="residence")))
        await memory.flush(
            memory.submit(
                event(
                    "北京路亚资源旧任务。" + "旧资料说明。" * 800,
                    request="old-resources",
                    kind="assistant_message",
                )
            )
        )
        backend.requests.clear()
        backend.fail_rank = rerank_fails
        goal_response = ready()
        goal_response["decision"]["goal"].update(
            objective="整理近一个月的北京路亚水域清单，使用文字说明。",
            inputs={
                "city": "北京",
                "period": "近一个月",
                "scope": "水域清单",
                "format": "文字说明",
            },
        )
        executor = FakeModelClient([graph_response(), {"draft": "水域资源文字说明"}])
        agent, intent_model, _ = agent_for(
            tmp_path,
            [
                routing(),
                clarify("资源类型、时间范围和形式？"),
                routing(types=["task_control"], relation="continue"),
                clarify("资源具体是哪一类？"),
                routing(types=["task_control"], relation="continue"),
                goal_response,
            ],
            memory=memory,
            executor=executor,
        )
        original = IntentSession(
            timezone="Asia/Shanghai", reference_time_utc=datetime(2026, 10, 4, tzinfo=UTC)
        )
        texts = [
            "帮我整理下最新的北京路亚资源。",
            "最新指1个月内，文字说明即可。",
            "主要是水域清单",
        ]
        async with memory.foreground():
            first = await agent.advance(original, texts[0])
            second = await agent.advance(first.session, texts[1])
            assert first.result is None and second.result is None and executor.requests == []
            final = await agent.advance(second.session, texts[2])
            assert final.result.execution_status == "COMPLETED"
            assert final.session.request_id == original.request_id
            assert final.session.reference_time_utc == original.reference_time_utc
            assert [
                m["content"]
                for m in final.session.goal.context["conversation"]
                if m["role"] == "user"
            ] == texts
            assert final.session.goal.inputs == goal_response["decision"]["goal"]["inputs"]
            assert final.memory_result.history.status == "none"
            assert final.memory_result.history.messages == []
            assert final.memory_result.coverage["dialogue_dependency"] == "current_task"
            assert not final.memory_result.coverage["requires_history"]
            assert "CANDIDATE_BUDGET_LIMIT" in final.memory_result.degradations
            if rerank_fails:
                assert "RERANK_FAILED_FUSION_ORDER" in final.memory_result.degradations
            rank_requests = [r for r in backend.requests if r.role == "memory_rerank"]
            assert rank_requests and all(
                r.input_data["history_candidates"] == [] for r in rank_requests
            )
            assessments = [r for r in intent_model.requests if r.role == "intent"]
            assert len(assessments[-1].input_data["messages"]) == 5 and len(executor.requests) == 2
            await memory._queue.join()
            rows = memory.storage.event_rows(request_id=original.request_id)
            assert sum(row["event_type"] == "user_message" for row in rows) == 3
    finally:
        await memory.close()


async def test_cancel_pending_task_does_not_call_assessor_or_engine(tmp_path):
    agent, model, executor = agent_for(
        tmp_path,
        [
            routing(),
            clarify("给谁写？"),
            routing("cancel", types=["task_control"], relation="continue"),
            routing("respond", types=["conversation"]),
            reply("你好"),
        ],
    )
    pending = await agent.advance(IntentSession(timezone="UTC"), "写邮件")
    cancelled = await agent.advance(pending.session, "不用做了")
    assert cancelled.response == "好的，已取消当前待澄清的任务。"
    assert (
        cancelled.session.request_id == pending.session.request_id and cancelled.session.completed
    )
    assert cancelled.session.questions == () and cancelled.result is None
    assert [r.role for r in model.requests] == [
        "intent_router",
        "intent_clarity",
        "intent",
        "intent_router",
    ]
    assert executor.requests == []
    next_turn = await agent.advance(cancelled.session, "你好")
    assert next_turn.session.request_id != pending.session.request_id
    assert model.requests[-2].input_data["pending_task"] is None


async def test_unrelated_input_during_clarification_starts_clean_request(tmp_path):
    agent, model, _ = agent_for(
        tmp_path,
        [
            routing(),
            clarify("给谁写？"),
            routing("respond", types=["information"]),
            reply("了解了，你住在北京。"),
        ],
    )
    pending = await agent.advance(IntentSession(timezone="UTC"), "写邮件")
    new = await agent.advance(pending.session, "我住在北京")
    assert new.session.request_id != pending.session.request_id
    assert [m["content"] for m in model.requests[-1].input_data["messages"]] == ["我住在北京"]
    assert pending.session.questions == ("给谁写？",)


async def test_direct_reply_can_clarify_and_reassess_its_followup(tmp_path):
    model = FakeModelClient(
        [
            routing("respond", types=["question"]),
            clarify("你指哪个偏好？"),
            routing("respond", types=["question", "task_control"], relation="continue"),
            reply("没有相应记录"),
        ]
    )
    intent = IntentRecognizer(model)
    pending = await intent.advance(IntentSession(timezone="UTC"), "我的偏好是什么？")
    final = await intent.advance(pending, "饮食偏好")
    assert final.reply == "没有相应记录" and final.goal is None
    assert final.request_id == pending.request_id


@pytest.mark.parametrize(
    "payload",
    [
        routing("cancel", types=["task_control"], relation="continue"),
        routing(types=["task_control"], relation="continue"),
    ],
)
async def test_cannot_cancel_or_continue_without_a_pending_task(payload):
    original = IntentSession(timezone="UTC")
    with pytest.raises(ValueError, match="No pending task"):
        await IntentRecognizer(FakeModelClient([payload])).advance(original, "取消")
    assert original.messages == ()


@pytest.mark.parametrize(
    "payload",
    [
        routing("respond", types=["task_request"]),
        routing(types=["information", "information"]),
        routing("cancel", types=["conversation"]),
        {"input_types": [], "handling": "respond", "task_relation": "new", "reason": "x"},
    ],
)
def test_invalid_routing_contract_is_rejected(payload):
    with pytest.raises(ValidationError):
        InputRouting.model_validate(payload)


async def test_classifier_failure_keeps_raw_input_but_leaves_session_unchanged(tmp_path):
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=MemoryModel(), embeddings=LocalEmbeddings()
    )
    await memory.start()
    error = ModelCallError("MODEL_UNAVAILABLE", "test")
    agent, _, executor = agent_for(tmp_path, [error], memory=memory)
    original = IntentSession(timezone="UTC")
    try:
        with pytest.raises(ModelCallError):
            await agent.advance(original, "我住在北京")
        await memory._queue.join()
        rows = memory.storage.event_rows(request_id=original.request_id)
        assert len(rows) == 1 and rows[0]["event_type"] == "user_message"
        assert original.messages == () and executor.requests == []
    finally:
        await memory.close()


@pytest.mark.parametrize("role", ["intent_router", "intent_response", "intent"])
async def test_transient_model_failure_recovers_before_reply_or_execution(tmp_path, role):
    failure = ModelCallError("MODEL_UNAVAILABLE", "temporary connection failure", retryable=True)
    if role == "intent_router":
        responses = [failure, routing("respond", types=["information"]), reply("收到搬家计划。")]
    elif role == "intent_response":
        responses = [routing("respond", types=["information"]), failure, reply("收到搬家计划。")]
    else:
        responses = [routing(), failure, clarify("报告需要覆盖哪些主题？")]
    observer = Observer(tmp_path / "observability")
    await observer.start()
    agent, model, executor = agent_for(tmp_path, responses, observer=observer)
    try:
        turn = await agent.advance(IntentSession(timezone="UTC"), "我计划下个月搬家。")
        if role == "intent":
            assert turn.session.questions == ("报告需要覆盖哪些主题？",)
        else:
            assert turn.response == "收到搬家计划。"
        assert executor.requests == []
        attempts = [r for r in model.requests if r.role == role]
        assert len(attempts) == 2 and attempts[1].timeout_seconds < attempts[0].timeout_seconds
    finally:
        await observer.close()
    events = TraceStore(observer.root_dir).trace(turn.trace_id)["events"]
    retries = [e for e in events if e["event_type"] == "model.retry"]
    assert len(retries) == 1 and retries[0]["data"]["model_role"] == role


async def test_exhausted_transport_retries_save_raw_input_once(tmp_path):
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=MemoryModel(), embeddings=LocalEmbeddings()
    )
    await memory.start()
    failures = [ModelCallError("MODEL_UNAVAILABLE", "temporary", retryable=True) for _ in range(3)]
    agent, model, executor = agent_for(tmp_path, failures, memory=memory)
    original = IntentSession(timezone="UTC")
    try:
        with pytest.raises(ModelCallError) as raised:
            await agent.advance(original, "我计划下个月搬家。")
        assert raised.value is failures[-1] and len(model.requests) == 3
        await memory._queue.join()
        assert len(memory.storage.event_rows(request_id=original.request_id)) == 1
        assert original.messages == () and executor.requests == []
    finally:
        await memory.close()


@pytest.mark.parametrize(
    "code,retryable",
    [("MODEL_AUTH_FAILED", False), ("MODEL_UNAVAILABLE", False), ("MODEL_RESPONSE_INVALID", True)],
)
async def test_non_transport_or_non_retryable_errors_fail_without_retry(tmp_path, code, retryable):
    failure = ModelCallError(code, "failure", retryable=retryable)
    agent, model, _ = agent_for(tmp_path, [failure])
    with pytest.raises(ModelCallError) as raised:
        await agent.advance(IntentSession(timezone="UTC"), "你好")
    assert raised.value is failure and len(model.requests) == 1


async def test_cancellation_interrupts_retry_wait_without_another_call():
    entered_wait = asyncio.Event()

    class RetryObserver(Observer):
        def emit(self, event_type, **kwargs):
            if event_type == "model.retry":
                entered_wait.set()

    model = FakeModelClient([ModelCallError("MODEL_UNAVAILABLE", "temporary", retryable=True)])
    intent = IntentRecognizer(model, observer=RetryObserver())
    call = asyncio.create_task(intent.classify(IntentSession(timezone="UTC"), "你好"))
    await asyncio.wait_for(entered_wait.wait(), 1)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert len(model.requests) == 1


async def test_retry_total_deadline_is_not_reset(monkeypatch):
    from dataclasses import replace

    from dynamic_graph.models.client import ModelRequest

    from karen.intent import recognizer

    monkeypatch.setattr(
        recognizer,
        "ModelRequest",
        lambda **kwargs: replace(ModelRequest(**kwargs), timeout_seconds=0.6),
    )

    class SlowRetry:
        requests = []

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ModelCallError("MODEL_UNAVAILABLE", "temporary", retryable=True)
            await asyncio.Event().wait()

    model = SlowRetry()
    with pytest.raises(ModelCallError) as raised:
        await asyncio.wait_for(
            IntentRecognizer(model).classify(IntentSession(timezone="UTC"), "你好"), 1
        )
    assert raised.value.code == "MODEL_TIMEOUT" and len(model.requests) == 2
    assert 0 < model.requests[1].timeout_seconds < 0.2


async def test_observation_records_route_and_no_false_execution(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    agent, _, _ = agent_for(
        tmp_path,
        [routing("respond", types=["information"]), reply("好的，了解了。")],
        observer=observer,
    )
    try:
        turn = await agent.advance(IntentSession(timezone="UTC"), "我住在北京")
        agent.record_response(turn.session, turn.response, trace_id=turn.trace_id)
    finally:
        await observer.close()
    store = TraceStore(observer.root_dir)
    detail = store.trace(turn.trace_id)
    event = next(e for e in detail["events"] if e["event_type"] == "intent.routed")
    assert event["data"]["input_types"] == ["information"] and event["data"]["route"] == "respond"
    assert not any(e["event_type"] == "execution.started" for e in detail["events"])
    assert store.tasks()["tasks"][0]["outcome"] == "replied"
    assert detail["checks"][-1]["status"] == "pass"
    assert any(e.get("model_role") == "intent_router" for e in detail["events"])


async def test_classification_live_trace_is_visible_before_request_routing(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClassifier(FakeModelClient):
        async def generate(self, request):
            if request.role == "intent_router":
                entered.set()
                await release.wait()
            return await super().generate(request)

    observer = Observer(tmp_path / "observability")
    await observer.start()
    model = ObservedModel(
        SlowClassifier([routing("respond", types=["conversation"]), reply("你好")]), observer
    )
    executor = FakeModelClient()
    agent = Karen(
        intent=IntentRecognizer(model, observer=observer),
        engine=DynamicGraphEngine(models=ModelBindings(executor, executor)),
        observer=observer,
    )
    running = asyncio.create_task(agent.advance(IntentSession(timezone="UTC"), "你好"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await observer._queue.join()
        store = TraceStore(observer.root_dir)
        live = store.tasks()["tasks"][0]
        assert live["input"] == "你好" and live["status"] == "nonterminal"
        assert live["request_id"] is None
        detail = store.trace(live["trace_id"])
        assert detail["runs"] == [] and any(
            e["stage"] == "intent.classify" for e in detail["events"]
        )
        release.set()
        turn = await running
        await observer._queue.join()
        finished = store.tasks()["tasks"][0]
        assert (
            finished["request_id"] == turn.session.request_id and finished["outcome"] == "replied"
        )
    finally:
        release.set()
        await running
        await observer.close()


async def test_new_topic_raw_event_and_observation_do_not_belong_to_old_pending_task(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    memory = ContextMemory(
        root_dir=tmp_path / "context",
        model=MemoryModel(),
        embeddings=LocalEmbeddings(),
        observer=observer,
    )
    await memory.start()
    agent, _, _ = agent_for(
        tmp_path,
        [
            routing(),
            clarify("给谁写？"),
            routing("respond", types=["information"]),
            reply("了解了，你住在北京。"),
        ],
        memory=memory,
        observer=observer,
    )
    try:
        pending = await agent.advance(IntentSession(timezone="UTC"), "写邮件")
        new = await agent.advance(pending.session, "我住在北京")
        agent.record_response(new.session, new.response, trace_id=new.trace_id)
        await memory.flush()
        new_events = memory.storage.event_rows(request_id=new.session.request_id)
        assert {r["event_type"] for r in new_events} == {"user_message", "assistant_message"}
        old_events = memory.storage.event_rows(request_id=pending.session.request_id)
        assert all(
            memory.storage.load_event(r["event_id"]).payload.get("content") != "我住在北京"
            for r in old_events
        )
    finally:
        await memory.close()
        await observer.close()
    detail = TraceStore(observer.root_dir).trace(new.trace_id)
    assert detail["request_id"] == new.session.request_id
    assert not any(e.get("trace_id") == pending.trace_id for e in detail["events"])
    assert any(e["event_type"] == "memory.indexed" for e in detail["events"])


async def test_new_request_time_is_anchored_before_slow_classifier(monkeypatch, tmp_path):
    from karen import agent as agent_module
    from karen.intent import recognizer

    class Clock(datetime):
        instant = datetime(2026, 10, 3, 15, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(agent_module, "datetime", Clock)
    monkeypatch.setattr(recognizer, "datetime", Clock)

    class CrossingClassifier(FakeModelClient):
        async def generate(self, request):
            if request.role == "intent_router":
                Clock.instant = datetime(2026, 10, 3, 16, 1, tzinfo=UTC)
            return await super().generate(request)

    model = CrossingClassifier([routing(), ready()])
    executor = FakeModelClient([graph_response(), {"draft": "新任务"}])
    agent = Karen(
        intent=IntentRecognizer(model),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(executor, executor),
        ),
    )
    pending = IntentSession(
        timezone="Asia/Shanghai",
        reference_time_utc=datetime(2026, 10, 2, tzinfo=UTC),
        messages=(Message(role="user", content="写邮件"),),
        questions=("给谁写？",),
    )
    turn = await agent.advance(pending, "新任务：写明天的安排")
    assert turn.session.request_id != pending.request_id
    assert turn.session.goal.context["time_context"]["relative_dates"]["tomorrow"] == "2026-10-04"
    assert turn.session.reference_time_utc == datetime(2026, 10, 3, 15, 59, tzinfo=UTC)


async def test_clarity_gate_retains_pending_context_and_stops_goal_creation():
    from dynamic_graph import FakeModelClient as RawModel

    def clarity(questions, *, known=None, criteria=None):
        return {
            "known_referents": known or {},
            "references": [],
            "selection_criteria": criteria or [],
            "questions": questions,
            "reason": "Only unresolved necessary information is requested",
        }

    model = RawModel(
        [
            routing(),
            clarity(["你说的产品编号对应哪个产品？"]),
            routing(types=["task_control"], relation="continue"),
            clarity(["采用哪一个版本？"], known={"产品": "离线音频转码器"}),
            routing(types=["task_control"], relation="continue"),
            clarity([], known={"产品": "离线音频转码器版本2"}),
            ready(),
        ]
    )
    recognizer = IntentRecognizer(model)
    first = await recognizer.advance(IntentSession(timezone="UTC"), "比较这个产品的耗时")
    assert first.questions and first.goal is None
    assert [r.role for r in model.requests] == ["intent_router", "intent_clarity"]
    second = await recognizer.advance(first, "我说的是离线音频转码器")
    assert second.questions and second.goal is None
    final = await recognizer.advance(second, "版本2")
    assert final.goal is not None and not final.questions
    checks = [r for r in model.requests if r.role == "intent_clarity"]
    assert len(checks) == 3
    assert checks[-1].input_data["messages"][0]["content"] == "比较这个产品的耗时"
    assert checks[-1].input_data["messages"][-1]["content"] == "版本2"
    assert model.requests[-1].input_data["clarity"]["known_referents"] == {
        "产品": "离线音频转码器版本2"
    }


async def test_direct_question_uses_clarity_gate_but_information_does_not():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "known_referents": {},
                "references": [],
                "selection_criteria": [],
                "questions": ["你指哪一位？"],
                "reason": "指代不明确",
            },
        ]
    )
    pending = await IntentRecognizer(model).advance(IntentSession(timezone="UTC"), "他叫什么名字？")
    assert pending.questions == ("你指哪一位？",) and pending.reply is None
    assert [r.role for r in model.requests] == ["intent_router", "intent_clarity"]


async def test_multiple_inferred_referents_trigger_clarification_before_answer():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel([
        routing('respond', types=['question']),
        {'known_referents': {}, 'selection_criteria': [], 'questions': [],
         'references': [{'expression': '她', 'candidates': ['周宁', '沈清'],
                         'resolution': 'inferred', 'evidence': '按句法倾向猜周宁'}],
         'reason': '只按句法猜测仍非唯一指代'},
    ])
    result = await IntentRecognizer(model).advance(IntentSession(timezone='UTC'), '周宁告诉沈清，她收到了信。是谁收到了信？')
    assert result.questions and '周宁' in result.questions[0] and '沈清' in result.questions[0]
    assert result.reply is None and result.goal is None


