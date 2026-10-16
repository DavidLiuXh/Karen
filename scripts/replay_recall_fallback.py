"""Replay the observed residence failure without rerunning extraction or retrieval."""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from karen.context.contracts import QueryAnalysis, Ranking, RecallQuery, StoredMemory
from karen.context.retrieval import Retriever
from karen.evaluation.runner import MeteredModel
from karen.intent import IntentRecognizer, IntentSession
from karen.models import deepseek_client
from karen.observability import Observer


async def main():
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = ",".join(
            filter(None, [os.environ.get(name, ""), "api.deepseek.com,localhost,127.0.0.1"])
        )
    root = Path("runs/evaluation/control-20261010-final/candidate/zh-residence")
    output = Path("runs/evaluation/fallback-repair-20261012")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "results.json").exists():
        raise ValueError("Preserve existing results")
    events = [
        json.loads(line)
        for f in (root / "observability/traces").glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    retrieved = next(
        e["data"]
        for e in events
        if e["event_type"] == "decision" and e["stage"] == "memory.recall.retrieve"
    )
    req = next(
        e["data"]["input"]
        for e in events
        if e["event_type"] == "model.request" and e.get("model_role") == "memory_rerank"
    )
    saved = json.loads((root / "result.json").read_text())["steps"][0]["memory"]
    service = SimpleNamespace(storage=None, observer=Observer(), _pending={})
    query = RecallQuery(
        text=req["query"],
        conversation_id="diagnostic",
        request_id=req["current_request_id"],
        timezone="Asia/Shanghai",
        current_time_utc=datetime.fromisoformat(saved["query_time_basis"]["current_time_utc"]),
    )
    state = {
        "query": query,
        "analysis": QueryAnalysis.model_validate(req["analysis"]),
        "ranking": Ranking(ranking=[]),
        "degraded": ["RERANK_FAILED_FUSION_ORDER"],
        "memories": {
            key: StoredMemory.model_validate(value)
            for key, value in retrieved["candidates"].items()
        },
        "primary": req["primary_ids"],
        "bundles": retrieved["bundles"],
        "scores": retrieved["scores"],
        "index_coverage": retrieved["index_coverage"],
        "revision": retrieved["revision"],
        "anchors": [],
        "deadline": time.monotonic() + 120,
    }
    recalled = (await Retriever(service).assemble(state))["result"]
    assert any(
        h.memory.value == {"city": "苏州", "status": "settled"} and not h.evidence_only
        for h in recalled.m1
    )
    assert all(h.evidence_only for h in recalled.m1 if h.memory.state != "active")
    cases = [
        ("zh", "我目前住在哪个城市？"),
        ("en", "Which city do I live in now?"),
        ("unrelated", "太阳表面温度通常是多少？"),
    ]
    results = []
    for name, text in cases:
        model = MeteredModel(deepseek_client())
        try:
            session = await IntentRecognizer(model).advance(
                IntentSession(timezone="Asia/Shanghai", reference_time_utc=query.current_time_utc),
                text,
                memory_context=recalled.context(),
            )
            results.append(
                {
                    "id": name,
                    "reply": session.reply,
                    "questions": session.questions,
                    "goal": session.goal.model_dump(mode="json") if session.goal else None,
                    "calls": model.calls,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": name,
                    "error": getattr(exc, "code", type(exc).__name__),
                    "calls": model.calls,
                }
            )
    (output / "results.json").write_text(
        json.dumps(
            {"before": saved, "after": recalled.context(), "answers": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    print([(r["id"], r.get("reply"), r.get("error")) for r in results], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
