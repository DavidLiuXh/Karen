"""Protocol 2: independent action agreement and contextual quality grading."""

import json
from collections import Counter
from pathlib import Path

from dynamic_graph import ModelRequest

from ..intent.policy import CLARIFICATION_POLICY_VERSION, TASK_GAP_INSTRUCTION
from .prompts import JUDGE_INSTRUCTION

PROTOCOL_VERSION = "2"

QUALITY_SCHEMA = {
    "type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "disputed"]},
        "reason": {"type": "string", "minLength": 1},
        "repeats_answered_information": {"type": "boolean"},
    }, "required": ["verdict", "reason", "repeats_answered_information"], "additionalProperties": False,
}


def action_agreement(expected, actual):
    outcome = expected.get("outcome")
    if outcome is None:
        return {"expected_ask": None, "actual_ask": None, "matched": None}
    if outcome not in {"clarification", "complete", "goal", "reply", "execution"}:
        raise ValueError("Unknown expected clarification action")
    expected_ask = outcome == "clarification"
    actual_ask = (actual["outcome"] == "clarification" if actual and actual.get("outcome") in {
        "clarification", "goal", "reply", "execution",
    } else None)
    return {"expected_ask": expected_ask, "actual_ask": actual_ask,
            "matched": actual_ask == expected_ask if actual_ask is not None else False}


async def grade_quality(client, case, actual_steps, *, policy=False):
    step = case["steps"][len(actual_steps) - 1]
    expectation = step.get("policy_expectation") if policy else step["expected"]
    if expectation is None:
        return None
    actual = actual_steps[-1]
    agreement = action_agreement(expectation, actual)
    if not agreement["matched"]:
        return None  # An action error is not a question-quality failure.
    if actual["outcome"] == "clarification":
        dimension = "question_quality"
        reference = case.get("gold", {}).get("clarifying_question")
    elif actual["outcome"] == "goal":
        dimension = "goal_quality"
        reference = case.get("gold", {}).get("answer")
    else:
        dimension = "answer_quality"
        reference = case.get("gold", {}).get("answer")
    if policy:
        reference = expectation.get("reference")
    if not reference and not expectation.get("necessary_information") and not policy:
        return None
    messages = []
    for index, turn in enumerate(actual_steps):
        messages.append({"role": "user", "content": case["steps"][index]["text"]})
        if index < len(actual_steps) - 1:
            messages.append({"role": "assistant", "content": turn.get("answer", "")})
    response = await client.generate(ModelRequest(
        role="evaluation_quality_v2", system_instruction=JUDGE_INSTRUCTION,
        task_instruction="Grade only the specified quality dimension using the explicit expectation.",
        input_data={
            "dimension": dimension, "expected_action": expectation,
            "expectation_source": "product_policy" if policy else "public_label",
            "product_policy_version": CLARIFICATION_POLICY_VERSION,
            "product_policy": TASK_GAP_INSTRUCTION if policy else None,
            "provided_context": case.get("context", ""), "conversation": messages,
            "recalled_memory": actual.get("memory"),
            "timezone": case.get("timezone"), "reference_time": case.get("reference_time"),
            "reference": reference,
            "candidate": actual.get("goal") if dimension == "goal_quality" else actual.get("answer", ""),
        }, output_schema=QUALITY_SCHEMA, max_output_tokens=2048, timeout_seconds=90,
    ))
    payload = response.payload
    if (not isinstance(payload, dict) or set(payload) != {"verdict", "reason", "repeats_answered_information"}
            or payload["verdict"] not in {"pass", "fail", "disputed"}
            or not isinstance(payload["reason"], str) or not payload["reason"].strip()
            or type(payload["repeats_answered_information"]) is not bool
            or (payload["verdict"] == "pass" and payload["repeats_answered_information"])):
        raise ValueError("Invalid protocol 2 quality grade")
    return {"dimension": dimension, **payload}


async def assess_steps(client, case, actual_steps):
    """Keep public labels and independently frozen product annotations separate."""
    dimensions = []
    for index, actual in enumerate(actual_steps):
        step = case["steps"][index]
        entry = {"public_action": action_agreement(step["expected"], actual),
                 "product_action": action_agreement(step.get("policy_expectation", {}), actual)}
        for source, policy in (("public_quality", False), ("product_quality", True)):
            try:
                entry[source] = await grade_quality(client, case, actual_steps[:index + 1], policy=policy)
            except Exception as error:
                dimension = ("question_quality" if actual["outcome"] == "clarification" else
                             "goal_quality" if actual["outcome"] == "goal" else "answer_quality")
                entry[source] = {"dimension": dimension, "verdict": "error",
                                 "error_code": getattr(error, "code", type(error).__name__)}
        dimensions.append(entry)
    return dimensions


def summarize_dimensions(results):
    """Errors remain in action denominators; unannotated product cases stay unscored."""
    summary = {}
    for source in ("public", "product"):
        matrix, quality, errors, case_statuses = Counter(), Counter(), Counter(), Counter()
        total = correct = 0
        questions_graded = repeated = 0
        for result in results:
            expected_steps = result.get("expected_steps", [])
            actual_steps = result.get("steps", [])
            dimensions = result.get("dimensions", [])
            agreements = [action_agreement(
                step.get("policy_expectation", {}) if source == "product" else step["expected"],
                actual_steps[index] if index < len(actual_steps) else None,
            ) for index, step in enumerate(expected_steps)]
            annotated = [a for a in agreements if a["expected_ask"] is not None]
            grades = [d[f"{source}_quality"] for d in dimensions if d.get(f"{source}_quality")]
            if not annotated:
                status = "unscored"
            elif any(not a["matched"] for a in annotated) or any(g["verdict"] == "fail" for g in grades):
                status = "failed"
            elif any(g["verdict"] == "error" for g in grades):
                status = "judge_error"
            elif any(g["verdict"] == "disputed" for g in grades):
                status = "disputed"
            else:
                status = "passed"
            case_statuses[status] += 1
            for index, expected in enumerate(expected_steps):
                wanted = expected.get("policy_expectation", {}) if source == "product" else expected["expected"]
                actual = actual_steps[index] if index < len(actual_steps) else None
                agreement = action_agreement(wanted, actual)
                if agreement["expected_ask"] is None:
                    continue
                total += 1
                correct += int(agreement["matched"])
                if agreement["actual_ask"] is None:
                    matrix["error"] += 1
                    errors["missed_ask_error" if agreement["expected_ask"] else "nonask_error"] += 1
                else:
                    matrix[("tp" if agreement["actual_ask"] else "fn") if agreement["expected_ask"]
                           else ("fp" if agreement["actual_ask"] else "tn")] += 1
                if index < len(dimensions):
                    grade = dimensions[index].get(f"{source}_quality")
                    if grade:
                        quality[f"{grade.get('dimension', 'quality')}/{grade['verdict']}"] += 1
                        if grade.get("dimension") == "question_quality" and grade["verdict"] != "error":
                            questions_graded += 1
                            repeated += int(grade.get("repeats_answered_information", False))
        required = matrix["tp"] + matrix["fn"] + errors["missed_ask_error"]
        not_required = matrix["fp"] + matrix["tn"] + errors["nonask_error"]
        summary[source] = {
            "annotated_turns": total, "correct_actions": correct,
            "action_accuracy": correct / total if total else None,
            "confusion": dict(matrix),
            "error_breakdown": dict(errors),
            "case_status_counts": dict(case_statuses),
            "clarification_recall": matrix["tp"] / required if required else None,
            "overclarification_rate": matrix["fp"] / not_required if not_required else None,
            "quality_counts": dict(quality),
            "repeated_answered_question_rate": repeated / questions_graded if questions_graded else None,
            "questions_graded_for_repetition": questions_graded,
        }
    multi = [r for r in results if len(r.get("expected_steps", [])) > 1
             and r["suite"] != "clamber" and r.get("adapter") != "intent"]
    completed = sum(r.get("status") == "passed" and len(r.get("steps", [])) == len(r["expected_steps"])
                    and r["steps"][-1]["outcome"] in {"reply", "execution"} for r in multi)
    summary["multi_turn_completion"] = {"completed": completed, "total": len(multi),
                                        "rate": completed / len(multi) if multi else None}
    return summary


def trace_origins(directory):
    origins = []
    for path in sorted((Path(directory) / "observability" / "traces").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            data = event.get("data", {})
            if event.get("event_type") == "model.response":
                payload = data.get("payload", {})
                if isinstance(payload, dict) and payload.get("decision", {}).get("outcome") == "needs_clarification":
                    origins.append({"origin": event.get("model_role"), "event_id": event["event_id"]})
            elif event.get("event_type") == "intent.clarity_resolution" and data.get("user_questions"):
                origins.append({"origin": "intent_clarity", "event_id": event["event_id"]})
            elif event.get("event_type") == "intent.clarification_decision":
                origins.append({"origin": data.get("origin"), "action": data.get("action"),
                                "event_id": event["event_id"]})
    return origins
