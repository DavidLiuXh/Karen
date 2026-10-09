"""Behavioral boundaries of the versioned clarification grader and reports."""

import json
from copy import deepcopy

import pytest
from dynamic_graph import FakeModelClient

from karen.evaluation.clarification import assess_steps, grade_quality, summarize_dimensions
from karen.evaluation.review import calibrate, freeze_v2, regrade


def case():
    return {"id": "private-target", "suite": "clamber", "split": "development", "category": "target",
            "context": "There are two files named report.txt.", "history": [],
            "steps": [{"text": "Delete that report.", "expected": {"outcome": "clarification"},
                       "policy_expectation": {"outcome": "clarification",
                                              "necessary_information": ["Unique file path"],
                                              "reference": "Which file path should be deleted?",
                                              "rationale": "Deletion requires an identified target."}}],
            "gold": {"clarifying_question": "Which report?"}}


def grade(verdict="pass", repeated=False):
    return {"verdict": verdict, "reason": "Addresses the remaining gap.",
            "repeats_answered_information": repeated}


async def test_v2_grader_receives_expected_action_and_full_chain_without_changing_public_labels():
    c = case()
    c["steps"].append({"text": "The one on my desktop.", "expected": {"outcome": "complete"},
                       "policy_expectation": {"outcome": "complete", "necessary_information": [],
                                              "reference": "Only the desktop file is selected."}})
    actual = [{"outcome": "clarification", "answer": "Which report?"},
              {"outcome": "goal", "answer": "", "goal": {"objective": "Delete only the desktop report."}}]
    model = FakeModelClient([grade()])
    snapshot = deepcopy(c)
    result = await grade_quality(model, c, actual, policy=True)
    data = model.requests[0].input_data
    assert result["dimension"] == "goal_quality"
    assert data["expected_action"]["outcome"] == "complete"
    assert [m["role"] for m in data["conversation"]] == ["user", "assistant", "user"]
    assert data["provided_context"] == c["context"] and c == snapshot


async def test_action_mismatch_does_not_become_failed_question_quality():
    model = FakeModelClient()
    dimensions = await assess_steps(model, case(), [{"outcome": "goal", "goal": {}, "answer": ""}])
    assert dimensions[0]["public_action"]["matched"] is False
    assert dimensions[0]["product_action"]["matched"] is False
    assert dimensions[0]["public_quality"] is None and model.requests == []


async def test_product_policy_and_public_expectation_can_disagree_without_overwriting_either():
    c = case()
    c["steps"][0]["expected"]["outcome"] = "complete"
    model = FakeModelClient([grade()])
    dims = await assess_steps(model, c, [{"outcome": "clarification", "answer": "Which path?"}])
    assert dims[0]["public_action"]["matched"] is False
    assert dims[0]["product_action"]["matched"] is True
    assert dims[0]["public_quality"] is None
    assert dims[0]["product_quality"]["verdict"] == "pass"
    assert model.requests[0].input_data["reference"] == "Which file path should be deleted?"


async def test_inconsistent_reference_is_disputed_and_errors_are_not_passes():
    model = FakeModelClient([grade("disputed"), ValueError("invalid response")])
    dims = await assess_steps(model, case(), [{"outcome": "clarification", "answer": "Which path?"}])
    assert dims[0]["public_quality"]["verdict"] == "disputed"
    assert dims[0]["product_quality"]["verdict"] == "error"


async def test_invalid_or_contradictory_judge_response_is_rejected():
    model = FakeModelClient([grade("pass", repeated=True)])
    with pytest.raises(ValueError, match="Invalid protocol"):
        await grade_quality(model, case(), [{"outcome": "clarification", "answer": "Which path?"}])


def test_dimension_reporting_keeps_errors_and_unannotated_cases_explicit():
    expected = [{"expected": {"outcome": "clarification"}}]
    results = [
        {"suite": "clamber", "expected_steps": expected, "steps": [], "status": "error"},
        {"suite": "clamber", "expected_steps": expected,
         "steps": [{"outcome": "clarification"}], "status": "failed"},
        {"suite": "clamber", "expected_steps": [{"expected": {"outcome": "complete"}}],
         "steps": [{"outcome": "clarification"}], "status": "failed"},
    ]
    summary = summarize_dimensions(results)
    assert summary["public"]["action_accuracy"] == 1 / 3
    assert summary["public"]["clarification_recall"] == 0.5
    assert summary["public"]["overclarification_rate"] == 1
    assert summary["product"]["annotated_turns"] == 0
    assert summary["product"]["action_accuracy"] is None
    assert summary["multi_turn_completion"]["rate"] is None


def test_freezing_v2_preserves_original_labels_and_refuses_overwrite(tmp_path):
    source = tmp_path / "v1.json"
    original = {"protocol_version": "1", "cases": [case()]}
    source.write_text(json.dumps(original))
    before = source.read_bytes()
    destination = tmp_path / "v2.json"
    upgraded = freeze_v2(source, destination)
    assert source.read_bytes() == before and upgraded["cases"] == original["cases"]
    assert upgraded["protocol_version"] == "2"
    with pytest.raises(FileExistsError):
        freeze_v2(source, destination)


async def test_calibration_is_not_product_performance_or_human_reward_approval(tmp_path):
    path = tmp_path / "calibration.json"
    fixture = {"protocol_version": "2", "provenance": {"human_review": "pending"}, "cases": [
        {"id": "calibration-1", "case": case(), "actual_steps": [
            {"outcome": "clarification", "answer": "Which path?"}],
         "expected_verdict": "pass", "expected_repetition": False}]}
    path.write_text(json.dumps(fixture))
    result = await calibrate(path, tmp_path / "output", client_factory=lambda: FakeModelClient([grade()]))
    assert result["calibrated_on_curated_set"] is True
    assert result["independent_human_review"] is False and result["training_reward_approved"] is False


async def test_regrade_does_not_overwrite_old_results_or_execute_agent(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    c = case()
    c["steps"][0].pop("policy_expectation")
    (source / "manifest.json").write_text(json.dumps({"protocol_version": "1", "cases": [c]}))
    saved = {"id": c["id"], "suite": c["suite"], "split": c["split"],
             "status": "failed", "steps": [{"outcome": "clarification", "answer": "Which path?"}],
             "checks": ["Old grader required a final answer"]}
    original = json.dumps(saved) + "\n"
    (source / "results.jsonl").write_text(original)
    client = FakeModelClient([grade()])
    report = await regrade(source, tmp_path / "new", client_factory=lambda: client)
    assert (source / "results.jsonl").read_text() == original
    assert report["mode"] == "regrade_saved_responses"
    assert report["dimensions"]["public"]["action_accuracy"] == 1
    assert [r.role for r in client.requests] == ["evaluation_quality_v2"]
