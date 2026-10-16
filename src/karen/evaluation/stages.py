"""Separate action, evidence presence and delivery; never replace public grades."""

from .clarification import action_agreement


def evidence_presence(expectation, records):
    """Annotated quote groups allow equivalent sources; this measures presence, not truth.

    A group is supported when one explicitly annotated quote occurs in a source quote
    or raw detail. An empty record list is scored as missing; None means unobserved.
    No expected answer is inferred from the model's response.
    """
    if expectation is None:
        return {"status": "unscored"}
    groups = expectation.get("required_quote_groups", [])
    if not groups or any(
        not group or any(not isinstance(q, str) or not q for q in group) for group in groups
    ):
        raise ValueError("Evidence annotations require nonempty alternative quote groups")
    if records is None:
        return {"status": "unobserved", "required": len(groups)}
    quotes = []
    for record in records:
        if "memory" in record:
            record = record["memory"]
        quotes.extend(s["quote"] for s in record.get("sources", []) if s.get("quote"))
        if record.get("source") and record.get("text"):
            quotes.append(record["text"])
    matched = [any(q in source for q in group for source in quotes) for group in groups]
    return {
        "status": "present" if all(matched) else "missing",
        "required": len(groups),
        "found": sum(matched),
        "missing_groups": [i for i, found in enumerate(matched) if not found],
    }


def stage_metrics(case, result):
    """Evidence annotations are optional and frozen independently of the answer judge."""
    steps = []
    for index, expected in enumerate(case["steps"]):
        actual = result.get("steps", [])[index] if index < len(result.get("steps", [])) else None
        memory = actual.get("memory") if actual else None
        records = (
            [*memory.get("m1", []), *memory.get("m2", []), *memory.get("details", [])]
            if memory is not None
            else None
        )
        try:
            evidence = evidence_presence(expected.get("evidence_expectation"), records)
        except ValueError:
            evidence = {"status": "annotation_error"}
        execution = {"status": "not_executed"}
        if actual and actual.get("outcome") == "execution":
            execution = {
                "status": actual.get("execution_status", "unknown"),
                "output_complete": actual.get("output_complete"),
                "diagnostics": actual.get("diagnostics", []),
            }
        steps.append(
            {
                "public_action": action_agreement(expected["expected"], actual),
                "product_action": action_agreement(expected.get("policy_expectation", {}), actual),
                "packed_evidence": evidence,
                "recall_status": memory.get("status") if memory else None,
                "recall_degradations": memory.get("degradations", []) if memory else [],
                "execution": execution,
            }
        )
    return {
        "steps": steps,
        "runtime_error": result.get("error_code"),
        "error_stage": result.get("stage"),
        "quality_status": result.get("status"),
    }


def ranking_quality(primary_ids, ranking, required_groups, irrelevant_ids):
    """Frozen candidate-ID annotations for fixed-input reranking experiments."""
    if not set(irrelevant_ids) <= set(primary_ids) or any(
        not set(g) <= set(primary_ids) or not g for g in required_groups
    ):
        raise ValueError("Ranking annotations must refer to actual primary IDs")
    if ranking is None:
        return {"status": "error", "required": len(required_groups)}
    if sorted(r["memory_id"] for r in ranking) != sorted(primary_ids):
        return {"status": "invalid_ids", "required": len(required_groups)}
    if any(r["relevance"] not in {"relevant", "uncertain", "irrelevant"} for r in ranking):
        return {"status": "invalid_relevance", "required": len(required_groups)}
    selected = {r["memory_id"] for r in ranking if r["relevance"] != "irrelevant"}
    missing = [i for i, group in enumerate(required_groups) if not selected.intersection(group)]
    unexpected = sorted(selected.intersection(irrelevant_ids))
    return {
        "status": "pass" if not missing and not unexpected else "fail",
        "required": len(required_groups),
        "retained": len(required_groups) - len(missing),
        "missing_groups": missing,
        "irrelevant_retained": unexpected,
    }
