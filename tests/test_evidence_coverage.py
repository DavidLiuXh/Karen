"""Evidence must survive packing and be audited against the actual deliverable."""

from copy import deepcopy

import pytest
from dynamic_graph import FakeModelClient, ModelCallError

from karen import IntentRecognizer, IntentSession


def memory():
    return {"m2": [{
        "relevance": "relevant", "memory": {
            "memory_id": "practice", "text": "用户用设备录制过语音。",
            "sources": [{"source_role": "user", "quote": "我录制过语音。"}],
        },
    }]}


def reply(answer, coverage=None):
    return {"decision": {"outcome": "reply", "answer": answer},
            "evidence_coverage": coverage or []}


def use(evidence_id="practice", quote="录制语音时", disposition="covered"):
    return {"evidence_id": evidence_id, "disposition": disposition,
            "output_quote": quote, "reason": "结合已有实践给出具体建议。"}


def clear():
    return {"known_referents": {}, "references": [], "selection_criteria": [],
            "questions": [], "reason": "输入清晰"}


@pytest.mark.parametrize("bad", [[], [use("invented")], [use(quote="仅在支持事实中出现")],
                                  [use(), use()]])
async def test_invalid_coverage_is_repaired_before_publishing_reply(bad):
    draft = reply("注意调整参数。")
    corrected = reply("你录制语音时可以降低环境噪声。", [use()])
    model = FakeModelClient([
        {"input_types": ["question"], "handling": "respond", "task_relation": "new",
         "reason": "根据已有记录回应"}, clear(), draft,
        reply(corrected["decision"]["answer"], bad), corrected,
    ])
    session = IntentSession()
    result = await IntentRecognizer(model).advance(
        session, "如何改善这个设备的使用效果？", memory_context=memory(),
    )
    assert result.reply == corrected["decision"]["answer"]
    assert session.reply is None and not session.messages
    repair = model.requests[-1]
    assert repair.role == "intent_response_review"
    assert repair.input_data["validation_errors"]
    original = repair.input_data["original_input"]["original_input"]
    assert original["evidence_to_consider"][0]["evidence_id"] == "practice"
    assert original["memory"] == memory()


async def test_repeated_invalid_coverage_does_not_publish_draft():
    model = FakeModelClient([
        {"input_types": ["question"], "handling": "respond", "task_relation": "new",
         "reason": "根据已有记录回应"}, clear(), reply("草稿"), reply("未覆盖"), reply("未覆盖"),
    ])
    session = IntentSession()
    with pytest.raises(ModelCallError, match="coverage"):
        await IntentRecognizer(model).advance(session, "设备如何改进？", memory_context=memory())
    assert session.reply is None and not session.messages
    assert len([r for r in model.requests if r.role == "intent_response_review"]) == 2


def test_supporting_facts_alone_do_not_prove_goal_coverage():
    from karen.intent.recognizer import Assessment

    draft = {"decision": {"outcome": "ready", "goal": {
        "objective": "改善设备效果", "success_criteria": ["提供建议"],
        "supporting_facts": ["录制语音时"], "evidence_coverage": [use()],
    }}}
    inputs = {"evidence_to_consider": [{"evidence_id": "practice"}]}
    with pytest.raises(ValueError, match="COVERAGE_QUOTE"):
        IntentRecognizer._check_evidence_coverage(inputs, Assessment.model_validate(draft))
    fixed = deepcopy(draft)
    fixed["decision"]["goal"]["success_criteria"] = ["说明录制语音时的改进方法"]
    IntentRecognizer._check_evidence_coverage(inputs, Assessment.model_validate(fixed))


@pytest.mark.parametrize("kind", ["reply", "goal"])
async def test_accepted_review_preserves_draft_and_still_checks_deliverable_coverage(kind):
    if kind == "reply":
        draft = reply("你录制语音时可以降低环境噪声。")
        handling = "respond"
    else:
        draft = {"decision": {"outcome": "ready", "goal": {
            "objective": "改善录制语音时的设备效果", "success_criteria": ["提供具体改进方案"],
            "inputs": {"device": "用户已有设备"},
        }}}
        handling = "assess"
    model = FakeModelClient([
        {"input_types": ["question"], "handling": handling, "task_relation": "new", "reason": "已有记录"},
        clear(), draft,
        {"accepted": True, "evidence_coverage": [use(quote="草稿中不存在的文字")]},
        {"accepted": True, "evidence_coverage": [use()]},
    ])
    original = deepcopy(draft)
    result = await IntentRecognizer(model).advance(IntentSession(), "设备如何改进？", memory_context=memory())
    assert draft == original
    if kind == "reply":
        assert result.reply == draft["decision"]["answer"]
    else:
        assert result.goal.objective == draft["decision"]["goal"]["objective"]
        assert result.goal.inputs == draft["decision"]["goal"]["inputs"]
    repairs = [r for r in model.requests if r.role.endswith("_review")]
    assert len(repairs) == 2
    assert repairs[-1].input_data["validation_errors"] == [{"type": "COVERAGE_QUOTE_NOT_IN_DELIVERABLE"}]
