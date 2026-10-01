import asyncio

import pytest
from dynamic_graph import FakeModelClient, GoalSpec, ModelCallError
from pydantic import ValidationError

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
    model = FakeModelClient([ready()])
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
    assert updated.questions == ()
    assert session.messages == () and session.goal is None
    assert len(model.requests) == 1


async def test_multiple_clarifications_keep_full_conversation_until_ready():
    model = FakeModelClient([clarify(), clarify("目前进度是什么？"), ready()])
    recognizer = IntentRecognizer(model)
    first = await recognizer.advance(IntentSession(), "帮我写封邮件")
    assert first.goal is None and first.questions == ("给谁写邮件？",)
    second = await recognizer.advance(first, "给客户，用中文，先不要发送")
    assert second.goal is None and second.questions == ("目前进度是什么？",)
    final = await recognizer.advance(second, "设计已完成")
    assert final.goal is not None and final.questions == ()
    assert [m["content"] for m in model.requests[-1].input_data["messages"]] == [
        "帮我写封邮件",
        "给谁写邮件？",
        "给客户，用中文，先不要发送",
        "目前进度是什么？",
        "设计已完成",
    ]
    assert final.goal.context["conversation"] == model.requests[-1].input_data["messages"]
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
        await IntentRecognizer(FakeModelClient([payload])).advance(session, "写邮件")
    assert session.messages == () and session.goal is None


@pytest.mark.parametrize("text", ["", " \n "])
async def test_blank_input_is_rejected_before_model_call(text):
    model = FakeModelClient()
    with pytest.raises(ValidationError):
        await IntentRecognizer(model).advance(IntentSession(), text)
    assert model.requests == []


async def test_provider_failure_propagates_and_original_session_can_be_retried():
    error = ModelCallError("MODEL_UNAVAILABLE", "provider unavailable")
    recognizer = IntentRecognizer(FakeModelClient([error, ready()]))
    session = IntentSession()
    with pytest.raises(ModelCallError) as raised:
        await recognizer.advance(session, "写邮件")
    assert raised.value is error
    final = await recognizer.advance(session, "给客户写中文进度邮件，设计已完成")
    assert len(final.messages) == 1


async def test_completed_intent_session_cannot_execute_a_second_task():
    model = FakeModelClient([ready()])
    recognizer = IntentRecognizer(model)
    session = await recognizer.advance(IntentSession(), "写邮件")
    with pytest.raises(ValueError, match="already has a goal"):
        await recognizer.advance(session, "再写一封")
    assert len(model.requests) == 1


async def test_session_context_is_isolated_between_turns():
    original = IntentSession(user_context={"preferences": {"language": "中文"}})
    updated = await IntentRecognizer(FakeModelClient([ready()])).advance(original, "写邮件")
    updated.goal.context["user_context"]["preferences"]["language"] = "英语"
    assert original.user_context["preferences"]["language"] == "中文"


async def test_custom_output_schema_is_validated_by_engine_contract():
    schema = {
        "type": "object",
        "properties": {"draft": {"type": "string"}},
        "required": ["draft"],
        "additionalProperties": False,
    }
    session = await IntentRecognizer(FakeModelClient([ready(output_schema=schema)])).advance(
        IntentSession(), "输出 draft 字段"
    )
    assert session.goal.output_schema.document() == schema
    with pytest.raises(ValueError):
        await IntentRecognizer(FakeModelClient([ready(output_schema={"type": "array"})])).advance(
            IntentSession(), "写邮件"
        )


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
