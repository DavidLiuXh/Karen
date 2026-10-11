"""Summarize diagnostic interventions without mixing them into benchmark scores."""

import json
import statistics
from collections import Counter
from pathlib import Path

from diagnose_pipeline import dump


def main():
    root = Path("runs/evaluation/diagnosis-20261011")
    rows = [json.loads(line) for line in (root / "results.jsonl").read_text().splitlines()]
    assert len(rows) == len({r["key"] for r in rows}) == 224
    summary = {"clarification": {}, "memory": {}, "reviews": {}, "rerank": {}}
    for arm in ("direct", "runtime"):
        selected = [
            r
            for r in rows
            if r.get("experiment") == "clarification"
            and r["id"].startswith("clamber")
            and r["arm"] == arm
        ]
        counts = Counter()
        for row in selected:
            actual = row["steps"][0]["outcome"] == "clarification" if row["steps"] else None
            expected = row["expected"][0]["outcome"] == "clarification"
            counts[
                "error"
                if actual is None
                else "correct"
                if actual == expected
                else "missed"
                if expected
                else "overask"
            ] += 1
        summary["clarification"][arm] = {
            "counts": dict(counts),
            "median_seconds": statistics.median(r["seconds"] for r in selected),
            "calls": sum(len(r["calls"]) for r in selected),
            "repeat_scores": [
                sum(
                    bool(r["steps"])
                    and (r["steps"][0]["outcome"] == "clarification")
                    == (r["expected"][0]["outcome"] == "clarification")
                    for r in selected
                    if r["repeat"] == n
                )
                for n in (0, 1)
            ],
        }
    for arm in ("raw", "baseline", "candidate", "content"):
        selected = [
            r
            for r in rows
            if r.get("experiment") == "memory" and r["id"] != "zh-residence" and r["arm"] == arm
        ]
        counts = Counter()
        case_grades = {}
        for row in selected:
            grade = json.loads(
                (root / "evidence-aware-grades" / (row["key"] + ".json")).read_text()
            )["answer"]
            case_grades[row["id"]] = grade
            counts[grade["verdict"]] += 1
        summary["memory"][arm] = {"counts": dict(counts), "case_grades": case_grades}
    stored = [
        json.loads(f.read_text())
        for f in (root / "storage").glob("*.json")
        if f.name != "plan.json"
    ]
    assert len(stored) == 15
    summary["memory"]["stored"] = dict(
        Counter(
            r.get("error") or (r.get("quality") or {}).get("verdict", "ungraded")
            for r in stored
            if r["id"] != "zh-residence"
        )
    )
    reviewed = [r for r in rows if r.get("experiment") == "review" and not r.get("skipped")]
    transitions = Counter()
    for row in reviewed:
        grade = json.loads((root / "evidence-aware-grades" / (row["key"] + ".json")).read_text())
        transitions[
            (
                (grade["draft"] or {}).get("verdict", "ungraded"),
                (grade["final"] or {}).get("verdict", "ungraded"),
            )
        ] += 1
    summary["reviews"] = {
        "pairs": len(reviewed),
        "changed_answers": sum(r["changed"] for r in reviewed),
        "transitions": {str(k): v for k, v in transitions.items()},
    }
    for arm in ("full", "compact", "full_nonthinking"):
        selected = [
            json.loads(f.read_text())
            for f in (root / "rerank").glob("*.json")
            if f.name != "plan.json"
        ]
        selected = [r for r in selected if r["arm"] == arm]
        assert len(selected) == 6
        summary["rerank"][arm] = {
            "valid_id_sets": sum(
                r.get("valid_ids", False) and not r.get("error") for r in selected
            ),
            "errors": dict(Counter(r.get("error", "ok") for r in selected)),
            "median_seconds": statistics.median(r["calls"][0]["seconds"] for r in selected),
        }
    summary["limitations"] = [
        "Memory grades below are automatic diagnostic grades, not official LongMemEval scores.",
        "Raw 09ba9854 is disputed: evidence-aware judge passes an answer that does not return the reference $50 and rejects the user-reported $10 fare. Keep this anomaly visible.",
        "CLAMBER has 24 unique cases repeated twice, not 48 independent examples.",
        "Direct vs runtime changes prompt packaging/output contract as well as flow; it does not isolate node count alone.",
        "Nonthinking profile also sets temperature=0; this is a profile comparison, not a pure thinking-switch causal estimate.",
        "Rerank ID validity is not relevance quality; saved inputs and six first attempts cannot establish a new production default.",
    ]
    dump(root / "summary.json", summary)
    print(
        json.dumps(
            {
                k: (
                    {a: {x: y for x, y in b.items() if x != "case_grades"} for a, b in v.items()}
                    if k == "memory"
                    else v
                )
                for k, v in summary.items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
