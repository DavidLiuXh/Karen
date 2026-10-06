"""Clarification state contracts: partial answers never release unfinished work."""

from datetime import UTC, datetime

import pytest
from dynamic_graph import FakeModelClient, ModelCallError
from test_input_routing import routing
from test_intent import ready

from karen import IntentRecognizer, IntentSession


def gap(text, question_id=""):
    return {"text": text, "question_id": question_id, "kind": "missing_requirement",
            "resolution_source": "user", "blocking_reason": "缺口影响实际交付。"}


def update(question_id, status="pending", quote=""):
    return {"question_id": question_id, "status": status,
            "evidence_quote": quote, "reason": "按用户本次回答判断该缺口。"}


def clarity(*questions, updates=()):
    return {"references": [], "known_referents": {}, "selection_criteria": [],
            "questions": list(questions), "question_updates": list(updates),
            "reason": "只询问尚未解决的必要缺口。"}


async def test_partial_answer_retains_unmentioned_gap_until_later_reply():
    model = FakeModelClient([
        routing(), clarity(gap("邮件给谁？"), gap("需要说明什么进度？")),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[update("q1", "answered", "给客户"), update("q2")]),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[update("q2", "answered", "设计已完成")]), ready(),
    ])
    recognizer = IntentRecognizer(model)
    original = IntentSession(reference_time_utc=datetime(2026, 10, 4, tzinfo=UTC))
    first = await recognizer.advance(original, "帮我写进度邮件，仅输出草稿")
    second = await recognizer.advance(first, "给客户")
    assert second.questions == ("需要说明什么进度？",)
    assert second.goal is None and not any(r.role == "intent" for r in model.requests)
    assert [item.status for item in second.clarification_items] == ["answered", "pending"]
    assert second.clarification_items[0].answer_message_index == 2
    assert first.clarification_items[0].status == "pending"
    final = await recognizer.advance(second, "设计已完成")
    assert final.goal and not final.questions
    assert final.request_id == first.request_id == original.request_id
    assert final.reference_time_utc == original.reference_time_utc
    assert [item.status for item in final.clarification_items] == ["answered", "answered"]
    assert final.goal.context["conversation"][0]["content"] == "帮我写进度邮件，仅输出草稿"
    assert model.requests[-1].input_data["clarification_items"][0]["evidence_quote"] == "给客户"


async def test_question_rewording_keeps_identity_and_one_pending_item():
    model = FakeModelClient([
        routing(), clarity(gap("给谁写？")),
        routing(types=["task_control"], relation="continue"),
        clarity(gap("请提供收件人。", "q1"), updates=[update("q1")]),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写封邮件")
    second = await intent.advance(first, "还没想好")
    assert second.questions == ("请提供收件人。",)
    assert len(second.clarification_items) == 1
    assert second.clarification_items[0].question_id == first.clarification_items[0].question_id
    assert second.clarification_items[0].asked_after == first.clarification_items[0].asked_after


async def test_question_budget_preserves_unshown_gaps():
    model = FakeModelClient([
        routing(), clarity(*(gap(f"必要问题{i}？") for i in range(1, 5))),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[*(update(f"q{i}", "answered", f"答案{i}") for i in range(1, 4)),
                         update("q4")]),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "请完成这项工作")
    assert len(first.questions) == 3 and len(first.clarification_items) == 4
    assert "必要问题4" not in first.messages[-1].content
    second = await intent.advance(first, "答案1、答案2、答案3")
    assert second.questions == ("必要问题4？",) and second.goal is None
    assert not any(r.role == "intent" for r in model.requests)


@pytest.mark.parametrize("invalid_update", [
    [],
    [update("q9")],
    [update("q1"), update("q1")],
    [update("q1", "answered", "给谁写？")],  # Assistant question is not an answer.
    [update("q1", "answered", "写邮件给客户")],  # Before the question was asked.
    [update("q1", "withdrawn", "取消了")],  # Never said by the user.
    [update("q1", "pending", "仍然不知道")],
])
async def test_invalid_question_updates_repair_once_without_mutating_session(invalid_update):
    model = FakeModelClient([
        routing(), clarity(gap("给谁写？")),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=invalid_update), clarity(updates=invalid_update),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写邮件给客户")
    with pytest.raises(ModelCallError) as error:
        await intent.advance(first, "仍然不知道")
    assert error.value.code == "MODEL_RESPONSE_INVALID"
    assert first.questions == ("给谁写？",) and first.clarification_items[0].status == "pending"
    assert first.messages[-1].role == "assistant"
    assert not any(r.role == "intent" for r in model.requests)
    assert len([r for r in model.requests if r.role == "intent_clarity"]) == 3


async def test_new_task_discards_old_question_state_and_answers():
    model = FakeModelClient([
        routing(), clarity(gap("给谁写？")),
        routing(relation="new"), clarity(), ready(objective="查询上海明天天气"),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写封邮件")
    second = await intent.advance(first, "改查上海明天天气")
    assert second.goal and second.request_id != first.request_id
    assert second.clarification_items == ()
    assert len(second.messages) == 1
    assert model.requests[-1].input_data["clarification_items"] == []
    assert model.requests[2].input_data["pending_task"]["clarification_items"][0]["question_id"] == "q1"


async def test_continue_cannot_bypass_clarity_by_being_routed_as_information():
    model = FakeModelClient([
        routing(), clarity(gap("给谁写？")),
        routing("respond", types=["information"], relation="continue"),
        clarity(updates=[update("q1")]),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写封邮件")
    second = await intent.advance(first, "我还不清楚收件人")
    assert second.questions and second.reply is None and second.goal is None
    assert model.requests[-1].role == "intent_clarity"


async def test_partial_answer_never_calls_execution_engine(tmp_path):
    from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings

    from karen import Karen

    model = FakeModelClient([
        routing(), clarity(gap("收件人是谁？"), gap("邮件内容是什么？")),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[update("q1", "answered", "给客户"), update("q2")]),
    ])
    executor = FakeModelClient()
    agent = Karen(
        intent=IntentRecognizer(model),
        engine=DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"),
                                 models=ModelBindings(executor, executor)),
    )
    first = await agent.advance(IntentSession(), "写邮件")
    second = await agent.advance(first.session, "给客户")
    assert second.session.questions == ("邮件内容是什么？",)
    assert second.result is None and second.response is None and executor.requests == []
    assert not (tmp_path / "runs").exists()


async def test_overclarification_can_be_retracted_without_inventing_user_consent():
    model = FakeModelClient([
        routing(), clarity(gap("项目名称是什么？"), gap("项目进度是什么？")),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[update("q1", "not_required", "只起草，不要发送"),
                         update("q2", "answered", "设计已完成")]), ready(),
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "给客户起草进度邮件，只起草，不要发送")
    final = await intent.advance(first, "设计已完成")
    assert final.goal and not final.questions
    item = final.clarification_items[0]
    assert item.status == "not_required" and item.evidence_quote == "只起草，不要发送"
    assert item.answer_message_index == 0
    assert final.clarification_items[1].status == "answered"
    assert first.clarification_items[0].status == "pending"


async def test_unnecessary_question_retraction_still_needs_user_evidence():
    invalid = clarity(updates=[update("q1", "not_required", "项目名称是什么？")])
    model = FakeModelClient([
        routing(), clarity(gap("项目名称是什么？")),
        routing(types=["task_control"], relation="continue"), invalid, invalid,
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "起草邮件")
    with pytest.raises(ModelCallError) as error:
        await intent.advance(first, "先不要写")
    assert error.value.code == "MODEL_RESPONSE_INVALID"
    assert first.clarification_items[0].status == "pending"


@pytest.mark.parametrize("repeat", [
    update("q1", "answered", "给客户"),
    update("q1", "pending"),
    update("q1", "answered", "给同事"),
])
async def test_repeated_confirmations_are_idempotent_but_cannot_rewrite_answers(repeat):
    final_check = clarity(updates=[repeat, update("q2", "answered", "设计已完成")])
    model = FakeModelClient([
        routing(), clarity(gap("给谁写？"), gap("进度是什么？")),
        routing(types=["task_control"], relation="continue"),
        clarity(updates=[update("q1", "answered", "给客户"), update("q2")]),
        routing(types=["task_control"], relation="continue"),
        final_check, ready() if repeat["status"] == "answered" and repeat["evidence_quote"] == "给客户" else final_check,
    ])
    intent = IntentRecognizer(model)
    first = await intent.advance(IntentSession(), "写邮件")
    second = await intent.advance(first, "给客户")
    if repeat["status"] == "answered" and repeat["evidence_quote"] == "给客户":
        final = await intent.advance(second, "设计已完成，给同事是另外一封邮件才用的对象")
        assert final.goal and final.clarification_items[0] == second.clarification_items[0]
    else:
        with pytest.raises(ModelCallError) as error:
            await intent.advance(second, "设计已完成，给同事是另外一封邮件才用的对象")
        assert error.value.code == "MODEL_RESPONSE_INVALID"
        assert second.clarification_items[0].evidence_quote == "给客户"
