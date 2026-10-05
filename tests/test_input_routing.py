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
from karen.context.storage import encode
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


@pytest.mark.parametrize(
    "question,memory,draft,reviewed",
    [
        (
            "两种方案相差多少钱？",
            {"details": [
                {"text": "方案甲报价13元。", "source": {"source_role": "user"}},
                {"text": "市场上方案甲可能19元。", "source": {"source_role": "assistant"}},
                {"text": "方案乙报价29元。", "source": {"source_role": "user"}},
            ]},
            reply("按市场估价，差10元。"),
            reply("按你记录的报价，29−13=16元。"),
        ),
        (
            "怎样改善这个设备的使用效果？",
            {"m2": [
                {"memory": {"text": "用户曾用设备录制语音。"}},
                {"memory": {"text": "用户此前询问该设备的视频剪辑功能。"}},
            ]},
            reply("录音时注意降低环境噪声。"),
            reply("录音时降低环境噪声；你也问过视频剪辑，可分别调整画面与音轨。"),
        ),
    ],
)
async def test_direct_memory_reply_is_reviewed_against_original_evidence(
    question, memory, draft, reviewed
):
    from dynamic_graph import FakeModelClient as RawModel

    clear = {"known_referents": {}, "references": [], "selection_criteria": [], "questions": [], "reason": "输入清晰"}
    model = RawModel([routing("respond", types=["question"]), clear, draft, reviewed])
    session = IntentSession()
    result = await IntentRecognizer(model).advance(session, question, memory_context=memory)
    assert result.reply == reviewed["decision"]["answer"]
    assert result.goal is None and result.questions == () and session.messages == ()
    review = model.requests[-1]
    assert review.role == "intent_response_review"
    assert review.input_data["original_input"]["memory"] == memory
    assert review.input_data["draft"]["decision"]["answer"] == draft["decision"]["answer"]
    assert model.requests[-2].timeout_seconds >= review.timeout_seconds > 0


async def test_direct_reply_review_keeps_the_original_total_deadline(monkeypatch):
    import time
    from dataclasses import replace

    from karen.intent import recognizer

    request_factory = recognizer.ModelRequest
    monkeypatch.setattr(
        recognizer, "ModelRequest",
        lambda **kwargs: replace(request_factory(**kwargs), timeout_seconds=0.15),
    )

    class SlowReviewModel(FakeModelClient):
        async def generate(self, request):
            if request.role == "intent_response":
                await asyncio.sleep(0.09)
            if request.role == "intent_response_review":
                self.requests.append(request)
                await asyncio.sleep(0.1)
            return await super().generate(request)

    model = SlowReviewModel([routing("respond", types=["information"]), reply("草稿")])
    session = IntentSession()
    started = time.monotonic()
    with pytest.raises(ModelCallError, match="timed out"):
        await IntentRecognizer(model).advance(
            session, "收到", memory_context={"m2": [{"memory": {"text": "已有背景"}}]}
        )
    assert time.monotonic() - started < 0.21
    review = next(r for r in model.requests if r.role == "intent_response_review")
    assert 0 < review.timeout_seconds < 0.09
    assert session.reply is None and session.messages == ()


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
        recalled = next(r for r in model.requests if r.role == "intent_response").input_data["memory"]
        assert len(recalled["m1"]) == 2 and recalled["collection"] is None
        assert recalled["history"]["messages"] == [] and executor.requests == []
    finally:
        await memory.close()


@pytest.mark.parametrize("needs_external_data", [False, True])
@pytest.mark.parametrize("query_kind", ["detail", "relevance"])
@pytest.mark.parametrize("initial_types", [["question"], ["question", "task_request"], ["task_request", "question"]])
async def test_question_handling_is_reconsidered_with_recalled_detail_evidence(
    tmp_path, needs_external_data, query_kind, initial_types
):
    backend = MemoryModel()
    backend.kind = query_kind
    memory = ContextMemory(
        root_dir=tmp_path / "context", model=backend, embeddings=LocalEmbeddings()
    )
    await memory.start()
    try:
        await memory.flush(
            memory.submit(event("地铁单次费用8元；出租车报价74元。", request="costs"))
        )
        final_handling = "assess" if needs_external_data else "respond"
        goal = ready()
        goal["decision"]["goal"]["objective"] = "查证当前实际交通费用"
        goal["decision"]["goal"]["success_criteria"] = ["输出当前费用"]
        executor = FakeModelClient([graph_response(), {"draft": "当前报价已经查证。"}])
        agent, model, _ = agent_for(
            tmp_path,
            [
                routing(types=initial_types),
                routing(final_handling, types=["question"]),
                goal if needs_external_data else reply("按你提供的报价，可节省66元。"),
            ],
            memory=memory,
            executor=executor,
        )
        query = (
            "查证今天实际交通费用，然后比较差价。"
            if needs_external_data
            else "乘地铁比出租车节省多少？"
        )
        original = IntentSession(timezone="UTC")
        turn = await agent.advance(original, query)
        requests = [r for r in model.requests if r.role == "intent_router"]
        assert len(requests) == 2 and "memory" not in requests[0].input_data
        evidence = requests[1].input_data["memory"]
        assert any("74元" in h["memory"]["text"] for h in evidence["m2"])
        assert turn.session.routing.handling == final_handling
        assert turn.session.routing.input_types == ["question"]
        assert turn.session.request_id == original.request_id
        if needs_external_data:
            assert turn.result.execution_status == "COMPLETED" and executor.requests
        else:
            assert turn.response == "按你提供的报价，可节省66元。"
            assert turn.result is None and executor.requests == []
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
            assert final.memory_result.status == "degraded"
            assert not final.memory_result.coverage["complete"]
            assert len(encode(final.memory_result.context()).encode()) <= 12 * 1024
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
    [("MODEL_AUTH_FAILED", False), ("MODEL_UNAVAILABLE", False)],
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
            "questions": [{"text": text, "kind": "missing_requirement"} for text in questions],
            "reason": "Only unresolved necessary information is requested",
        }

    model = RawModel(
        [
            routing(),
            clarity(["你说的产品编号对应哪个产品？"]),
            routing(types=["task_control"], relation="continue"),
            clarity(["采用哪一个版本？"], known={"产品": "离线音频转码器"}),
            clarity(["采用哪一个版本？"], known={"产品": "离线音频转码器"}),
            routing(types=["task_control"], relation="continue"),
            clarity([], known={"产品": "离线音频转码器版本2"}),
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
                "questions": [{"text": "你指哪一位？", "kind": "ambiguous_reference"}],
                "reason": "指代不明确",
            },
        ]
    )
    pending = await IntentRecognizer(model).advance(IntentSession(timezone="UTC"), "他叫什么名字？")
    assert pending.questions == ("你指哪一位？",) and pending.reply is None
    assert [r.role for r in model.requests] == ["intent_router", "intent_clarity"]


@pytest.mark.parametrize("resolution", ["inferred", "explicit_identification"])
async def test_multiple_inferred_referents_trigger_clarification_before_answer(resolution):
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "known_referents": {},
                "selection_criteria": [],
                "questions": [],
                "references": [
                    {
                        "expression": "她",
                        "candidates": ["周宁", "沈清"],
                        "resolution": resolution,
                        "evidence": "按句法倾向猜周宁",
                    }
                ],
                "reason": "只按句法猜测仍非唯一指代",
            },
        ]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(timezone="UTC"), "周宁告诉沈清，她收到了信。是谁收到了信？"
    )
    assert result.questions and "周宁" in result.questions[0] and "沈清" in result.questions[0]
    assert result.reply is None and result.goal is None


async def test_personal_fact_absence_is_answered_without_asking_for_the_missing_answer():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel([routing("respond", types=["question"]), reply("我没有记录你的宠物名字。")])
    result = await IntentRecognizer(model).advance(
        IntentSession(timezone="UTC"),
        "我的宠物叫什么名字？",
        memory_context={"coverage": {"query_kind": "facts"}, "m1": [], "m2": []},
    )
    assert not result.questions and result.reply == "我没有记录你的宠物名字。"
    assert [r.role for r in model.requests] == ["intent_router", "intent_response"]


async def test_explicit_referent_identification_uses_user_evidence():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "references": [
                    {
                        "expression": "她",
                        "candidates": ["周宁", "沈清"],
                        "resolution": "explicit_identification",
                        "evidence": "这里她指沈清",
                    }
                ],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [],
                "reason": "用户已直接说明",
            },
            reply("沈清收到了信。"),
        ]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(timezone="UTC"), "周宁告诉沈清，她收到了信。这里她指沈清。是谁收到了信？"
    )
    assert not result.questions and result.reply == "沈清收到了信。"


async def test_supporting_memory_facts_remain_internal_to_direct_response():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "supporting_facts": ["内部证据：先借了两本，后来又借了三本。"],
                "decision": {"outcome": "reply", "answer": "你共借了五本。"},
            },
        ]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(timezone="UTC"),
        "我总共借了多少本书？",
        memory_context={"coverage": {"query_kind": "detail"}, "m1": [], "m2": []},
    )
    assert result.reply == "你共借了五本。"
    assert result.messages[-1].content == result.reply
    assert "内部证据" not in result.reply and not result.questions


@pytest.mark.parametrize("status", ["ambiguous", "unavailable"])
@pytest.mark.parametrize("required", [False, True])
async def test_optional_task_identity_does_not_block_supported_fact_reading(status, required):
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [routing("respond", types=["question"]), reply("按记录的报价，差额是78美元。"),
         reply("按记录的报价，差额是78美元。")]
    )
    memory = {
        "coverage": {"query_kind": "detail", "requires_history": required},
        "history": {"status": status, "messages": []},
        "details": [{"text": "火车17美元，出租车95美元。"}],
    }
    result = await IntentRecognizer(model).advance(
        IntentSession(),
        "按我记录的报价能省多少？",
        memory_context=memory,
    )
    assert bool(result.questions) is required
    if required:
        assert result.reply is None and all(r.role != "intent_response" for r in model.requests)
    else:
        assert result.reply == "按记录的报价，差额是78美元。"
        assert model.requests[-1].input_data["original_input"]["memory"]["details"] == memory["details"]


async def test_compatible_reference_alternatives_do_not_force_unique_selection():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "references": [
                    {
                        "expression": "两个版本里的主演",
                        "candidates": ["早期版", "新版"],
                        "resolution": "unresolved",
                        "evidence": "",
                        "requires_unique_resolution": False,
                    }
                ],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [],
                "reason": "可以分别回答两个版本",
            },
            reply("早期版是甲，新版是乙。"),
        ]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(), "这个作品两个版本里的主演分别是谁？"
    )
    assert result.reply == "早期版是甲，新版是乙。" and not result.questions


async def test_required_selection_criteria_cannot_be_omitted_by_clarity_decision():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "references": [],
                "known_referents": {},
                "selection_criteria": [],
                "selection_criteria_required": True,
                "questions": [],
                "reason": "缺少实质选择标准",
            },
        ]
    )
    result = await IntentRecognizer(model).advance(IntentSession(), "给我推荐些值得看的纪录片")
    assert result.questions and result.reply is None and result.goal is None


async def test_grounded_requirement_conflict_blocks_goal_until_user_corrects_it():
    from dynamic_graph import FakeModelClient as RawModel

    assessment = {
        "requirement_conflicts": [
            {
                "first_requirement": "全文最多500字",
                "second_requirement": "正文至少1000字",
                "question": "长度要求互相冲突，请确认以哪项为准？",
            }
        ],
        "references": [],
        "known_referents": {},
        "selection_criteria": [],
        "questions": [],
        "reason": "两个同时生效的长度要求不能同时满足",
    }
    resolved = {**assessment, "requirement_conflicts": [], "reason": "用户已明确纠正旧要求"}
    model = RawModel(
        [
            routing(),
            assessment,
            {
                "requirement_conflicts": assessment["requirement_conflicts"],
                "reason": "要求完成正文",
            },
            routing(types=["task_control"], relation="continue"),
            resolved,
            ready(),
        ]
    )
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写一份说明，全文最多500字，但正文至少1000字")
    assert first.goal is None and first.questions == ("长度要求互相冲突，请确认以哪项为准？",)
    final = await intent.advance(first, "以最多500字为准，不需要至少1000字")
    assert final.goal and not final.questions and final.request_id == first.request_id
    assert model.requests[-1].input_data["messages"][-1]["content"].startswith("以最多500字为准")


@pytest.mark.parametrize("second", ["至少2000字", "全文最多500字"])
async def test_unquoted_or_duplicate_conflict_does_not_create_a_user_question(second):
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing(),
            {
                "requirement_conflicts": [
                    {
                        "first_requirement": "全文最多500字",
                        "second_requirement": second,
                        "question": "应使用哪个长度？",
                    }
                ],
                "references": [],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [],
                "reason": "模拟无效冲突证据",
            },
            ready(),
        ]
    )
    result = await IntentRecognizer(model).advance(IntentSession(), "全文最多500字")
    assert result.goal and not result.questions


@pytest.mark.parametrize("initial_handling", ["assess", "respond"])
async def test_public_fact_lookup_preserves_unknowns_without_promoting_them_to_facts(initial_handling):
    from dynamic_graph import FakeModelClient as RawModel

    missing = ["核查用户所写精确名称的身份，再查询已验证对象的公开参数"]
    assessment = {
        "external_information_needed": missing,
        "references": [],
        "known_referents": {},
        "selection_criteria": [],
        "questions": [],
        "reason": "查证对象与问题已明确，缺少的是外部资料",
    }
    model = RawModel([routing(initial_handling, types=["question"]), assessment, ready()])
    result = await IntentRecognizer(model).advance(IntentSession(), "查询这个精确名称的公开参数")
    assert result.goal and not result.questions
    assert result.routing.handling == "assess"
    assert model.requests[-1].input_data["clarity"]["external_information_needed"] == missing
    assert result.goal.context["information_to_verify"] == missing
    assert result.goal.context["supporting_facts"] == []
    assert "information_to_verify" not in result.goal.inputs


async def test_external_information_does_not_override_a_necessary_identity_question():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing(),
            {
                "external_information_needed": ["查询相关产品的公开参数"],
                "references": [],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [{"text": "要更新哪一个私人项目？", "kind": "missing_requirement"}],
                "reason": "公共参数可查询，但真实更新对象未明确",
            },
        ]
    )
    result = await IntentRecognizer(model).advance(IntentSession(), "按最新参数更新我的项目")
    assert result.questions == ("要更新哪一个私人项目？",) and not result.goal


@pytest.mark.parametrize("distinct_identification", [False, True])
async def test_review_cannot_resolve_ambiguity_by_relabeling_the_same_passage(
    distinct_identification,
):
    from dynamic_graph import FakeModelClient as RawModel

    ambiguous = "程伊告诉宋宁，她已经寄出了文件。"
    identifying = "这里的她明确指程伊。"
    original_ref = {
        "expression": "她",
        "candidates": ["程伊", "宋宁"],
        "resolution": "unresolved",
        "evidence": ambiguous,
        "requires_unique_resolution": True,
    }
    initial = {
        "references": [original_ref],
        "known_referents": {"文件": "待寄出的物件"},
        "selection_criteria": [],
        "questions": [],
        "reason": "原句存在两个候选",
    }
    reviewed = {
        **initial,
        "references": [
            {
                **original_ref,
                "resolution": "explicit_identification",
                "evidence": identifying if distinct_identification else ambiguous,
            }
        ],
        "reason": "复核声称已消除歧义",
    }
    model = RawModel(
        [
            routing("respond", types=["question"]),
            initial,
            reviewed,
            reply("程伊寄出了文件。"),
        ]
    )
    session = IntentSession(
        user_context={"provided_context": identifying} if distinct_identification else {}
    )
    result = await IntentRecognizer(model).advance(session, ambiguous + "谁寄出了文件？")
    if distinct_identification:
        assert result.reply == "程伊寄出了文件。" and not result.questions
    else:
        assert result.questions and not result.reply and not result.goal
        assert model.requests[-1].role == "intent_clarity_review"


@pytest.mark.parametrize("incompatible", [False, True])
async def test_dropped_requirement_conflict_needs_independent_source_check(incompatible):
    from dynamic_graph import FakeModelClient as RawModel

    conflict = {
        "first_requirement": "全文最多400字",
        "second_requirement": "正文至少700字",
        "question": "两个长度要求不能同时满足，采用哪项？",
    }
    initial = {
        "requirement_conflicts": [conflict],
        "references": [],
        "known_referents": {"正文": "主要介绍文本"},
        "selection_criteria": [],
        "questions": [],
        "reason": "已发现当前要求冲突",
    }
    revised = {**initial, "requirement_conflicts": [], "reason": "复核声称要求兼容"}
    check = {
        "requirement_conflicts": [conflict] if incompatible else [],
        "reason": "仅核查原始要求及完整当前输入",
    }
    model = RawModel([routing(), initial, revised, check, ready()])
    result = await IntentRecognizer(model).advance(
        IntentSession(), "写介绍，全文最多400字，正文至少700字"
    )
    assert bool(result.questions) is incompatible
    audit = next(r for r in model.requests if r.role == "intent_requirement_check")
    assert audit.input_data["original_input"]["messages"][0]["content"] == (
        "写介绍，全文最多400字，正文至少700字"
    )
    assert "quoted_requirements" not in audit.input_data
    assert "复核声称" not in str(audit.input_data)
    assert model.requests[1].timeout_seconds >= audit.timeout_seconds > 0


@pytest.mark.parametrize("analyze_only", [False, True])
async def test_conflict_in_material_is_not_always_a_blocking_delivery_requirement(analyze_only):
    from dynamic_graph import FakeModelClient as RawModel

    assessment = {
        "requirement_conflicts": [
            {
                "first_requirement": "全文最多300字",
                "second_requirement": "正文至少600字",
                "question": "请确认要保留哪项要求？",
            }
        ],
        "references": [],
        "known_referents": {"正文": "全文的主要内容"},
        "selection_criteria": [],
        "questions": [],
        "reason": "材料中的字数范围互不兼容",
    }
    check = {
        "requirement_conflicts": assessment["requirement_conflicts"] if not analyze_only else [],
        "reason": "仅分析材料的矛盾可直接交付；按材料写正文才需要修改要求",
    }
    model = RawModel(
        [
            routing("respond" if analyze_only else "assess", types=["question"]),
            assessment,
            assessment,
            check,
            reply("是的，两项字数要求矛盾。"),
        ]
    )
    text = "全文最多300字，正文至少600字。" + (
        "请判断这两项要求是否矛盾，只解释即可。" if analyze_only else "按这两项要求写正文。"
    )
    result = await IntentRecognizer(model).advance(IntentSession(), text)
    assert bool(result.questions) is not analyze_only
    assert result.reply == ("是的，两项字数要求矛盾。" if analyze_only else None)
    audits = [r for r in model.requests if r.role == "intent_requirement_check"]
    assert len(audits) == 1
    assert audits[0].input_data["original_input"]["messages"][-1]["content"] == text


async def test_overlapping_conflict_guesses_use_one_independent_whole_input_verdict():
    from dynamic_graph import FakeModelClient as RawModel

    text = "规则是样本需有同一适用条件。材料甲和材料乙。推断唯一条件后输出结果。"
    first = {
        "first_requirement": "样本需有同一适用条件",
        "second_requirement": "材料甲和材料乙",
        "question": "材料之间是否冲突？",
    }
    second = {
        "first_requirement": "规则是样本需有同一适用条件。材料甲和材料乙。",
        "second_requirement": "推断唯一条件后输出结果",
        "question": "是否缺少唯一条件？",
    }
    assessment = {
        "requirement_conflicts": [first],
        "known_referents": {"材料": "用户提供的两个材料"},
        "references": [], "selection_criteria": [], "questions": [], "reason": "临时猜测",
    }
    model = RawModel([
        routing(), assessment, {**assessment, "requirement_conflicts": [second]},
        {"requirement_conflicts": [], "reason": "完整材料可同时满足要求"}, ready(),
    ])
    result = await IntentRecognizer(model).advance(IntentSession(), text)
    assert result.goal and not result.questions
    checks = [r for r in model.requests if r.role == "intent_requirement_check"]
    assert len(checks) == 1
    assert checks[0].input_data == {"original_input": model.requests[1].input_data}
    assert "临时猜测" not in str(checks[0].input_data)


async def test_requirement_review_cannot_create_a_conflict_from_unprovided_quotes():
    from dynamic_graph import FakeModelClient as RawModel

    conflict = {"first_requirement": "最多三项", "second_requirement": "至少五项", "question": "用哪项？"}
    assessment = {
        "requirement_conflicts": [conflict], "references": [], "known_referents": {},
        "selection_criteria": [], "questions": [], "reason": "数量要求矛盾",
    }
    review = {
        "requirement_conflicts": [{**conflict, "second_requirement": "至少九项"}],
        "reason": "核验引用了未提供的数值",
    }
    model = RawModel([routing(), assessment, review, ready()])
    original = IntentSession()
    with pytest.raises(ModelCallError, match="unsupported evidence"):
        await IntentRecognizer(model).advance(original, "列出最多三项，至少五项")
    assert original.goal is None and original.messages == ()


@pytest.mark.parametrize("requires_unique", [False, True])
async def test_reference_questions_obey_the_declared_need_for_a_unique_choice(requires_unique):
    from dynamic_graph import FakeModelClient as RawModel

    assessment = {
        "references": [
            {
                "expression": "改善效果",
                "candidates": ["录音", "剪辑"],
                "resolution": "unresolved",
                "evidence": "录音和剪辑都要改善效果",
                "requires_unique_resolution": requires_unique,
            }
        ],
        "known_referents": {},
        "selection_criteria": [],
        "questions": [{"text": "你指录音还是剪辑？", "kind": "ambiguous_reference"}],
        "reason": "两个场景可分别提供建议，单次操作则需要选定一个",
    }
    model = RawModel([routing(), assessment, ready()])
    result = await IntentRecognizer(model).advance(
        IntentSession(),
        "录音和剪辑都要改善效果。"
        + ("今天先修改其中一个。" if requires_unique else "分别给建议。"),
    )
    assert bool(result.questions) is requires_unique
    assert bool(result.goal) is not requires_unique


async def test_clarity_review_can_reject_an_unfounded_entity_definition():
    from dynamic_graph import FakeModelClient as RawModel

    original = {
        "references": [],
        "known_referents": {"Neravion": "用户所指的一个类群名称，身份需查证"},
        "selection_criteria": [],
        "questions": [],
        "reason": "候选把猜测当成识别",
    }
    revised = {
        **original,
        "questions": [
            {
                "text": "Neravion 是什么类群，或是否有其他拼写？",
                "kind": "unknown_identity",
                "subject": "Neravion",
            }
        ],
        "reason": "没有提供识别实体的具体事实",
    }
    model = RawModel(
        [
            routing(),
            original,
            revised,
            {
                "recognized": False,
                "canonical_name": "Neravion",
                "definition": "",
                "reason": "无法独立识别",
            },
        ]
    )
    result = await IntentRecognizer(model).advance(IntentSession(), "Neravion 什么时候出现？")
    assert result.questions and result.goal is None
    audit = next(r for r in model.requests if r.role == "intent_clarity_review")
    assert audit.input_data["original_input"]["messages"][0]["content"] == "Neravion 什么时候出现？"
    # Entity guesses from another model call must not become evidence for review.
    assert "candidate_assessment" not in audit.input_data
    assert "用户所指的一个类群名称，身份需查证" not in str(audit.input_data)
    assert "routing" not in audit.input_data["original_input"]
    assert "routing" not in model.requests[1].input_data
    assert model.requests[1].timeout_seconds >= audit.timeout_seconds > 0


async def test_clarity_and_goal_use_the_same_scoped_memory_policy():
    from dynamic_graph import FakeModelClient as RawModel

    from karen.intent.prompts import MEMORY_CONTEXT_INSTRUCTION

    memory = {
        "m1": [{"memory": {"text": "偏好研究图像处理", "state": "active", "scope": "global"}}],
        "m2": [],
        "details": [],
    }
    clarity = {
        "known_referents": {"领域": "已支持的相关研究偏好"},
        "references": [],
        "selection_criteria": ["图像处理"],
        "questions": [],
        "reason": "使用已知偏好作为默认标准",
    }
    model = RawModel([routing(), clarity, clarity, ready(), ready()])
    result = await IntentRecognizer(model).advance(
        IntentSession(), "推荐近期论文", memory_context=memory
    )
    assert result.goal is not None and not result.questions
    for request in model.requests[1:]:
        assert MEMORY_CONTEXT_INSTRUCTION in request.system_instruction
        evidence = request.input_data.get("original_input", request.input_data)
        assert evidence["memory"] == memory


@pytest.mark.parametrize("role", ["intent_clarity", "intent"])
@pytest.mark.parametrize("has_raw", [True, False])
async def test_invalid_generated_json_enters_bounded_intent_repair(role, has_raw):
    from dynamic_graph import FakeModelClient as RawModel

    raw = '{"questions": [broken]}'
    syntax = {"message": "Expecting value", "line": 1, "column": 16, "position": 15}
    invalid = ModelCallError(
        "MODEL_RESPONSE_INVALID",
        "Invalid JSON",
        raw_response=raw if has_raw else None,
        details={"json_syntax": syntax},
    )
    clear = {
        "references": [],
        "known_referents": {},
        "selection_criteria": [],
        "questions": [],
        "reason": "核心内容已完整",
    }
    responses = (
        [routing(), invalid, clear, ready()]
        if role == "intent_clarity"
        else [routing(), clear, invalid, ready()]
    )
    model = RawModel(responses)
    result = await IntentRecognizer(model).advance(
        IntentSession(), "给客户起草进度邮件，设计已完成"
    )
    assert result.goal is not None and not result.questions
    calls = [request for request in model.requests if request.role == role]
    assert len(calls) == 2
    assert calls[-1].input_data["previous_response"] == (raw if has_raw else None)
    assert calls[-1].input_data["validation_errors"][0]["json_syntax"] == syntax
    assert calls[0].timeout_seconds >= calls[-1].timeout_seconds > 0


async def test_repeated_invalid_json_does_not_extend_the_intent_repair_limit():
    from dynamic_graph import FakeModelClient as RawModel

    errors = [
        ModelCallError("MODEL_RESPONSE_INVALID", "Invalid JSON", raw_response="{bad}")
        for _ in range(2)
    ]
    clear = {
        "references": [],
        "known_referents": {},
        "selection_criteria": [],
        "questions": [],
        "reason": "核心内容已完整",
    }
    model = RawModel([routing(), clear, *errors])
    with pytest.raises(ModelCallError) as raised:
        await IntentRecognizer(model).advance(IntentSession(), "给客户起草进度邮件，设计已完成")
    assert raised.value is errors[-1]
    assert len([request for request in model.requests if request.role == "intent"]) == 2


async def test_review_preserves_existing_quote_for_unchanged_explicit_identification():
    from dynamic_graph import FakeModelClient as RawModel

    known = {
        "references": [
            {
                "expression": "那个文件",
                "candidates": ["甲目录文件", "乙目录文件"],
                "resolution": "explicit_identification",
                "evidence": "那个文件指甲目录文件",
            }
        ],
        "known_referents": {"文件": "用户指名的文件"},
        "selection_criteria": [],
        "questions": [],
        "reason": "用户已消除歧义",
    }
    reviewed = {
        **known,
        "references": [{**known["references"][0], "evidence": "复核说明：用户已消除歧义"}],
    }
    model = RawModel(
        [routing("respond", types=["question"]), known, reviewed, reply("对象是甲目录文件。")]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(), "那个文件指甲目录文件。你知道对象是哪一个了吗？"
    )
    assert result.reply == "对象是甲目录文件。" and not result.questions
    response = model.requests[-1]
    assert response.input_data["clarity"]["references"][0]["evidence"] == "那个文件指甲目录文件"


async def test_explicit_referent_evidence_can_come_from_provided_context():
    from dynamic_graph import FakeModelClient as RawModel

    model = RawModel(
        [
            routing("respond", types=["question"]),
            {
                "references": [
                    {
                        "expression": "她",
                        "candidates": ["周宁", "沈清"],
                        "resolution": "explicit_identification",
                        "evidence": "这里她指沈清",
                    }
                ],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [],
                "reason": "上下文已明确",
            },
            reply("沈清收到了信。"),
        ]
    )
    session = IntentSession(user_context={"notes": {"identity": "这里她指沈清"}})
    result = await IntentRecognizer(model).advance(
        session, "周宁告诉沈清，她收到了信。是谁收到了信？"
    )
    assert result.reply == "沈清收到了信。" and not result.questions
    assert session.user_context == {"notes": {"identity": "这里她指沈清"}}


@pytest.mark.parametrize(
    "kind,scope,evidence,clarifies",
    [
        ("unknown_identity", "蓝砂", "《蓝砂》里的灰羽是谁扮演的？", False),
        ("unknown_identity", "红河", "《蓝砂》里的灰羽是谁扮演的？", True),
        ("unknown_identity", "蓝砂", "灰羽属于蓝砂", True),
        ("unknown_identity", "", "", True),
        ("ambiguous_reference", "蓝砂", "《蓝砂》里的灰羽是谁扮演的？", True),
    ],
)
async def test_scoped_identity_gap_requires_grounded_location_and_keeps_real_ambiguity(
    kind, scope, evidence, clarifies
):
    from dynamic_graph import FakeModelClient as RawModel

    assessment = {
        "references": [
            {
                "expression": "灰羽",
                "candidates": ["片中角色", "范围外同名歌曲"],
                "resolution": "unresolved",
                "evidence": "《蓝砂》里的灰羽是谁扮演的？",
            }
        ],
        "known_referents": {"蓝砂": "用户明确给出的作品范围"},
        "selection_criteria": [],
        "questions": [
            {
                "text": "灰羽具体指什么？",
                "kind": kind,
                "subject": "灰羽",
                "lookup_scope": scope,
                "scope_evidence": evidence,
            }
        ],
        "reason": "模型不知道该名字的身份",
    }
    model = RawModel([routing(), assessment, assessment, ready()])
    result = await IntentRecognizer(model).advance(IntentSession(), "《蓝砂》里的灰羽是谁扮演的？")
    assert bool(result.questions) is clarifies
    assert (result.goal is None) is clarifies
    if not clarifies:
        reference = model.requests[-1].input_data["clarity"]["references"][0]
        assert reference["resolution"] == "explicit_identification"
        assert reference["evidence"] == evidence


@pytest.mark.parametrize(
    "recognized,canonical_name,clarifies",
    [(True, "Solenara", False), (False, "Solenara", True), (True, "Solenaria", True)],
)
async def test_disputed_entity_definition_needs_exact_independent_recognition(
    recognized, canonical_name, clarifies
):
    from dynamic_graph import FakeModelClient as RawModel

    initial = {
        "references": [],
        "known_referents": {"Solenara": "一种公开协议的名称"},
        "selection_criteria": [],
        "questions": [],
        "reason": "已识别对象",
    }
    reviewed = {
        **initial,
        "questions": [
            {"text": "Solenara 是什么？", "kind": "unknown_identity", "subject": "Solenara"}
        ],
        "reason": "复核未识别该名称",
    }
    model = RawModel(
        [
            routing(),
            initial,
            reviewed,
            {
                "recognized": recognized,
                "canonical_name": canonical_name,
                "definition": "一种公开协议的完整定义" if recognized else "",
                "reason": "独立核验精确名称",
            },
            ready(),
        ]
    )
    result = await IntentRecognizer(model).advance(IntentSession(), "Solenara 的覆盖范围是什么？")
    assert bool(result.questions) is clarifies
    assert (result.goal is None) is clarifies
    audit = next(request for request in model.requests if request.role == "intent_entity_check")
    assert audit.input_data["subject"] == "Solenara"
    assert audit.input_data["proposed_definition"] == "一种公开协议的名称"
    assert model.requests[1].timeout_seconds >= audit.timeout_seconds > 0
