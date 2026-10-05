import hashlib
import json
from pathlib import Path

import pytest

from karen.evaluation.datasets import clamber, longmemeval, parse_date, prepare_next, read_rows
from karen.evaluation.runner import check_step, summarize


def long_row():
    return {
        "question_id": "example",
        "question_type": "knowledge-update",
        "question": "Where?",
        "answer": "Shanghai",
        "question_date": "2026/10/04 (Sun) 12:00",
        "haystack_dates": ["2026/10/03 (Sat) 09:00", "2026/10/01 (Thu) 10:00"],
        "haystack_session_ids": ["new", "old"],
        "answer_session_ids": ["new"],
        "haystack_sessions": [
            [{"role": "user", "content": "Shanghai", "has_answer": True}],
            [{"role": "assistant", "content": "Beijing"}],
        ],
    }


def test_official_clamber_double_encoding_and_gold_separation(tmp_path):
    row = {
        "question": "Where?",
        "context": "given context",
        "clarifying_question": "Which city?",
        "require_clarification": 1,
        "category": "FD",
        "subclass": "where",
        "predict_clarifying_question": "must never enter agent input",
    }
    path = tmp_path / "data.jsonl"
    path.write_text(json.dumps(json.dumps(row)) + "\n")
    case = clamber(read_rows(path)[0], 12)
    assert case["context"] == "given context"
    assert case["steps"][0]["text"] == "Where?"
    assert "Which city?" not in json.dumps(case["steps"])
    assert "predict" not in json.dumps(case)


def test_memory_adapter_sorts_preserves_roles_and_excludes_answer_annotations():
    case = longmemeval(long_row(), "oracle", "Asia/Shanghai")
    assert [h["session_id"] for h in case["history"]] == ["old", "new"]
    assert [h["role"] for h in case["history"]] == ["assistant", "user"]
    assert len(case["history"]) == 2
    assert "has_answer" not in json.dumps(case["history"])
    assert "answer_session_ids" not in json.dumps(case["history"])
    assert case["reference_time"].endswith("+08:00")


def test_memory_adapter_rejects_future_sessions_without_silently_truncating():
    row = long_row()
    row["haystack_dates"][0] = "2026/10/05 (Mon) 09:00"
    with pytest.raises(ValueError, match="future"):
        longmemeval(row, "s")
    row["haystack_session_ids"].pop()
    with pytest.raises(ValueError, match="Misaligned"):
        longmemeval(row, "s")


def test_date_offset_is_preserved_and_naive_date_uses_explicit_zone():
    assert parse_date("2026-10-04T00:00:00+09:00", "UTC").utcoffset().total_seconds() == 32400
    assert (
        parse_date("2026/10/04 (Sun) 00:00", "Asia/Shanghai").utcoffset().total_seconds() == 28800
    )


def test_engine_completion_and_trusted_clock_are_not_business_passes():
    actual = {
        "outcome": "execution",
        "execution_status": "COMPLETED",
        "output_complete": False,
        "answer": "Done",
        "goal": {"context": {"date": "2026-10-05"}},
    }
    failures = check_step({"outcome": "execution", "goal_contains": ["2026-10-05"]}, actual)
    assert len(failures) == 2
    assert check_step({"outcome": "complete"}, {"outcome": "clarification", "answer": "When?"})


def test_reporting_keeps_errors_in_denominator_and_splits_separate():
    results = [
        {"suite": "clamber", "split": "development", "status": status}
        for status in ["passed", "failed", "error", "judge_error"]
    ]
    summary = summarize(results)["clamber/development"]
    assert summary["total"] == 4
    assert summary["pass_rate_all"] == 0.25


def test_frozen_chinese_cases_are_public_synthetic_and_have_unique_ids():
    rows = read_rows(Path(__file__).parent / "fixtures/evaluation_zh.json")
    assert len({row["id"] for row in rows}) == len(rows)
    assert {row["split"] for row in rows} == {"development", "heldout"}
    assert all(row["timezone"] and row["reference_time"] for row in rows)


def test_expansion_is_disjoint_reproducible_and_preserves_entire_histories(tmp_path):
    clamber_path = tmp_path / "clamber.jsonl"
    clamber_path.write_text(
        "\n".join(
            json.dumps(
                dict(
                    question=f"Question {i}",
                    context="",
                    require_clarification=1,
                    category="FD",
                    clarifying_question="Which?",
                )
            )
            for i in range(8)
        )
    )
    memory_path = tmp_path / "longmemeval_oracle.json"
    rows = [{**long_row(), "question_id": f"memory-{i}"} for i in range(6)]
    future = {**long_row(), "question_id": "future", "question_date": "2026-09-01"}
    memory_path.write_text(
        json.dumps([*rows, {**long_row(), "question_id": "memory-0_abs"}, future])
    )
    previous = tmp_path / "previous.json"
    previous.write_text(
        json.dumps(
            {
                "protocol_version": "1",
                "sources_sha256": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [clamber_path, memory_path]
                },
                "cases": [
                    {"id": "clamber-0000", "suite": "clamber"},
                    {"id": "memory-0", "suite": "longmemeval-oracle"},
                ],
            }
        )
    )
    first = prepare_next(tmp_path, [previous], tmp_path / "first.json")
    second = prepare_next(tmp_path, [previous], tmp_path / "second.json")
    assert first == second
    assert len(first["cases"]) == 6
    assert not {c["id"] for c in first["cases"]} & {
        "clamber-0000",
        "memory-0",
        "memory-0_abs",
        "future",
    }
    assert len([c for c in first["cases"] if c["split"] == "heldout"]) == 3
    assert all(len(c["history"]) == 2 for c in first["cases"] if c["history"])
    assert first["eligible_remaining_by_category"] == {
        "clamber.jsonl/FD/1": 7,
        "longmemeval_oracle.json/knowledge-update": 5,
    }
    assert first["excluded_invalid_records"][0]["id"] == "future"
    with pytest.raises(FileExistsError):
        prepare_next(tmp_path, [previous], tmp_path / "first.json")
    memory_path.write_text("[]")
    with pytest.raises(ValueError, match="Source changed"):
        prepare_next(tmp_path, [previous], tmp_path / "third.json")


async def test_ingestion_waits_for_all_events_and_async_statuses():
    from types import SimpleNamespace

    from karen.evaluation.runner import ingest

    class Memory:
        def __init__(self):
            self.events = []
            self.flushed = False

        def submit(self, event):
            self.events.append(event)
            return SimpleNamespace(event_id=event.event_id)

        async def flush(self):
            self.flushed = True

        async def write_status(self, receipt):
            assert self.flushed
            return SimpleNamespace(model_dump=lambda **_: {"id": receipt.event_id})

    memory = Memory()
    result = await ingest(memory, longmemeval(long_row(), "oracle"))
    assert len(result) == 2
    assert [e.event_type for e in memory.events] == ["assistant_message", "user_message"]


@pytest.mark.parametrize("mutate", [False, True])
async def test_run_preserves_results_but_rejects_source_changes(tmp_path, monkeypatch, mutate):
    from karen.evaluation import runner
    from karen.evaluation.datasets import PROTOCOL_VERSION

    state = {"changed": False}
    monkeypatch.setattr(
        runner, "source_hash", lambda directory: "after" if state["changed"] else "before"
    )
    monkeypatch.setattr(runner, "revision", lambda _: "local-test")
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *args, **kwargs: "")

    async def evaluate(case, directory):
        state["changed"] = mutate
        return {
            "id": case["id"],
            "suite": "synthetic",
            "split": "development",
            "status": "passed",
            "checks": [],
        }

    monkeypatch.setattr(runner, "evaluate_case", evaluate)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "cases": [{"id": "synthetic", "suite": "synthetic", "split": "development"}],
            }
        )
    )
    output = tmp_path / "run"
    if mutate:
        with pytest.raises(RuntimeError, match="SOURCE_CHANGED_DURING_EVALUATION"):
            await runner.run(manifest, output, concurrency=1)
    else:
        assert (await runner.run(manifest, output, concurrency=1))[0]["status"] == "passed"
    metadata = json.loads((output / "run.json").read_text())
    assert metadata["source_integrity"] == ("changed" if mutate else "stable")
    assert len((output / "results.jsonl").read_text().splitlines()) == 1
    assert json.loads((output / "summary.json").read_text())["synthetic/development"]["total"] == 1
