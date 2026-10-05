import hashlib
import json
from pathlib import Path

import pytest

from karen.evaluation.datasets import clamber, longmemeval, parse_date, prepare_next, read_rows
from karen.evaluation.runner import check_step, summarize


def test_frozen_grader_keeps_legacy_nonthinking_request(monkeypatch):
    from karen.evaluation import runner

    seen = {}

    def chat(**kwargs):
        seen["provider"] = kwargs
        return "frozen-chat"

    def adapter(**kwargs):
        seen["adapter"] = kwargs
        return "frozen-client"

    monkeypatch.setattr(runner, "ChatDeepSeek", chat)
    monkeypatch.setattr(runner, "LangChainModelClient", adapter)
    assert runner.frozen_judge_client() == "frozen-client"
    assert seen["provider"] == {"model": "deepseek-chat", "temperature": 0, "max_retries": 0}
    assert seen["adapter"] == {"chat_model": "frozen-chat", "model": "deepseek-chat", "mode": "function_calling"}


async def test_memory_and_decision_clients_share_accounting_but_keep_profiles_separate(tmp_path, monkeypatch):
    from dynamic_graph import FakeModelClient
    from intent_helpers import ClarityAwareModel
    from test_context import LocalEmbeddings, MemoryModel

    from karen.evaluation import runner

    memory_client = MemoryModel()
    memory_client.metadata = {"thinking": False}
    decision_client = ClarityAwareModel([
        {"input_types": ["question"], "handling": "respond", "task_relation": "new", "reason": "读取已知记录"},
        {"decision": {"outcome": "reply", "answer": "你记录了35元。"}},
    ])
    decision_client.metadata = {"thinking": True}
    seen = []

    def factory(*, thinking=True):
        seen.append(thinking)
        return decision_client if thinking else memory_client

    monkeypatch.setattr(runner, "memory_embeddings", LocalEmbeddings)
    case = {
        "id": "profile-isolation", "suite": "karen-zh", "split": "heldout", "category": "model-profiles",
        "timezone": "UTC", "reference_time": "2026-10-04T08:00:00+00:00", "context": "",
        "history": [{"session_id": "cost", "turn": 0, "occurred_at": "2026-10-01T08:00:00+00:00", "role": "user", "content": "这次我花了35元。"}],
        "steps": [{"text": "我这次花了多少？", "expected": {"outcome": "complete", "contains": ["35"]}}],
        "gold": {"answer": "按用户记录，本次花费35元。"}, "search_fixture": False,
    }
    result = await runner.evaluate_case(
        case, tmp_path / "case", client_factory=factory,
        judge_client_factory=lambda: FakeModelClient([{"passed": True, "reason": "匹配记录"}]),
    )
    assert result["status"] == "passed" and seen == [True, False]
    assert result["agent_model"]["thinking"] is True
    assert result["memory_model"]["thinking"] is False
    for call in result["model_calls"]:
        if call["role"].startswith("memory_"):
            assert call["model"]["thinking"] is False
        elif call["role"].startswith("intent"):
            assert call["model"]["thinking"] is True
    assert len(result["model_calls"]) == len(memory_client.requests) + len(decision_client.requests) + 1


async def test_final_rejected_model_output_is_saved_without_changing_error_or_grading(tmp_path):
    from dynamic_graph import FakeModelClient, ModelCallError

    from karen.evaluation.runner import evaluate_case

    raw = '{"input_types":["question"],"handling":"respond"}}'
    syntax = {"message": "Extra data", "position": len(raw) - 1}
    invalid = ModelCallError(
        "MODEL_RESPONSE_INVALID", "Rejected JSON", raw_response=raw,
        details={"json_syntax": syntax, "provider_exception": "must never be persisted"},
    )
    client = FakeModelClient([invalid, invalid])
    grader = FakeModelClient()
    case = {
        "id": "invalid-model-output", "suite": "clamber", "split": "development",
        "category": "FD/0", "timezone": "UTC", "reference_time": "2026-10-04T08:00:00+00:00",
        "context": "", "history": [],
        "steps": [{"text": "查看公开项目。", "expected": {"outcome": "goal"}}], "gold": {},
    }
    directory = tmp_path / "case"
    result = await evaluate_case(
        case, directory, client_factory=lambda: client, judge_client_factory=lambda: grader
    )
    saved = json.loads((directory / "result.json").read_text())
    assert result == saved
    assert result["status"] == "error" and result["error_code"] == "MODEL_RESPONSE_INVALID"
    assert result["invalid_model_response"] == raw and result["json_syntax"] == syntax
    assert "must never be persisted" not in json.dumps(result)
    assert grader.requests == [] and len(client.requests) == 2


async def test_agent_and_frozen_judge_are_isolated_and_both_calls_are_recorded(tmp_path):
    from dynamic_graph import FakeModelClient

    from karen.evaluation.runner import evaluate_case

    agent = FakeModelClient(
        [
            {
                "input_types": ["task_request"],
                "handling": "assess",
                "task_relation": "new",
                "reason": "缺少目标文件",
            },
            {
                "references": [],
                "known_referents": {},
                "selection_criteria": [],
                "questions": [{"text": "你指哪个文件？", "kind": "missing_requirement"}],
                "reason": "需要文件定位",
            },
        ]
    )
    grader = FakeModelClient([{"passed": True, "reason": "必要澄清符合参考"}])
    case = {
        "id": "isolated-grader",
        "suite": "clamber",
        "split": "development",
        "category": "FD/1",
        "timezone": "UTC",
        "reference_time": "2026-10-04T08:00:00+00:00",
        "context": "",
        "history": [],
        "steps": [{"text": "修改那个文件。", "expected": {"outcome": "clarification"}}],
        "gold": {"clarifying_question": "请提供唯一的文件路径（评分专用）。"},
    }
    result = await evaluate_case(
        case, tmp_path / "case", client_factory=lambda: agent, judge_client_factory=lambda: grader
    )
    assert result["status"] == "passed"
    assert len(grader.requests) == 1 and grader.requests[0].role == "evaluation_judge"
    assert grader.requests[0].input_data["reference"] == case["gold"]["clarifying_question"]
    assert all("评分专用" not in str(request.input_data) for request in agent.requests)
    assert [call["role"] for call in result["model_calls"]] == [
        "intent_router",
        "intent_clarity",
        "evaluation_judge",
    ]


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


def test_live_search_is_explicit_and_fixture_profile_cannot_be_mixed(tmp_path, monkeypatch):
    from dynamic_graph import FakeModelClient
    from dynamic_graph.tools import tavily_search_tool

    from karen.capabilities import all_capabilities_policy
    from karen.evaluation import runner

    searches = []

    def tool(**kwargs):
        searches.append(kwargs)
        return tavily_search_tool(api_key="fake-search-key")

    monkeypatch.setattr(runner, "tavily_search_tool", tool)
    for name, live in [("files", False), ("live", True)]:
        directory = tmp_path / name
        directory.mkdir()
        engine = runner.create_engine(FakeModelClient(), directory, False, live_search=live)
        policy = all_capabilities_policy(engine)
        assert any("tavily" in item for item in policy.allowed_tools) is live
    assert searches == [{}]
    with pytest.raises(ValueError, match="either fixed or live"):
        runner.create_engine(FakeModelClient(), tmp_path / "mixed", True, live_search=True)


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
