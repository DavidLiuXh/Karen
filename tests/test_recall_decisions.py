"""Replay malformed responses and verify recall decisions at the public boundary."""

import asyncio
from datetime import UTC, datetime

import pytest
from dynamic_graph import FakeModelClient, ModelCallError
from pydantic import ValidationError
from test_context import LocalEmbeddings, MemoryModel, event, query

from karen.context import ContextMemory, RecallQuery
from karen.context.contracts import QueryAnalysis
from karen.observability import Observer
from karen.observability.viewer import TraceStore

WEATHER_QUERY = "今天傍晚北京有大风，帮我看一下明天北京的天气情况，特别是是否还有大风"
MISSING_AT_RESPONSE = {
    "search_text": "明天北京的天气情况，特别是是否还有大风",
    "entities": ["北京"],
    "needed_fact_keys": [],
    "time_mode": "effective_at",
    "time_range": {
        "start": "2026-10-04T00:00:00+08:00",
        "end": "2026-10-05T00:00:00+08:00",
    },
    "dialogue_dependency": "none",
    "kind": "relevance",
}


@pytest.mark.parametrize("mode", ["effective_at", "known_at"])
def test_historical_time_reference_is_explicit_and_not_inferred_from_range(mode):
    with pytest.raises(ValidationError) as caught:
        QueryAnalysis.model_validate({**MISSING_AT_RESPONSE, "time_mode": mode})
    assert caught.value.errors()[0]["type"] == "time_reference_required"
    assert caught.value.errors()[0]["ctx"]["time_mode"] == mode
    instant = datetime(2026, 10, 4, tzinfo=UTC)
    valid = QueryAnalysis.model_validate({**MISSING_AT_RESPONSE, "time_mode": mode, "at": instant})
    assert valid.at == instant
    assert valid.time_range.start.isoformat() == "2026-10-04T00:00:00+08:00"


async def test_recorded_missing_at_response_falls_back_with_visible_validation_reason(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    service = ContextMemory(
        root_dir=tmp_path / "context",
        model=FakeModelClient([MISSING_AT_RESPONSE]),
        embeddings=LocalEmbeddings(),
        observer=observer,
    )
    await service.start()
    trace = "a" * 32
    try:
        with observer.span("turn", trace_id=trace, request_id="weather"):
            result = await service.recall(
                RecallQuery(
                    text=WEATHER_QUERY,
                    timezone="Asia/Shanghai",
                    current_time_utc=datetime(2026, 10, 3, 13, 15, tzinfo=UTC),
                    conversation_id="conversation",
                    request_id="weather",
                )
            )
        assert result.status == "degraded"
        assert result.degradations == ["QUERY_ANALYSIS_FAILED"]
        assert result.query_time_basis["mode"] == "unspecified"
        assert result.query_time_basis["at"] is None
        assert result.history.messages == []
    finally:
        await service.close()
        await observer.close()
    detail = TraceStore(observer.root_dir).trace(trace)
    failure = next(e for e in detail["events"] if e["event_type"] == "memory.degraded")
    assert failure["stage"] == "memory.recall.analyze"
    assert failure["status"] == "degraded"
    assert failure["data"]["error_code"] == "MODEL_RESPONSE_VALIDATION_FAILED"
    issue = failure["data"]["validation_errors"][0]
    assert issue["type"] == "time_reference_required"
    assert issue["field"] == "at" and issue["time_mode"] == "effective_at"
    assert "time_range 不能替代 at" in issue["reason"]
    assert TraceStore(observer.root_dir).tasks()["tasks"][0]["degraded"]


@pytest.mark.parametrize(
    "response,expected_error,validation_type",
    [
        (
            {"search_text": "weather", "time_mode": "known_at", "at": "2026-10-03T10:00:00"},
            "MODEL_RESPONSE_VALIDATION_FAILED",
            "at_timezone_required",
        ),
        (
            {"search_text": "", "entities": ["private-input"]},
            "MODEL_RESPONSE_VALIDATION_FAILED",
            "string_too_short",
        ),
        (ModelCallError("MODEL_UNAVAILABLE", "private-input"), "MODEL_UNAVAILABLE", None),
        (TimeoutError("private-input"), "RECALL_TIMEOUT", None),
        (ValueError({"detail": "private-input"}), "ValueError", None),
    ],
)
async def test_degradation_records_safe_specific_causes(
    tmp_path, response, expected_error, validation_type
):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    service = ContextMemory(
        root_dir=tmp_path / "context",
        model=FakeModelClient([response]),
        embeddings=LocalEmbeddings(),
        observer=observer,
    )
    await service.start()
    trace = "b" * 32
    try:
        with observer.span("turn", trace_id=trace):
            result = await service.recall(query("天气"))
        assert result.degradations == ["QUERY_ANALYSIS_FAILED"]
    finally:
        await service.close()
        await observer.close()
    detail = TraceStore(observer.root_dir).trace(trace)
    failure = next(e["data"] for e in detail["events"] if e["event_type"] == "memory.degraded")
    assert failure["error_code"] == expected_error
    if validation_type:
        assert failure["validation_errors"][0]["type"] == validation_type
    assert "private-input" not in str(failure)


class DecisionModel(MemoryModel):
    async def generate(self, request):
        if request.role == "memory_rerank":
            # Preserve the backend decisions through the real retrieval/assembly path.
            self.requests.append(request)
            from dynamic_graph import ModelResponse

            reviewing = "上次" in request.input_data["query"]
            return ModelResponse(
                {
                    "ranking": [
                        {
                            "memory_id": mid,
                            "relevance": "relevant" if reviewing else "irrelevant",
                            "reason": "用于回顾旧澄清"
                            if reviewing
                            else "没有补充当前请求所需的信息",
                        }
                        for mid in request.input_data["primary_ids"]
                    ],
                    "history_status": "none",
                }
            )
        return await super().generate(request)


async def test_redundant_clarification_is_excluded_but_can_be_recalled_for_review(tmp_path):
    model = DecisionModel()
    service = ContextMemory(root_dir=tmp_path, model=model, embeddings=LocalEmbeddings())
    await service.start()
    try:
        receipt = service.submit(
            event(
                "‘北否’指哪个地点？‘明天’是哪一天？",
                kind="assistant_message",
                request="old-weather",
            )
        )
        await service.flush(receipt)
        current = RecallQuery(
            text=WEATHER_QUERY,
            timezone="Asia/Shanghai",
            conversation_id="conversation",
            request_id="new-weather",
            current_task_messages=[{"role": "user", "content": "地点是北京，日期是2026-10-04"}],
        )
        result = await service.recall(current)
        assert result.status == "empty" and result.m2 == []
        ranked_request = next(r for r in model.requests if r.role == "memory_rerank")
        assert ranked_request.input_data["primary_ids"]
        assert ranked_request.input_data["current_request_id"] == "new-weather"
        assert ranked_request.input_data["current_task_messages"] == current.current_task_messages
        assert all(m["request_id"] == "old-weather" for m in ranked_request.input_data["memories"])
        reviewed = await service.recall(query("上次你提出了哪些澄清问题"))
        assert reviewed.status == "ok" and reviewed.m2
        assert any("北否" in hit.memory.text for hit in reviewed.m2)
    finally:
        await service.close()


async def test_rerank_failure_reports_reason_and_preserves_unverified_fusion_order(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    model = MemoryModel()
    service = ContextMemory(
        root_dir=tmp_path / "context", model=model, embeddings=LocalEmbeddings(), observer=observer
    )
    await service.start()
    trace = "c" * 32
    try:
        await service.flush(service.submit(event("我住在北京")))
        model.fail_rank = True
        with observer.span("turn", trace_id=trace):
            result = await service.recall(query("我住在哪里"))
        assert result.m1 and all(
            h.relevance == "unverified" and h.rerank_rank is None for h in result.m1
        )
        assert [h.fusion_rank for h in result.m1] == sorted(h.fusion_rank for h in result.m1)
    finally:
        await service.close()
        await observer.close()
    failures = [
        e
        for e in TraceStore(observer.root_dir).trace(trace)["events"]
        if e["event_type"] == "memory.degraded"
    ]
    assert failures[0]["data"]["code"] == "RERANK_FAILED_FUSION_ORDER"
    assert failures[0]["data"]["error_code"] == "RERANK_OFFLINE"


async def test_cancelled_query_propagates_without_becoming_a_degradation(tmp_path):
    started = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            started.set()
            await asyncio.Event().wait()

    service = ContextMemory(root_dir=tmp_path, model=WaitingModel(), embeddings=LocalEmbeddings())
    await service.start()
    try:
        task = asyncio.create_task(service.recall(query("天气")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await service.close()


async def test_invalid_rerank_ids_are_reported_as_validation_failure(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()

    class InvalidRankingModel(MemoryModel):
        async def generate(self, request):
            if request.role == "memory_rerank":
                from dynamic_graph import ModelResponse

                return ModelResponse(
                    {
                        "ranking": [
                            {
                                "memory_id": "private-input",
                                "relevance": "relevant",
                                "reason": "wrong id",
                            }
                        ]
                    }
                )
            return await super().generate(request)

    service = ContextMemory(
        root_dir=tmp_path / "context",
        model=InvalidRankingModel(),
        embeddings=LocalEmbeddings(),
        observer=observer,
    )
    await service.start()
    trace = "d" * 32
    try:
        await service.flush(service.submit(event("我住在北京")))
        with observer.span("turn", trace_id=trace):
            result = await service.recall(query("我住在哪里"))
        assert result.degradations == ["RERANK_FAILED_FUSION_ORDER"]
        assert result.m1 and all(h.relevance == "unverified" for h in result.m1)
    finally:
        await service.close()
        await observer.close()
    failure = next(
        e["data"]
        for e in TraceStore(observer.root_dir).trace(trace)["events"]
        if e["event_type"] == "memory.degraded"
    )
    assert failure["error_code"] == "INVALID_RERANK_IDS"
    assert "private-input" not in str(failure)
