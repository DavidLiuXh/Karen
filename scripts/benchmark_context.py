"""Reproducible local storage benchmark, entirely within a temporary directory.

Run: uv run python scripts/benchmark_context.py --records 10000
This measures storage/index retrieval, not LLM quality or end-to-end latency.
"""

import argparse
import tempfile
import time
from pathlib import Path

from karen.context import ContextEvent, RecallQuery
from karen.context.contracts import Evidence, StoredMemory, utcnow
from karen.context.retrieval import hybrid
from karen.context.storage import Storage


def benchmark(count):
    with tempfile.TemporaryDirectory(prefix="karen-context-benchmark-") as directory:
        storage = Storage(Path(directory))
        storage.initialize()
        now = utcnow()
        event = ContextEvent(
            conversation_id="benchmark",
            request_id="source",
            timezone="UTC",
            event_type="user_message",
            payload={"content": "项目日报基准测试合成证据"},
        )
        storage.append(event, 1, now)
        source = storage.source(
            Evidence(event_id=event.event_id, pointer="/payload/content", quote="合成证据"),
            {event.event_id: event},
        )
        memories = [
            StoredMemory(
                memory_id=f"memory-{i}",
                layer="m2",
                text=f"项目日报记录编号 {i} 文件 /tmp/report-{i}.html",
                sources=[source],
                recorded_at=now,
                created_at=now,
                updated_at=now,
                request_id=f"task-{i}",
            )
            for i in range(count)
        ]
        started = time.monotonic()
        storage.commit_memories(event.event_id, memories, 0)
        indexing = time.monotonic() - started
        # Fixed synthetic vectors isolate scan costs from embedding inference.
        vector = [1.0] + [0.0] * 1023
        storage.save_vectors(memories, [vector] * count, "benchmark-1024")
        durations = []
        for _ in range(3):
            started = time.monotonic()
            _, snapshot, vectors, keyword, _ = storage.retrieval_snapshot("项目日报")
            _, roots, _ = hybrid(
                snapshot,
                vectors,
                keyword,
                vector,
                "benchmark-1024",
                RecallQuery(
                    text="项目日报", timezone="UTC", conversation_id="bench", request_id="query"
                ),
            )
            durations.append(round((time.monotonic() - started) * 1000, 1))
        size = sum(path.stat().st_size for path in Path(directory).rglob("*") if path.is_file())
        print(
            {
                "records": count,
                "vector_dimension": 1024,
                "index_seconds": round(indexing, 2),
                "snapshot_bm25_cosine_rrf_ms": durations,
                "primary_candidates": len(roots),
                "disk_mib": round(size / 1024**2, 2),
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.records <= 100000:
        parser.error("records must be between 1 and 100000")
    benchmark(args.records)
