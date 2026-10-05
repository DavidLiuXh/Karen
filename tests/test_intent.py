import asyncio
from datetime import UTC, datetime

import pytest
from dynamic_graph import GoalSpec, ModelCallError
from intent_helpers import TaskIntentModel
from pydantic import ValidationError
from tzlocal import get_localzone_name

from karen import IntentRecognizer, IntentSession


def clarify(question="给谁写邮件？"):
    return {"decision": {"outcome": "needs_clarification", "questions": [question]}}


def ready(**overrides):
    return {
        "decision": {
            "outcome": "ready",
            "goal": {
                "objective": "给客户写一封中文进度邮件",
                "success_criteria": ["邮件说明当前进度", "邮件正文使用中文"],
                "constraints": ["不发送邮件，仅输出草稿"],
                "inputs": {"recipient": "客户", "progress": "设计已完成"},
                **overrides,
            },
        }
    }


async def test_clear_request_produces_engine_goal_without_clarification():
    model = TaskIntentModel([ready()])
    session = IntentSession(request_id="request-1", user_context={"language": "zh-CN"})
    updated = await IntentRecognizer(model).advance(session, "给客户写中文进度邮件，设计已完成")
    assert isinstance(updated.goal, GoalSpec)
    assert updated.goal.request_id == "request-1"
    assert updated.goal.objective == "给客户写一封中文进度邮件"
    assert [c.description for c in updated.goal.success_criteria] == [
        "邮件说明当前进度",
        "邮件正文使用中文",
    ]
    assert updated.goal.inputs == {"recipient": "客户", "progress": "设计已完成"}
    assert updated.goal.context["constraints"] == ["不发送邮件，仅输出草稿"]
    assert updated.goal.context["user_context"] == {"language": "zh-CN"}
    assert updated.goal.context["timezone"] == session.timezone
    assert updated.questions == ()
    assert session.messages == () and session.goal is None
    assert len(model.assessments) == 1


async def test_selected_supporting_facts_reach_execution_as_internal_context():
    facts = ["用户更换了部件。", "用户同时改变了记录方式。"]
    model = TaskIntentModel(
        [
            ready(
                objective="解释观测结果的变化",
                success_criteria=["区分实际变化与记录方式变化"],
                inputs={"observation": "结果改善"},
                supporting_facts=facts,
            )
        ]
    )
    result = await IntentRecognizer(model).advance(
        IntentSession(), "我更换了部件并改变记录方式，解释最近观测到的变化"
    )
    assert result.goal.context["supporting_facts"] == facts
    assert result.goal.inputs == {"observation": "结果改善"}
    assert "supporting_facts" not in result.goal.inputs


def test_default_timezone_uses_local_machine_configuration():
    assert IntentSession().timezone == get_localzone_name()


@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/New_York", "UTC"])
async def test_explicit_user_timezone_reaches_model_clarification_and_goal(timezone):
    model = TaskIntentModel([clarify(), ready()])
    recognizer = IntentRecognizer(model)
    session = await recognizer.advance(IntentSession(timezone=timezone), "帮我写邮件")
    assert session.timezone == timezone
    assert model.assessments[0].input_data["timezone"] == timezone
    final = await recognizer.advance(session, "给客户写中文进度邮件，设计已完成")
    assert model.assessments[1].input_data["timezone"] == timezone
    assert final.goal.context["timezone"] == timezone


@pytest.mark.parametrize(
    "instant,zone,today,tomorrow",
    [
        ("2026-10-03T12:54:02+00:00", "Asia/Shanghai", "2026-10-03", "2026-10-04"),
        ("2026-10-03T16:30:00+00:00", "Asia/Shanghai", "2026-10-04", "2026-10-05"),
        ("2026-10-03T00:30:00+00:00", "America/New_York", "2026-10-02", "2026-10-03"),
        ("2026-12-31T12:00:00+00:00", "UTC", "2026-12-31", "2027-01-01"),
        ("2028-02-28T12:00:00+00:00", "UTC", "2028-02-28", "2028-02-29"),
        ("2026-03-08T04:30:00+00:00", "America/New_York", "2026-03-07", "2026-03-08"),
    ],
)
async def test_calendar_dates_use_request_time_and_user_timezone(instant, zone, today, tomorrow):
    model = TaskIntentModel([ready()])
    original = IntentSession(timezone=zone, reference_time_utc=datetime.fromisoformat(instant))
    final = await IntentRecognizer(model).advance(original, "今天北京傍晚大风，明天是否还有大风？")
    clock = model.assessments[0].input_data["time_context"]
    assert clock["reference_time_utc"] == instant
    assert clock["local_date"] == today
    assert clock["relative_dates"]["today"] == today
    assert clock["relative_dates"]["tomorrow"] == tomorrow
    assert clock["timezone"] == zone
    assert final.goal.context["time_context"] == clock
    assert (
        final.goal.context["conversation"][0]["content"] == "今天北京傍晚大风，明天是否还有大风？"
    )


async def test_first_input_anchors_time_and_clarification_keeps_it_across_midnight(monkeypatch):
    from karen.intent import recognizer

    class Clock(datetime):
        instant = datetime(2026, 10, 3, 15, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(recognizer, "datetime", Clock)
    model = TaskIntentModel([clarify("你想查询哪里？"), ready()])
    original = IntentSession(timezone="Asia/Shanghai")
    assert original.reference_time_utc is None
    intent = IntentRecognizer(model)
    pending = await intent.advance(original, "明天还有大风吗？")
    Clock.instant = datetime(2026, 10, 3, 16, 1, tzinfo=UTC)
    final = await intent.advance(pending, "北京")
    assert original.reference_time_utc is None
    assert final.reference_time_utc == pending.reference_time_utc
    assert (
        model.assessments[1].input_data["time_context"]
        == model.assessments[0].input_data["time_context"]
    )
    assert final.goal.context["time_context"]["relative_dates"]["tomorrow"] == "2026-10-04"


def test_time_reference_rejects_naive_datetime_and_normalizes_offset():
    with pytest.raises(ValidationError, match="timezone-aware"):
        IntentSession(reference_time_utc=datetime(2026, 10, 3))
    session = IntentSession(reference_time_utc=datetime.fromisoformat("2026-10-03T20:00:00+08:00"))
    assert session.reference_time_utc == datetime(2026, 10, 3, 12, tzinfo=UTC)


@pytest.mark.parametrize("timezone", ["", "Not/A_Timezone", "UTC+08:00"])
def test_invalid_timezone_is_rejected(timezone):
    with pytest.raises(ValidationError):
        IntentSession(timezone=timezone)


async def test_multiple_clarifications_keep_full_conversation_until_ready():
    model = TaskIntentModel([clarify(), clarify("目前进度是什么？"), ready()])
    recognizer = IntentRecognizer(model)
    first = await recognizer.advance(IntentSession(), "帮我写封邮件")
    assert first.goal is None and first.questions == ("给谁写邮件？",)
    second = await recognizer.advance(first, "给客户，用中文，先不要发送")
    assert second.goal is None and second.questions == ("目前进度是什么？",)
    final = await recognizer.advance(second, "设计已完成")
    assert final.goal is not None and final.questions == ()
    assert [m["content"] for m in model.assessments[-1].input_data["messages"]] == [
        "帮我写封邮件",
        "给谁写邮件？",
        "给客户，用中文，先不要发送",
        "目前进度是什么？",
        "设计已完成",
    ]
    assert final.goal.context["conversation"] == model.assessments[-1].input_data["messages"]
    assert first.messages[-1].content == "给谁写邮件？"


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": {"outcome": "needs_clarification", "questions": []}},
        clarify("   "),
        ready(objective=" "),
        ready(success_criteria=[]),
        ready(success_criteria=[" "]),
        ready(request_id="model-created-id"),
        {"decision": {"outcome": "unknown"}},
    ],
)
async def test_invalid_model_response_does_not_change_session(payload):
    session = IntentSession()
    with pytest.raises(ValidationError):
        await IntentRecognizer(TaskIntentModel([payload, payload])).advance(session, "写邮件")
    assert session.messages == () and session.goal is None


@pytest.mark.parametrize("text", ["", " \n "])
async def test_blank_input_is_rejected_before_model_call(text):
    model = TaskIntentModel()
    with pytest.raises(ValidationError):
        await IntentRecognizer(model).advance(IntentSession(), text)
    assert model.assessments == []


async def test_provider_failure_propagates_and_original_session_can_be_retried():
    error = ModelCallError("MODEL_UNAVAILABLE", "provider unavailable")
    recognizer = IntentRecognizer(TaskIntentModel([error, ready()]))
    session = IntentSession()
    with pytest.raises(ModelCallError) as raised:
        await recognizer.advance(session, "写邮件")
    assert raised.value is error
    final = await recognizer.advance(session, "给客户写中文进度邮件，设计已完成")
    assert len(final.messages) == 1


async def test_completed_intent_session_cannot_execute_a_second_task():
    model = TaskIntentModel([ready()])
    recognizer = IntentRecognizer(model)
    session = await recognizer.advance(IntentSession(), "写邮件")
    with pytest.raises(ValueError, match="already has a goal"):
        await recognizer.advance(session, "再写一封")
    assert len(model.assessments) == 1


async def test_session_context_is_isolated_between_turns():
    original = IntentSession(user_context={"preferences": {"language": "中文"}})
    updated = await IntentRecognizer(TaskIntentModel([ready()])).advance(original, "写邮件")
    updated.goal.context["user_context"]["preferences"]["language"] = "英语"
    assert original.user_context["preferences"]["language"] == "中文"


async def test_custom_output_schema_is_validated_by_engine_contract():
    schema = {
        "type": "object",
        "properties": {"draft": {"type": "string"}},
        "required": ["draft"],
        "additionalProperties": False,
    }
    session = await IntentRecognizer(TaskIntentModel([ready(output_schema=schema)])).advance(
        IntentSession(), "输出 draft 字段"
    )
    assert session.goal.output_schema.document() == schema
    invalid = ready(output_schema={"type": "array"})
    model = TaskIntentModel([invalid, invalid])
    with pytest.raises(ValidationError):
        await IntentRecognizer(model).advance(IntentSession(), "写邮件")
    assert len(model.assessments) == 2


async def test_cancellation_during_assessment_propagates():
    entered = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            entered.set()
            await asyncio.Event().wait()

    session = IntentSession()
    task = asyncio.create_task(IntentRecognizer(WaitingModel()).advance(session, "写邮件"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.messages == ()


async def test_misplaced_model_field_is_repaired_without_losing_clarification():
    invalid = ready()
    invalid["decision"]["goal"]["reason"] = "wrong level"
    model = TaskIntentModel([clarify(), invalid, ready()])
    recognizer = IntentRecognizer(model)
    pending = await recognizer.advance(IntentSession(), "写邮件")
    final = await recognizer.advance(pending, "给客户，设计已完成，中文草稿")
    assert final.goal is not None
    assert final.goal.inputs["progress"] == "设计已完成"
    assert final.goal.context["conversation"][-1]["content"] == "给客户，设计已完成，中文草稿"
    repair = model.assessments[-1].input_data
    assert repair["original_input"]["messages"][-1]["content"] == "给客户，设计已完成，中文草稿"
    assert repair["validation_errors"][0]["type"] == "extra_forbidden"
    assert pending.goal is None


async def test_schema_repair_is_bounded_and_does_not_retry_transport_auth_failures():
    bad = ready(success_criteria=[])
    model = TaskIntentModel([bad, bad, ready()])
    with pytest.raises(ValidationError):
        await IntentRecognizer(model).advance(IntentSession(), "写邮件")
    assert len(model.assessments) == 2
    auth = ModelCallError("MODEL_AUTH_FAILED", "credentials", retryable=False)
    model = TaskIntentModel([auth, ready()])
    with pytest.raises(ModelCallError) as caught:
        await IntentRecognizer(model).advance(IntentSession(), "写邮件")
    assert caught.value is auth and len(model.assessments) == 1


async def test_unsupported_goal_inputs_are_repaired_at_model_boundary():
    model = TaskIntentModel([ready(inputs={"unknown": None}), ready(inputs={})])
    result = await IntentRecognizer(model).advance(IntentSession(), "写一个不依赖未提供数据的草稿")
    assert result.goal.inputs == {}
    assert len(model.assessments) == 2
    assert model.assessments[-1].input_data["previous_response"]["decision"]["goal"]["inputs"] == {
        "unknown": None
    }
    assert "Null input" in model.assessments[-1].input_data["validation_errors"][0]["message"]


async def test_explicit_nullable_input_schema_is_preserved_in_goal():
    schema = {
        "type": "object",
        "properties": {"value": {"type": ["integer", "null"]}},
        "required": ["value"],
        "additionalProperties": False,
    }
    model = TaskIntentModel([ready(inputs={"value": None}, input_schema=schema)])
    result = await IntentRecognizer(model).advance(IntentSession(), "保留输入中的空值")
    assert result.goal.inputs == {"value": None}
    assert result.goal.input_schema.document() == schema
    assert len(model.assessments) == 1
