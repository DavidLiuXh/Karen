"""Read-only evidence intervention before retrieval; companion to diagnose_pipeline."""

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from diagnose_pipeline import ANSWER_SCHEMA, READER_SYSTEM, dump, invoke

from karen.evaluation.clarification import grade_quality
from karen.model.models import deepseek_client


async def main():
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = ",".join(
            filter(None, [os.environ.get(name, ""), "api.deepseek.com,localhost,127.0.0.1"])
        )
    source = Path("runs/evaluation/control-20261010-final")
    out = Path("runs/evaluation/diagnosis-20261011/storage")
    if (out / "plan.json").exists():
        raise ValueError("Diagnostic output already exists; preserve the original run")
    cases = [
        c
        for c in json.loads((source / "manifest-v2.json").read_text())["cases"]
        if c["suite"] == "longmemeval-oracle" or c["id"] == "zh-residence"
    ]
    dump(
        out / "plan.json",
        {
            "cases": [c["id"] for c in cases],
            "intervention": "Supply all derived records whose sources are exclusively original history event IDs, before retrieval filtering. Candidate snapshot, readonly SQLite. Same reader and grade as main experiment. Do not supply answer gold to reader.",
            "concurrency": 1,
        },
    )
    for case in cases:
        db = source / "candidate" / case["id"] / "memory/memory.sqlite3"
        ids = {
            hashlib.sha256(f"{case['id']}:{e['session_id']}:{e['turn']}".encode()).hexdigest()
            for e in case["history"]
        }
        with sqlite3.connect("file:" + str(db.resolve()) + "?mode=ro", uri=True) as connection:
            corpus = [json.loads(r[0]) for r in connection.execute("select body from memories")]
            evidence = [
                m for m in corpus if m["sources"] and {s["event_id"] for s in m["sources"]} <= ids
            ]
        row = {"id": case["id"], "arm": "stored", "records": len(evidence), "calls": []}
        try:
            payload = await invoke(
                deepseek_client(),
                "diagnostic_reader",
                READER_SYSTEM,
                "Answer from the supplied evidence; do not use any unprovided conversation.",
                {
                    "question": case["steps"][0]["text"],
                    "reference_time": case["reference_time"],
                    "timezone": case["timezone"],
                    "evidence": evidence,
                },
                ANSWER_SCHEMA,
                row["calls"],
            )
            row["answer"] = payload["answer"]
            row["quality"] = await grade_quality(
                deepseek_client(thinking=False),
                case,
                [
                    {
                        "outcome": "reply",
                        "answer": payload["answer"],
                        "memory": {"evidence": evidence},
                    }
                ],
            )
            row["contains_checks"] = {
                s: s.casefold() in payload["answer"].casefold()
                for s in case["steps"][0]["expected"].get("contains", [])
            }
        except Exception as exc:
            row["error"] = getattr(exc, "code", type(exc).__name__)
        dump(out / (case["id"] + ".json"), row)
        print(case["id"], row.get("error", row.get("quality")), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
