"""Quality-gated profile comparison on frozen, annotated candidate sets."""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from karen.context.contracts import Ranking
from karen.context.prompts import RERANK
from karen.context.retrieval import Retriever
from karen.evaluation.runner import MeteredModel, source_hash
from karen.evaluation.stages import ranking_quality
from karen.models import deepseek_client
from karen.observability import Observer


async def main():
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = ",".join(
            filter(None, [os.environ.get(name, ""), "api.deepseek.com,localhost,127.0.0.1"])
        )
    fixture = Path("tests/fixtures/rerank_acceptance_v1.json")
    data = json.loads(fixture.read_text())
    root = Path("runs/evaluation/rerank-acceptance-20261012")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "plan.json").exists():
        raise ValueError("Preserve existing run")
    plan = {
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "source_sha256": source_hash(Path("src")),
        "repeats": 3,
        "cases": len(data["cases"]),
        "concurrency": 2,
        "policy": "Use production rerank deadline and truncation recovery. Same full schema and prompt. Score required evidence groups, irrelevant inclusion, history target and structure independently. No prompt or annotation tuning during run.",
    }
    (root / "plan.json").write_text(json.dumps(plan, indent=2))
    (root / "fixture.json").write_bytes(fixture.read_bytes())
    results = []
    sem = asyncio.Semaphore(2)

    async def run(case, repeat, thinking):
        async with sem:
            client = MeteredModel(deepseek_client(thinking=thinking))
            retriever = Retriever(
                SimpleNamespace(
                    storage=None, observer=Observer(), model=client, rerank_model=client
                )
            )
            row = {"id": case["id"], "repeat": repeat, "thinking": thinking, "calls": client.calls}
            start = time.monotonic()
            try:
                payload = await retriever.call(
                    {"deadline": start + 120}, "memory_rerank", RERANK, case["input"], Ranking
                )
                row["payload"] = payload.model_dump(mode="json")
                row["quality"] = ranking_quality(
                    case["input"]["primary_ids"],
                    row["payload"]["ranking"],
                    case["required_groups"],
                    case["irrelevant_ids"],
                )
                event_tasks = {
                    e["event_id"]: e["request_id"] for e in case["input"]["history_candidates"]
                }
                row["history_correct"] = (
                    payload.history_status == case["history_status"]
                    and set(payload.related_request_ids) == set(case["allowed_tasks"])
                    and set(payload.selected_event_ids) <= event_tasks.keys()
                    and set(payload.related_request_ids)
                    == {event_tasks[eid] for eid in payload.selected_event_ids}
                )
            except Exception as exc:
                row["error"] = getattr(exc, "code", type(exc).__name__)
            row["seconds"] = round(time.monotonic() - start, 3)
            results.append(row)
            with (root / "results.jsonl").open("a") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                case["id"],
                repeat,
                thinking,
                row.get("error") or row["quality"]["status"],
                row.get("history_correct"),
                flush=True,
            )

    jobs = []
    for index, case in enumerate(data["cases"]):
        for repeat in range(3):
            for thinking in [True, False] if (index + repeat) % 2 == 0 else [False, True]:
                jobs.append(run(case, repeat, thinking))
    await asyncio.gather(*jobs)
    (root / "completion.json").write_text(
        json.dumps(
            {
                "completed": len(results),
                "source_stable": source_hash(Path("src")) == plan["source_sha256"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
