"""Regrade fixed diagnostic answers with their exact evidence; no regeneration."""

import asyncio
import json
import os
from pathlib import Path

from diagnose_pipeline import dump

from karen.evaluation.clarification import grade_quality
from karen.model.models import deepseek_client


async def main():
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = ",".join(
            filter(None, [os.environ.get(name, ""), "api.deepseek.com,localhost,127.0.0.1"])
        )
    root = Path("runs/evaluation/diagnosis-20261011")
    source = Path("runs/evaluation/control-20261010-final")
    cases = {c["id"]: c for c in json.loads((source / "manifest-v2.json").read_text())["cases"]}
    rows = [json.loads(line) for line in (root / "results.jsonl").read_text().splitlines()]
    out = root / "evidence-aware-grades"
    if (out / "protocol.json").exists():
        raise ValueError("Diagnostic output already exists; preserve the original run")
    dump(
        out / "protocol.json",
        {
            "reason": "Initial diagnostic grader requests omitted reader evidence / review memory. Retain original grades but supersede them with these evidence-aware grades. Generation and original benchmark labels unchanged.",
            "source": "saved answers only",
            "concurrency": 1,
        },
    )
    for row in rows:
        case = cases[row["id"]]
        if row.get("experiment") == "memory" and row.get("answer"):
            evidence = row["calls"][0]["input"]["evidence"]
            pairs = [("answer", row["answer"], {"evidence": evidence})]
        elif row.get("experiment") == "review" and not row.get("skipped"):
            saved = json.loads((source / row["arm"] / row["id"] / "result.json").read_text())
            evidence = saved["steps"][0].get("memory")
            pairs = [("draft", row["draft"], evidence), ("final", row["final"], evidence)]
        else:
            continue
        result = {"key": row["key"], "id": row["id"]}
        for name, answer, evidence in pairs:
            try:
                result[name] = await grade_quality(
                    deepseek_client(thinking=False),
                    case,
                    [{"outcome": "reply", "answer": answer, "memory": evidence}],
                )
            except Exception as exc:
                result[name] = {
                    "verdict": "error",
                    "error": getattr(exc, "code", type(exc).__name__),
                }
        dump(out / (row["key"] + ".json"), result)
        print(
            row["key"],
            {
                k: (v or {}).get("verdict")
                for k, v in result.items()
                if k in ["answer", "draft", "final"]
            },
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
