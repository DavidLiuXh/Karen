"""Audit whether saved drafts already satisfy the actual review contract."""

import json
from collections import Counter
from pathlib import Path

from karen.intent.recognizer import Assessment, DirectAssessment, IntentRecognizer


def main():
    root = Path("runs/evaluation/control-20261010-final")
    rows = []
    for arm in ["baseline", "candidate"]:
        for directory in (root / arm).iterdir():
            if not directory.is_dir():
                continue
            events = [
                json.loads(line)
                for path in (directory / "observability/traces").glob("*.jsonl")
                for line in path.read_text().splitlines()
            ]
            for event in events:
                if event["event_type"] != "model.request" or event.get("model_role") not in {
                    "intent_response_review",
                    "intent_goal_review",
                }:
                    continue
                role = event["model_role"]
                data = event["data"]["input"]
                is_repair = "validation_errors" in data and "draft" not in data
                review_input = data["original_input"] if is_repair else data
                inputs = review_input.get("original_input", review_input)
                row = {
                    "arm": arm,
                    "id": directory.name,
                    "role": role,
                    "event_id": event["event_id"],
                    "evidence_count": len(inputs.get("evidence_to_consider", [])),
                    "request_kind": "validation_repair" if is_repair else "review",
                }
                try:
                    if is_repair:
                        row["draft_contract"] = "validation_repair"
                        row["validation_errors"] = data["validation_errors"]
                    else:
                        draft = (
                            DirectAssessment if role == "intent_response_review" else Assessment
                        ).model_validate(data["draft"])
                        IntentRecognizer._check_evidence_coverage(inputs, draft)
                        row["draft_contract"] = "valid"
                except Exception as exc:
                    row["draft_contract"] = str(exc).splitlines()[0][:150]
                row["review_responded"] = any(
                    e["event_type"] == "model.response" and e.get("span_id") == event["span_id"]
                    for e in events
                )
                rows.append(row)
    output = Path("runs/evaluation/review-contract-20261012.json")
    output.write_text(
        json.dumps(
            {"rows": rows, "summary": dict(Counter(r["draft_contract"] for r in rows))},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(Counter(r["draft_contract"] for r in rows))
    print(
        "empty evidence",
        sum(r["evidence_count"] == 0 for r in rows),
        "review failures",
        sum(not r["review_responded"] for r in rows),
    )


if __name__ == "__main__":
    main()
