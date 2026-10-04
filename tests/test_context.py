"""Behavioral memory tests use source-bearing responses, never a real home directory."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from dynamic_graph.models.client import ModelCallError, ModelResponse
from intent_helpers import TaskIntentModel
from langchain_core.embeddings import Embeddings

from karen.context import (
    ContextEvent,
    ContextMemory,
    DetailQuery,
    MemoryFlushError,
    MemoryQueueFull,
    PersistenceError,
    RecallQuery,
    TimeRange,
)
from karen.context.contracts import Evidence
from karen.context.storage import Storage, encode

UTC = timezone.utc


class LocalEmbeddings(Embeddings):
    model = "deterministic-test-v1"
    fail = False

    def embed_documents(self, texts):
        if self.fail:
            raise OSError("embedding offline")
        return [[1.0, float("上海" in text), float("北京" in text), 0.25] for text in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


class MemoryModel:
    def __init__(self):
        self.requests = []
        self.fail_rank = False
        self.fail_extract = False
        self.slow_extract = None
        self.dependency = "none"
        self.history_status = "none"
        self.kind = "relevance"
        self.time_mode = "current"
        self.at = None
        self.time_range = None
        self.fact_key = "profile.residence.city"

    async def generate(self, request):
        self.requests.append(request)
        data = request.input_data
        if request.role == "memory_extract":
            if self.slow_extract:
                await self.slow_extract.wait()
            if self.fail_extract:
                raise ModelCallError("TEST_AUTH_FAILED", "secret", retryable=False)
            event = next(e for e in data["events"] if e["event_id"] == data["new_event_id"])
            text = event["payload"].get("content", "")
            if not text:
                return ModelResponse({"facts": [], "summaries": []})
            evidence = [
                {"event_id": event["event_id"], "pointer": "/payload/content", "quote": text}
            ]
            facts = []
            city = "上海" if "上海" in text else "北京" if "北京" in text else None
            if city:
                facts.append(
                    {
                        "candidate_id": "city",
                        "fact_key": self.fact_key,
                        "value": city,
                        "text": text,
                        "evidence": evidence,
                    }
                )
            return ModelResponse(
                {
                    "facts": facts,
                    "summaries": [{"text": text, "event_kind": "statement", "evidence": evidence}],
                }
            )
        if request.role == "memory_verify":
            decisions = []
            for fact in data["candidates"]:
                old = [m for m in data["existing"] if m["state"] == "active"]
                operation = (
                    "new"
                    if not old
                    else "reinforce"
                    if old[0]["value"] == fact["value"]
                    else "correct"
                    if "纠正" in fact["text"]
                    else "replace"
                    if "搬" in fact["text"]
                    else "conflict"
                )
                decisions.append(
                    {
                        "candidate_id": fact["candidate_id"],
                        "verification": "supported",
                        "operation": operation,
                        "matched_ids": [m["memory_id"] for m in old],
                        "reason": "direct captured evidence",
                    }
                )
            return ModelResponse({"decisions": decisions})
        if request.role == "memory_query":
            result = {
                "search_text": data["text"],
                "dialogue_dependency": self.dependency,
                "kind": self.kind,
                "time_mode": self.time_mode,
                "needed_fact_keys": ["profile.residence.city"] if "住" in data["text"] else [],
            }
            if self.at:
                result["at"] = self.at.isoformat()
            if self.time_range:
                result["time_range"] = self.time_range.model_dump(mode="json")
            return ModelResponse(result)
        if request.role == "memory_rerank":
            if self.fail_rank:
                raise ModelCallError("RERANK_OFFLINE", "secret")
            anchors = data["history_candidates"]
            selected = anchors[:2] if self.history_status == "selected" else []
            return ModelResponse(
                {
                    "ranking": [
                        {"memory_id": mid, "relevance": "relevant", "reason": "test"}
                        for mid in data["primary_ids"]
                    ],
                    "history_status": self.history_status,
                    "selected_event_ids": [e["event_id"] for e in selected],
                    "related_request_ids": list(dict.fromkeys(e["request_id"] for e in selected)),
                    "history_reason": "resolved source" if selected else "test",
                }
            )
        raise AssertionError(request.role)


@pytest.fixture
async def memory(tmp_path):
    model = MemoryModel()
    service = ContextMemory(
        root_dir=tmp_path / "context", model=model, embeddings=LocalEmbeddings()
    )
    await service.start()
    yield service, model
    await service.close()


def event(
    text, *, request="task-1", kind="user_message", occurred_at=None, timezone="Asia/Shanghai"
):
    return ContextEvent(
        conversation_id="conversation",
        request_id=request,
        event_type=kind,
        timezone=timezone,
        payload={"content": text},
        **({"occurred_at": occurred_at} if occurred_at else {}),
    )


def query(text):
    return RecallQuery(
        text=text, conversation_id="conversation", request_id="new-task", timezone="Asia/Shanghai"
    )


async def test_future_move_plan_is_indexed_without_replacing_current_residence(tmp_path):
    class PlannedMoveModel(MemoryModel):
        async def generate(self, request):
            if request.role == "memory_extract":
                response = await super().generate(request)
                if response.payload["facts"] and "下个月" in response.payload["facts"][0]["text"]:
                    fact = response.payload["facts"][0]
                    fact["fact_key"] = "profile.residence.move_plan"
                    fact["value"] = {
                        "city": "上海",
                        "planned_for": {
                            "value": "2026-11",
                            "precision": "month",
                            "timezone": "Asia/Shanghai",
                            "origin": "inferred",
                        },
                        "status": "planned",
                    }
                return response
            if request.role == "memory_verify":
                candidate = request.input_data["candidates"][0]
                if candidate["fact_key"] == "profile.residence.move_plan":
                    return ModelResponse(
                        {
                            "decisions": [
                                {
                                    "candidate_id": candidate["candidate_id"],
                                    "verification": "supported",
                                    "operation": "new",
                                    "matched_ids": [],
                                    "reason": "未来计划与当前住所是不同事实",
                                }
                            ]
                        }
                    )
            return await super().generate(request)

    service = ContextMemory(
        root_dir=tmp_path / "context", model=PlannedMoveModel(), embeddings=LocalEmbeddings()
    )
    await service.start()
    try:
        current = event(
            "我目前住在北京。",
            request="current-residence",
            occurred_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        await service.flush(service.submit(current))
        planned = event(
            "我下个月会搬到上海。",
            request="move-plan",
            occurred_at=datetime(2026, 10, 4, 10, tzinfo=UTC),
        )
        receipt = service.submit(planned)
        await service.flush(receipt)
        _, stored, vectors = service.storage.snapshot()
        facts = {m.fact_key: m for m in stored.values() if m.layer == "m1"}
        residence, plan = facts["profile.residence.city"], facts["profile.residence.move_plan"]
        assert (
            residence.value == "北京" and residence.state == "active" and residence.valid_to is None
        )
        assert plan.value["city"] == "上海" and plan.value["status"] == "planned"
        assert plan.value["planned_for"]["value"] == "2026-11"
        assert plan.supersedes == [] and plan.corrects == [] and plan.state == "active"
        assert residence.memory_id in vectors and plan.memory_id in vectors
        assert (
            service.storage.load_event(planned.event_id).payload["content"]
            == planned.payload["content"]
        )
    finally:
        await service.close()


async def test_submit_is_isolated_nonblocking_and_close_persists_without_model(memory):
    service, model = memory
    model.slow_extract = asyncio.Event()
    original = event("一条原始消息")
    started = time.monotonic()
    receipt = service.submit(original)
    original.payload["content"] = "外部修改"
    assert time.monotonic() - started < 0.05
    await asyncio.wait_for(service.close(), 2)
    assert service.storage.load_event(receipt.event_id).payload["content"] == "一条原始消息"
    assert service.storage.job(receipt.event_id)["derived"] in {"pending", "extracting"}
    recovered = ContextMemory(
        root_dir=service.root_dir, model=MemoryModel(), embeddings=LocalEmbeddings()
    )
    await recovered.start()
    await asyncio.wait_for(recovered.flush(), 3)
    assert (await recovered.write_status(receipt)).index == "indexed"
    await recovered.close()


async def test_full_fact_extraction_change_recall_and_exact_source_lookup(memory):
    service, model = memory
    first = service.submit(event("我长期住在北京"))
    await service.flush(first)
    second = service.submit(event("我已经从北京搬到上海了", request="task-2"))
    await service.flush(second)
    result = await service.recall(query("我现在住在哪里"))
    assert result.status == "ok"
    active = [h.memory for h in result.m1 if not h.evidence_only]
    assert [m.value for m in active] == ["上海"]
    old = next(h for h in result.m1 if h.memory.value == "北京")
    assert old.evidence_only and old.memory.state == "superseded"
    assert active[0].supersedes == [old.memory.memory_id]
    assert any(h.memory.related_memory_ids for h in result.m2)
    detail = await service.search_details(DetailQuery(text="", sources=active[0].sources))
    assert detail.status == "complete"
    assert detail.hits[0].text == "我已经从北京搬到上海了"
    assert detail.hits[0].source.source_role == "user"
    assert all("memory" not in r.input_data for r in model.requests if r.role == "memory_query")
    assert len(encode(result.context()).encode()) <= 12 * 1024


async def test_reinforcement_uses_unique_direct_sources_not_assistant_echoes(memory):
    service, _ = memory
    receipts = [service.submit(event("我住在北京", request=f"task-{i}")) for i in range(2)]
    for receipt in receipts:
        await service.flush(receipt)
    echo = service.submit(event("你住在北京", kind="assistant_message", request="task-3"))
    await service.flush(echo)
    _, memories, _ = service.storage.snapshot()
    facts = [m for m in memories.values() if m.layer == "m1"]
    assert len(facts) == 1 and len(facts[0].sources) == 2
    assert all(s.source_role == "user" for s in facts[0].sources)


@pytest.mark.parametrize(
    "text,state", [("纠正：我住在上海", "corrected"), ("我住在上海", "conflicted")]
)
async def test_correction_and_unexplained_conflict_preserve_versions(memory, text, state):
    service, _ = memory
    first = service.submit(event("我住在北京"))
    await service.flush(first)
    second = service.submit(event(text, request="task-2"))
    await service.flush(second)
    result = await service.recall(query("我住哪里"))
    old = next(h.memory for h in result.m1 if h.memory.value == "北京")
    new = next(h.memory for h in result.m1 if h.memory.value == "上海")
    assert old.state == state
    if state == "conflicted":
        assert old.conflict_group_id == new.conflict_group_id
        assert all(h.evidence_only for h in result.m1)
    else:
        assert new.corrects == [old.memory_id]


async def test_known_at_does_not_leak_later_state_or_reinforcement(memory):
    service, model = memory
    first = service.submit(event("我住在北京"))
    await service.flush(first)
    as_of = datetime.now(UTC)
    await asyncio.sleep(0.01)
    second = service.submit(event("我搬到上海", request="task-2"))
    await service.flush(second)
    model.time_mode, model.at = "known_at", as_of
    result = await service.recall(query("当时你认为我住在哪里"))
    assert {h.memory.value for h in result.m1} == {"北京"}
    assert result.m1[0].memory.state == "active"
    assert result.query_time_basis["mode"] == "known_at"


async def test_rerank_failure_retains_fusion_order_and_marks_unverified(memory):
    service, model = memory
    receipt = service.submit(event("我长期住在北京"))
    await service.flush(receipt)
    model.fail_rank = True
    result = await service.recall(query("我住在哪里"))
    assert result.status == "degraded"
    assert "RERANK_FAILED_FUSION_ORDER" in result.degradations
    assert result.m1 and all(
        h.relevance == "unverified" and h.rerank_rank is None for h in result.m1
    )
    no_answer = await service.recall(query("太阳表面温度是多少"))
    assert no_answer.m1 == [] and no_answer.m2 == []


async def test_embedding_failure_keeps_metadata_bm25_and_reports_index_failure(memory):
    service, model = memory
    service.embeddings.fail = True
    receipt = service.submit(event("我长期住在北京"))
    # Finite retry behavior is observed without waiting real backoff intervals.
    for _ in range(3):
        for _ in range(100):
            status = await service.write_status(receipt)
            if status.attempts:
                break
            await asyncio.sleep(0.01)
        service.storage.update_job(receipt.event_id, next_attempt_at=0)
        previous = status.attempts
        for _ in range(100):
            if (
                await service.write_status(receipt)
            ).attempts > previous or status.index == "failed":
                break
            await asyncio.sleep(0.01)
    with pytest.raises(MemoryFlushError):
        await asyncio.wait_for(service.flush(receipt), 2)
    result = await service.recall(query("我长期住在北京吗"))
    assert result.m1 and "VECTOR_RECALL_UNAVAILABLE" in result.degradations
    assert (await service.write_status(receipt)).raw == "persisted"


async def test_failed_extraction_does_not_block_following_job(memory):
    service, model = memory
    model.fail_extract = True
    failed = service.submit(event("坏事件"))
    with pytest.raises(MemoryFlushError):
        await asyncio.wait_for(service.flush(failed), 2)
    model.fail_extract = False
    good = service.submit(event("我住在北京", request="task-2"))
    await asyncio.wait_for(service.flush(good), 2)
    assert (await service.write_status(good)).index == "indexed"
    assert (await service.write_status(failed)).error_code == "TEST_AUTH_FAILED"


async def test_foreground_blocks_new_background_calls_but_raw_records_persist(memory):
    service, model = memory
    async with service.foreground():
        receipt = service.submit(event("我住在北京"))
        for _ in range(100):
            if (await service.write_status(receipt)).raw == "persisted":
                break
            await asyncio.sleep(0.005)
        assert (await service.write_status(receipt)).raw == "persisted"
        assert model.requests == []
        with pytest.raises(RuntimeError, match="FOREGROUND"):
            await service.flush()
    await service.flush(receipt)


async def test_related_pending_events_are_selected_and_unrelated_history_is_absent(memory):
    service, model = memory
    async with service.foreground():
        service.submit(event("生成 /tmp/report.html", request="old-task"))
        independent = await service.recall(query("写一首诗"))
        assert independent.history.messages == []
        assert not any(r.role == "memory_rerank" for r in model.requests)
        model.dependency, model.history_status = "needed", "selected"
        related = await service.recall(query("把刚才那个页面换成蓝色"))
        assert related.history.status == "selected"
        assert "/tmp/report.html" in related.history.messages[0]["content"]
        query_calls = [r for r in model.requests if r.role == "memory_query"]
        assert all("report.html" not in encode(r.input_data) for r in query_calls)
        model.history_status = "ambiguous"
        ambiguous = await service.recall(query("再打开那个页面"))
        assert ambiguous.history.status == "ambiguous" and ambiguous.history.messages == []


async def test_current_clarification_does_not_reload_its_own_summary_as_history(memory):
    service, model = memory
    await service.flush(service.submit(event("整理公园路线清单", request="current-task")))
    model.dependency = "current_task"
    result = await service.recall(
        query("主要是骑行路线").model_copy(
            update={
                "request_id": "current-task",
                "current_task_messages": [
                    {"role": "user", "content": "整理公园路线清单"},
                    {"role": "assistant", "content": "哪一类路线？"},
                ],
            }
        )
    )
    assert result.status == "empty" and result.history.status == "none"
    assert not result.m2 and not result.related_request_ids
    assert result.coverage["dialogue_dependency"] == "current_task"
    assert not result.coverage["requires_history"]
    assert not any(r.role == "memory_rerank" for r in model.requests)


async def test_external_history_anchors_exclude_sources_from_current_task(memory):
    service, model = memory
    await service.flush(service.submit(event("我住在北京", request="current-task")))
    await service.flush(service.submit(event("我住在北京", request="old-task")))
    model.dependency, model.history_status = "needed", "selected"
    result = await service.recall(
        query("沿用之前北京方案中的参数").model_copy(
            update={
                "request_id": "current-task",
                "current_task_messages": [{"role": "user", "content": "请沿用旧方案"}],
            }
        )
    )
    assert result.history.status == "selected"
    assert result.related_request_ids == ["old-task"]
    assert all(m["request_id"] != "current-task" for m in result.history.messages)
    rank_request = next(r for r in model.requests if r.role == "memory_rerank")
    assert all(
        e["request_id"] != "current-task" for e in rank_request.input_data["history_candidates"]
    )


async def test_missing_external_history_still_clarifies_during_current_task(memory):
    from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, ModelBindings
    from test_input_routing import clarify, routing

    from karen import IntentRecognizer, IntentSession, Karen

    service, backend = memory
    intent_model = FakeModelClient(
        [routing(), clarify("格式有要求吗？"), routing(types=["task_control"], relation="continue")]
    )
    executor = FakeModelClient()
    agent = Karen(
        intent=IntentRecognizer(intent_model),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=service.root_dir / "runs"),
            models=ModelBindings(executor, executor),
        ),
        memory=service,
    )
    async with service.foreground():
        first = await agent.advance(IntentSession(timezone="Asia/Shanghai"), "帮我整理一份报告")
        backend.dependency, backend.history_status = "needed", "unavailable"
        second = await agent.advance(first.session, "沿用上周那份报告的格式")
    assert second.session.request_id == first.session.request_id
    assert second.result is None and second.memory_result.coverage["requires_history"]
    assert "哪一次任务" in second.session.questions[0]
    assert len(intent_model.requests) == 3 and not executor.requests


async def test_details_require_scope_decode_unicode_and_preserve_field_pointer(memory):
    service, _ = memory
    receipt = service.submit(event("参数是中文路径 /tmp/报告.html"))
    await service.flush(receipt)
    assert (await service.search_details(DetailQuery(text="报告"))).status == "needs_scope"
    result = await service.search_details(DetailQuery(text="报告", request_id="task-1"))
    assert result.status == "complete" and result.hits
    assert result.hits[0].source.pointer == "/payload/content"
    assert "/tmp/报告.html" in result.hits[0].text
    with pytest.raises(ValueError, match="SOURCE_QUOTE"):
        service.storage.source(
            Evidence(event_id=receipt.event_id, pointer="/payload/content", quote="fabricated"),
            {receipt.event_id: service.storage.load_event(receipt.event_id)},
        )


async def test_duplicate_ids_redaction_queue_limit_and_role_cannot_be_spoofed(memory):
    service, _ = memory
    original = event("password=secret-value")
    async with service.foreground():
        receipt = service.submit(original)
        assert service.submit(original) == receipt
        with pytest.raises(ValueError, match="MISMATCH"):
            service.submit(original.model_copy(update={"payload": {"content": "different"}}))
        await service._queue.join()
        stored = service.storage.load_event(receipt.event_id)
        assert "secret-value" not in encode(stored.model_dump(mode="json"))
        service._pending_bytes = 8 * 1024 * 1024
        with pytest.raises(MemoryQueueFull):
            service.submit(event("overflow"))
        service._pending_bytes = 0


async def test_second_writer_is_rejected_and_lock_released_on_close(memory):
    service, _ = memory
    second = ContextMemory(
        root_dir=service.root_dir, model=MemoryModel(), embeddings=LocalEmbeddings()
    )
    with pytest.raises(BlockingIOError):
        await second.start()
    await service.close()
    await second.start()
    await second.close()


def test_recovery_after_fsync_before_registration_and_incomplete_tail(tmp_path, monkeypatch):
    storage = Storage(tmp_path)
    storage.initialize()
    record = event("recover me")
    actual = storage._register
    monkeypatch.setattr(storage, "_register", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        storage.append(record, 1, datetime.now(UTC))
    path = next((tmp_path / "m3").glob("*.jsonl"))
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    monkeypatch.setattr(storage, "_register", actual)
    storage.recover()
    assert storage.load_event(record.event_id) == record
    assert storage.job(record.event_id)["derived"] == "pending"
    assert path.read_bytes().endswith(b"\n")


def test_malformed_middle_frame_is_not_silently_discarded(tmp_path):
    storage = Storage(tmp_path)
    storage.initialize()
    (tmp_path / "m3" / "2026-10-03.jsonl").write_text('{"broken":\n')
    with pytest.raises(PersistenceError, match="RECOVERY_CORRUPT"):
        storage.recover()


async def test_rotation_uses_source_timezone_and_attachment_read_is_bounded(memory):
    service, _ = memory
    async with service.foreground():
        receipt = service.submit(
            event("长原文" * 20000, occurred_at=datetime(2026, 10, 2, 16, 30, tzinfo=UTC))
        )
        await service._queue.join()
        assert (service.root_dir / "m3" / "2026-10-03.jsonl").exists()
        assert list((service.root_dir / "attachments").glob("*.json"))
        with pytest.raises(PersistenceError, match="BUDGET"):
            service.storage.load_event(receipt.event_id, max_bytes=1000)
        assert service.storage.load_event(receipt.event_id).payload["content"] == "长原文" * 20000


async def test_collection_counts_all_tasks_without_topk_and_deduplicates_results(memory):
    service, model = memory
    async with service.foreground():
        now = datetime.now(UTC)
        for index in range(15):
            for attempt in range(2):
                service.submit(
                    ContextEvent(
                        conversation_id="conversation",
                        request_id=f"task-{index}",
                        event_type="task_result",
                        timezone="UTC",
                        payload={
                            "run_id": f"run-{index}-{attempt}",
                            "execution_status": "COMPLETED",
                            "output_complete": True,
                        },
                    )
                )
        await service._queue.join()
        model.kind = "collection"
        model.time_range = TimeRange(start=now - timedelta(days=1), end=now + timedelta(days=1))
        result = await service.recall(query("今天完成了多少任务"))
        assert result.collection["total_matched"] == 15
        assert result.collection["returned_count"] == 15
        assert all(item["run_id"].endswith("-1") for item in result.collection["items"])


async def test_reindex_rebuilds_vectors_without_reextracting_facts(memory):
    service, model = memory
    receipt = service.submit(event("我住在北京"))
    await service.flush(receipt)
    count = len([r for r in model.requests if r.role == "memory_extract"])
    service.embeddings.model = "new-vector-model"
    await service.reindex()
    await service.flush()
    _, _, vectors = service.storage.snapshot()
    assert all(v["model_tag"].endswith("new-vector-model") for v in vectors.values())
    assert count == len([r for r in model.requests if r.role == "memory_extract"])


async def test_ambiguous_history_clarifies_then_reassesses_current_task(memory):
    from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, ModelBindings
    from test_execution import graph_response, ready

    from karen import IntentRecognizer, IntentSession, Karen

    service, model = memory
    executor = FakeModelClient([graph_response(), {"draft": "依据所选来源写的草稿"}])
    intent_model = TaskIntentModel([ready()])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=service.root_dir / "runs"),
        models=ModelBindings(executor, executor),
    )
    agent = Karen(intent=IntentRecognizer(intent_model), engine=engine, memory=service)
    reference = datetime(2026, 10, 3, 12, 54, tzinfo=UTC)
    async with service.foreground():
        service.submit(event("给甲客户写中文邮件", request="old-task"))
        model.dependency, model.history_status = "needed", "ambiguous"
        turn = await agent.advance(
            IntentSession(
                conversation_id="conversation",
                timezone="Asia/Shanghai",
                reference_time_utc=reference,
            ),
            "改一下刚才那封邮件",
        )
        assert turn.result is None and "任务" in turn.session.questions[0]
        assert intent_model.assessments == [] and executor.requests == []
        model.history_status = "selected"
        turn = await agent.advance(turn.session, "是给甲客户的那封")
        assert turn.result.execution_status == "COMPLETED"
        assert turn.session.goal.context["memory"]["history"]["status"] == "selected"
        assert turn.session.goal.context["timezone"] == "Asia/Shanghai"
        assert len(turn.session.goal.context["conversation"]) == 3
        assert intent_model.assessments[0].input_data["memory"]["history"]["messages"]
        query_requests = [r for r in model.requests if r.role == "memory_query"]
        assert all(
            r.input_data["current_time_utc"] == reference.isoformat() for r in query_requests
        )
        assert (
            intent_model.assessments[0].input_data["time_context"]["reference_time_utc"]
            == reference.isoformat()
        )
        agent.record_response(turn.session, "依据所选来源写的草稿")
    await service.flush()
    types = {
        row["event_type"] for row in service.storage.event_rows(request_id=turn.session.request_id)
    }
    assert types == {"user_message", "assistant_message", "goal_created", "task_result"}


async def test_independent_new_task_goal_does_not_inherit_previous_task_messages(memory):
    from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, ModelBindings
    from test_execution import graph_response, ready

    from karen import IntentRecognizer, IntentSession, Karen

    service, _ = memory
    executor = FakeModelClient(
        [graph_response(), {"draft": "第一封"}, graph_response(), {"draft": "第二封"}]
    )
    intent_model = TaskIntentModel([ready(), ready()])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=service.root_dir / "runs"),
        models=ModelBindings(executor, executor),
    )
    agent = Karen(intent=IntentRecognizer(intent_model), engine=engine, memory=service)
    first = await agent.advance(IntentSession(timezone="UTC"), "写第一封邮件")
    second = await agent.advance(first.session, "写一封全新的邮件")
    assert first.session.request_id != second.session.request_id
    assert first.session.conversation_id == second.session.conversation_id
    assert len(second.session.messages) == 1
    assert second.session.goal.context["memory"]["history"]["messages"] == []
    assert len(intent_model.assessments[1].input_data["messages"]) == 1


async def test_metadata_and_vector_transaction_reject_invalid_batch_atomically(memory):
    service, _ = memory
    receipt = service.submit(event("我住在北京"))
    await service.flush(receipt)
    _, memories, original = service.storage.snapshot()
    with pytest.raises(ValueError, match="DIMENSIONS"):
        service.storage.save_vectors(list(memories.values()), [[1, 0], [1, 0, 0]], "test-model")
    assert service.storage.snapshot()[2] == original
    with pytest.raises(ValueError, match="ZERO"):
        service.storage.save_vectors(
            list(memories.values()),
            [[0, 0, 0, 0]] * len(memories),
            next(iter(original.values()))["model_tag"],
        )
    assert service.storage.snapshot()[2] == original


async def test_late_older_statement_cannot_overwrite_newer_fact(memory):
    service, _ = memory
    now = datetime.now(UTC)
    first = service.submit(event("我住在上海", occurred_at=now))
    await service.flush(first)
    late = service.submit(event("我搬到北京", request="late", occurred_at=now - timedelta(days=10)))
    # Historical ordering failure is retained rather than applied as a current change.
    for _ in range(3):
        for _ in range(100):
            job = service.storage.job(late.event_id)
            if job and (job["next_attempt_at"] > 0 or job["derived"] == "failed"):
                break
            await asyncio.sleep(0.01)
        service.storage.update_job(late.event_id, next_attempt_at=0)
    with pytest.raises(MemoryFlushError):
        await asyncio.wait_for(service.flush(late), 3)
    result = await service.recall(query("我现在住哪里"))
    assert [h.memory.value for h in result.m1 if not h.evidence_only] == ["上海"]


async def test_raw_write_failure_is_observable_and_close_reports_it(memory, monkeypatch):
    service, _ = memory

    def fail(*args):
        raise OSError("disk full: credential=never-print")

    monkeypatch.setattr(service.storage, "append", fail)
    receipt = service.submit(event("原始记录"))
    await service._queue.join()
    status = await service.write_status(receipt)
    assert status.raw == "failed" and status.error_code == "MEMORY_PERSISTENCE_FAILED"
    with pytest.raises(PersistenceError, match="MEMORY_PERSISTENCE_FAILED") as caught:
        await service.close()
    assert "credential" not in str(caught.value)


async def test_old_pending_job_is_extracted_even_with_more_than_twelve_later_events(memory):
    service, _ = memory
    async with service.foreground():
        first = service.submit(event("我住在北京"))
        for index in range(20):
            service.submit(event(f"澄清补充 {index}", kind="assistant_message"))
        await service._queue.join()
    await asyncio.wait_for(service.flush(first), 3)
    assert (await service.write_status(first)).derived == "committed"


def test_temporal_precision_and_current_historical_versions():
    from karen.context.contracts import QueryAnalysis, StoredMemory, TemporalValue
    from karen.context.retrieval import evidence_only, time_bounds

    date = TemporalValue(value="2026-09", precision="month", timezone="Asia/Shanghai")
    assert (time_bounds(date)[1] - time_bounds(date)[0]).days == 30
    now = datetime.now(UTC)
    old = StoredMemory(
        memory_id="old",
        layer="m1",
        text="北京",
        value="北京",
        sources=[],
        state="superseded",
        recorded_at=now,
        created_at=now,
        updated_at=now,
        request_id="old-task",
        valid_from=date,
        valid_to=TemporalValue(value="2026-10-02", precision="day", timezone="Asia/Shanghai"),
    )
    at = QueryAnalysis(
        search_text="居住", time_mode="effective_at", at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert not evidence_only(old, at)
    boundary = at.model_copy(update={"at": datetime(2026, 9, 15, tzinfo=UTC)})
    assert evidence_only(old, boundary)
    assert evidence_only(old, QueryAnalysis(search_text="居住"))


async def test_query_never_obeys_rerank_invented_history_ids(memory):
    service, model = memory
    service.submit(event("我住在北京"))
    await service.flush()
    model.history_status = "selected"  # Invalid for an independent input.
    result = await service.recall(query("我住在哪个城市"))
    assert result.history.messages == []
    assert "RERANK_FAILED_FUSION_ORDER" in result.degradations


async def test_detail_search_budget_reports_partial_instead_of_no_evidence(memory):
    service, _ = memory
    async with service.foreground():
        for i in range(205):
            service.submit(event(f"消息 {i} 中文路径 /tmp/报告.html"))
        await service._queue.join()
        result = await service.search_details(DetailQuery(text="报告", request_id="task-1"))
        assert result.status == "partial" and result.scanned_events == 200
        assert len(result.hits) == 200


async def test_semantic_fact_matching_keeps_canonical_slot_when_new_key_differs(memory):
    service, model = memory
    first = service.submit(event("我住在北京"))
    await service.flush(first)
    model.fact_key = "user.home.city"
    second = service.submit(event("我搬到上海", request="task-2"))
    await service.flush(second)
    result = await service.recall(query("我现在住哪里"))
    assert {h.memory.fact_key for h in result.m1} == {"profile.residence.city"}
    assert [h.memory.value for h in result.m1 if not h.evidence_only] == ["上海"]


async def test_project_scope_does_not_leak_to_other_project(tmp_path):
    class ProjectModel(MemoryModel):
        async def generate(self, request):
            response = await super().generate(request)
            if request.role == "memory_extract":
                new = next(
                    e
                    for e in request.input_data["events"]
                    if e["event_id"] == request.input_data["new_event_id"]
                )
                for fact in response.payload["facts"]:
                    fact["scope"] = {"kind": "project", "project_id": new["project_id"]}
            return response

    memory = ContextMemory(root_dir=tmp_path, model=ProjectModel(), embeddings=LocalEmbeddings())
    await memory.start()
    try:
        record = event("我在北京办公").model_copy(update={"project_id": "project-A"})
        await memory.flush(memory.submit(record))
        assert (await memory.recall(query("北京办公"))).m1 == []
        matched = await memory.recall(
            query("北京办公").model_copy(update={"project_id": "project-A"})
        )
        assert matched.m1 and matched.m2
        unrelated = await memory.recall(
            query("北京办公").model_copy(update={"project_id": "project-B"})
        )
        assert unrelated.m1 == [] and unrelated.m2 == []
    finally:
        await memory.close()


async def test_credentials_are_redacted_with_json_pointer_metadata(memory):
    service, _ = memory
    async with service.foreground():
        record = event("DEEPSEEK_API_KEY=short-secret Authorization: Bearer sensitive-token")
        record.payload["DEEPSEEK_API_KEY"] = "dict-secret"
        receipt = service.submit(record)
        await service._queue.join()
        stored = service.storage.load_event(receipt.event_id)
        text = encode(stored.model_dump(mode="json"))
        assert all(
            secret not in text for secret in ("short-secret", "sensitive-token", "dict-secret")
        )
        assert stored.redacted_fields == ["/payload/content", "/payload/DEEPSEEK_API_KEY"]


async def test_collection_missing_time_range_requests_clarification_without_executing(memory):
    from dynamic_graph import DynamicGraphEngine, ModelBindings

    from karen import IntentRecognizer, IntentSession, Karen

    service, model = memory
    model.kind = "collection"
    intent = TaskIntentModel()
    agent = Karen(
        intent=IntentRecognizer(intent),
        engine=DynamicGraphEngine(models=ModelBindings(intent, intent)),
        memory=service,
    )
    turn = await agent.advance(IntentSession(timezone="UTC"), "列出所有完成的任务")
    assert turn.result is None and "时间范围" in turn.session.questions[0]
    assert intent.assessments == []


async def test_partial_index_coverage_is_reported_without_waiting_for_derivation(memory):
    service, _ = memory
    async with service.foreground():
        receipt = service.submit(event("我住在北京"))
        await service._queue.join()
        result = await service.recall(query("北京"))
        assert result.coverage["index"]["derived_pending"] == 1
        assert (await service.write_status(receipt)).derived == "pending"


async def test_uncertain_verification_keeps_only_marked_m2_not_personal_m1(tmp_path):
    class UncertainModel(MemoryModel):
        async def generate(self, request):
            response = await super().generate(request)
            if request.role == "memory_verify":
                for decision in response.payload["decisions"]:
                    decision.update(
                        verification="uncertain", operation="ignore", reason="可能只是引用"
                    )
            return response

    memory = ContextMemory(root_dir=tmp_path, model=UncertainModel(), embeddings=LocalEmbeddings())
    await memory.start()
    try:
        await memory.flush(memory.submit(event("可能住在北京")))
        _, memories, _ = memory.storage.snapshot()
        assert not any(m.layer == "m1" for m in memories.values())
        assert any(m.verification_state == "uncertain" for m in memories.values())
        assert all(m.sources[0].source_role == "user" for m in memories.values())
    finally:
        await memory.close()


async def test_result_metadata_overrides_model_guess_and_links_artifacts(tmp_path):
    class ResultModel(MemoryModel):
        async def generate(self, request):
            if request.role == "memory_extract":
                event_id = request.input_data["new_event_id"]
                return ModelResponse(
                    {
                        "summaries": [
                            {
                                "text": "已请求打开页面",
                                "event_kind": "task_result",
                                "outcome": {"execution_status": "COMPLETED"},
                                "actions": [{"status": "attempted"}],
                                "evidence": [
                                    {
                                        "event_id": event_id,
                                        "pointer": "/payload/outputs/answer",
                                        "quote": "已请求打开页面",
                                    }
                                ],
                            }
                        ]
                    }
                )
            return await super().generate(request)

    memory = ContextMemory(root_dir=tmp_path, model=ResultModel(), embeddings=LocalEmbeddings())
    await memory.start()
    try:
        record = ContextEvent(
            conversation_id="conversation",
            request_id="task",
            timezone="UTC",
            event_type="task_result",
            payload={
                "execution_status": "FAILED",
                "output_complete": False,
                "outputs": {"answer": "已请求打开页面"},
                "artifacts": [{"uri": "/tmp/page.html"}],
            },
        )
        await memory.flush(memory.submit(record))
        _, memories, _ = memory.storage.snapshot()
        summary = next(iter(memories.values()))
        assert summary.outcome["execution_status"] == "FAILED"
        assert summary.outcome["output_complete"] is False
        assert summary.artifact_refs == [{"uri": "/tmp/page.html"}]
    finally:
        await memory.close()


async def test_normal_cli_exit_waits_for_raw_persistence(tmp_path, monkeypatch):
    from dynamic_graph import FakeModelClient, RunResult

    from karen import TaskTurn, cli

    service = ContextMemory(root_dir=tmp_path, model=MemoryModel(), embeddings=LocalEmbeddings())
    monkeypatch.setattr(cli, "create_memory", lambda model, **kwargs: service)
    monkeypatch.setattr(cli, "create_observer", lambda: cli.Observer())
    monkeypatch.setattr(cli, "deepseek_client", FakeModelClient)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    captured = []

    class DisplayAgent:
        async def advance(self, session, user_input):
            captured.append(service.submit(event(user_input)))
            return TaskTurn(
                session,
                RunResult(
                    run_id="result",
                    execution_status="COMPLETED",
                    output_complete=True,
                    outputs={"answer": "完成"},
                ),
            )

        def record_response(self, session, text, **kwargs):
            captured.append(service.submit(event(text, kind="assistant_message")))
            return ()

    monkeypatch.setattr(cli, "Karen", lambda **kwargs: DisplayAgent())
    inputs = iter(["记录后退出", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    assert await cli.converse(timezone="UTC") == 0
    assert all(service.storage.load_event(receipt.event_id) for receipt in captured)


async def test_fusion_fallback_keeps_record_order_with_related_versions(memory):
    service, model = memory
    await service.flush(service.submit(event("我住在北京")))
    await service.flush(service.submit(event("我搬到上海", request="task-2")))
    model.fail_rank = True
    result = await service.recall(query("我住在哪里"))
    assert result.m1 and all(hit.relevance == "unverified" for hit in result.m1)
    assert [hit.fusion_rank for hit in result.m1] == sorted(hit.fusion_rank for hit in result.m1)
    assert {hit.memory.state for hit in result.m1} == {"active", "superseded"}


async def test_observation_tracks_background_changes_recall_and_degradation(tmp_path):
    from karen.observability import ObservedModel, Observer
    from karen.observability.viewer import TraceStore

    observer = Observer(tmp_path / "observability")
    await observer.start()
    backend = MemoryModel()
    service = ContextMemory(
        root_dir=tmp_path / "context",
        model=ObservedModel(backend, observer),
        embeddings=LocalEmbeddings(),
        observer=observer,
    )
    with observer.span("startup", request_id="unrelated-startup"):
        await service.start()
    trace_id = __import__("uuid").uuid4().hex
    try:
        with observer.span(
            "turn", trace_id=trace_id, request_id="task-1", conversation_id="conversation"
        ):
            receipt = service.submit(event("我住在北京"))
        await service.flush(receipt)
        backend.fail_rank = True
        with observer.span(
            "turn", trace_id=trace_id, request_id="task-1", conversation_id="conversation"
        ):
            result = await service.recall(query("我住在哪里"))
        assert "RERANK_FAILED_FUSION_ORDER" in result.degradations
    finally:
        await service.close()
        await observer.close()
    detail = TraceStore(observer.root_dir).trace(trace_id)
    assert {"memory.persisted", "memory.indexed", "memory.recalled"} <= {
        e["event_type"] for e in detail["events"]
    }
    derived = next(
        e
        for e in detail["events"]
        if e["stage"] == "memory.derive" and e["event_type"] == "span.started"
    )
    assert derived["source_trace_id"] == trace_id and derived["source_event_id"] == receipt.event_id
    assert derived["trace_id"] != trace_id
    assert derived["parent_span_id"] is None
    assert any(
        e["stage"] == "memory.commit"
        and e["event_type"] == "decision"
        and e["data"]["committed_memory_ids"]
        for e in detail["events"]
    )
    assert any(
        e["stage"] == "memory.recall.retrieve"
        and e["event_type"] == "decision"
        and e["data"]["fusion_order"]
        for e in detail["events"]
    )
    assert detail["checks"][2]["status"] == "pass"


def test_recovery_does_not_follow_links_outside_memory_directory(tmp_path):
    root = tmp_path / "context"
    storage = Storage(root)
    storage.initialize()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("incomplete original data")
    (root / "m3" / "2026-10-03.jsonl").symlink_to(outside)
    with pytest.raises(PersistenceError, match="INVALID_MEMORY_PATH"):
        storage.recover()
    assert outside.read_text() == "incomplete original data"


async def test_unavailable_storage_preserves_history_dependency_for_clarification(
    memory, monkeypatch
):
    from dynamic_graph import DynamicGraphEngine, ModelBindings

    from karen import IntentRecognizer, IntentSession, Karen

    service, model = memory
    model.dependency = "needed"

    def unavailable(*args):
        raise PersistenceError("TEST_STORAGE_UNAVAILABLE")

    monkeypatch.setattr(service.storage, "retrieval_snapshot", unavailable)
    intent = TaskIntentModel()
    agent = Karen(
        intent=IntentRecognizer(intent),
        engine=DynamicGraphEngine(models=ModelBindings(intent, intent)),
        memory=service,
    )
    turn = await agent.advance(IntentSession(timezone="UTC"), "继续修改刚才的页面")
    assert turn.result is None and turn.memory_result.status == "unavailable"
    assert turn.session.questions and intent.assessments == []


def test_long_version_chain_resolves_current_state_without_unbounded_history():
    from karen.context.contracts import StoredMemory
    from karen.context.retrieval import relation_bundle

    now = datetime.now(UTC)
    memories, reverse = {}, {}
    for i in range(100):
        mid = str(i)
        memories[mid] = StoredMemory(
            memory_id=mid,
            layer="m1",
            text=f"版本 {i}",
            state="active" if i == 99 else "superseded",
            fact_key="profile.residence.city",
            sources=[],
            recorded_at=now,
            created_at=now,
            updated_at=now,
            request_id=mid,
            supersedes=[str(i - 1)] if i else [],
        )
        if i:
            reverse[str(i - 1)] = [mid]
    assert relation_bundle("0", memories, reverse) == ["0", "99"]
    assert relation_bundle("99", memories, reverse) == ["99"]
    assert len(relation_bundle("0", memories, reverse, timeline=True)) == 100


async def test_bm25_failure_uses_vectors_and_reindex_rebuilds_keywords(memory):
    service, _ = memory
    await service.flush(service.submit(event("我长期住在北京")))
    with service.storage.connection() as conn:
        conn.execute("DROP TABLE memory_fts")
    result = await service.recall(query("我住在哪里"))
    assert result.m1 and "BM25_RECALL_UNAVAILABLE" in result.degradations
    await service.reindex()
    await service.flush()
    assert service.storage.keyword_ranks("北京")["m1"]


async def test_both_search_branches_unavailable_is_not_empty(memory):
    service, _ = memory
    await service.flush(service.submit(event("我长期住在北京")))
    with service.storage.connection() as conn:
        conn.execute("DROP TABLE memory_fts")
    service.embeddings.fail = True
    result = await service.recall(query("我住在哪里"))
    assert result.status == "unavailable"


async def test_failed_derivation_is_retried_after_restart_with_fixed_backend(tmp_path):
    model = MemoryModel()
    model.fail_extract = True
    first = ContextMemory(root_dir=tmp_path, model=model, embeddings=LocalEmbeddings())
    await first.start()
    receipt = first.submit(event("我住在北京"))
    with pytest.raises(MemoryFlushError):
        await first.flush(receipt)
    await first.close()
    second = ContextMemory(root_dir=tmp_path, model=MemoryModel(), embeddings=LocalEmbeddings())
    await second.start()
    try:
        await second.flush(receipt)
        assert (await second.write_status(receipt)).index == "indexed"
        assert (await second.recall(query("我住在哪里"))).m1
    finally:
        await second.close()


async def test_known_at_filters_future_raw_history_and_details(memory):
    service, model = memory
    past = datetime.now(UTC) - timedelta(days=2)
    async with service.foreground():
        service.submit(event("后来才生成的 /tmp/future.html"))
        await service._queue.join()
        model.time_mode, model.at, model.kind = "known_at", past, "detail"
        model.dependency, model.history_status = "needed", "selected"
        result = await service.recall(query("当时提到的页面是哪一个"))
        assert result.history.messages == [] and result.details == []
        assert result.snapshot_revision == 0
        assert result.history.status == "unavailable"


async def test_rerank_fallback_does_not_use_only_generic_shared_words(memory):
    service, model = memory
    await service.flush(service.submit(event("我们需要研究这个问题")))
    model.fail_rank = True
    result = await service.recall(query("我们需要研究那个问题"))
    # Shared substantive topic words can be useful; ordinary connective words alone cannot.
    generic = await service.recall(query("我们需要了解太阳的温度"))
    assert generic.m1 == [] and generic.m2 == []
    assert "RERANK_FAILED_FUSION_ORDER" in result.degradations
