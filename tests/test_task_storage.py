"""Persistence boundaries exercised through task commands and independent processes."""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from dynamic_graph import RunResult

from karen import IntentSession, TaskTurn
from karen.intent.recognizer import ClarificationItem, Message
from karen.message import messages
from karen.task import TaskInteraction, TaskManager
from karen.task.storage import TaskStore


class Agent:
    def __init__(self):
        self.calls = []

    async def advance(self, session, text, *, received_at, **kwargs):
        self.calls.append((session, text))
        if session.completed:
            session = session.next_turn()
        session = session.model_copy(update={
            "messages": (*session.messages, Message(role="user", content=text)),
            "reference_time_utc": session.reference_time_utc or received_at,
        })
        if text == "clarify":
            question = ClarificationItem(question_id="q1", text="Which format?", asked_after=1)
            return TaskTurn(session.model_copy(update={"clarification_items": (question,)}))
        if text == "execute":
            return TaskTurn(
                session, RunResult(run_id=uuid4().hex, execution_status="COMPLETED",
                                   output_complete=True, outputs={"answer": "delivered"}),
            )
        return TaskTurn(session.model_copy(update={"reply": "reply " + text}))

    def extract_workflow(self, turn):
        assert turn.result is not None
        return {"accepted": True, "job_id": "example-job"}


async def test_restart_preserves_clarification_and_closed_tasks(tmp_path):
    anchor = datetime(2026, 11, 18, 13, tzinfo=UTC)
    store = TaskStore(tmp_path)
    agent = Agent()
    first = TaskManager(agent, store=store)
    template = IntentSession(timezone="Asia/Shanghai")
    waiting = first.submit("clarify", template, received_at=anchor)
    closed = first.submit("finished", template)
    await first.drain()
    request_id = waiting.session.request_id
    first.finish(closed.task_id)
    await first.close()
    store.close()

    store = TaskStore(tmp_path)
    try:
        second = TaskManager(agent, store=store)
        ui = TaskInteraction(second, template)
        assert waiting.task_id in ui.accept("/tasks")
        assert second.tasks[waiting.task_id].session is None
        assert "Which format?" in ui.accept(f"/task {waiting.task_id}")
        calls_before = len(agent.calls)
        assert "Which format?" in ui.accept(f"/reply {waiting.task_id}")
        assert ui.submitted_task_id is None and len(agent.calls) == calls_before
        assert ui.accept(f"/reply {closed.task_id}") == messages.TASK_NOT_WAITING
        assert ui.accept(f"/reply {closed.task_id} more") == messages.TASK_NOT_WAITING
        ui.accept(f"/reply {waiting.task_id} Markdown", received_at=datetime(2026, 11, 20, tzinfo=UTC))
        await second.drain()
        restored_session, text = agent.calls[-1]
        assert text == "Markdown"
        assert restored_session.questions == ("Which format?",)
        assert restored_session.reference_time_utc == anchor
        assert restored_session.timezone == "Asia/Shanghai"
        assert restored_session.request_id == request_id
        assert "reply Markdown" in ui.accept(f"/task {waiting.task_id}")
        await second.close()
    finally:
        store.close()


async def test_flow_extraction_distinguishes_restored_execution_and_new_delivery(tmp_path):
    store = TaskStore(tmp_path)
    agent = Agent()
    manager = TaskManager(agent, store=store)
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new execute")
    task_id = ui.focus
    await manager.drain()
    assert "example-job" in ui.accept(f"/workflow extract {task_id}")
    await manager.close()
    store.close()

    store = TaskStore(tmp_path)
    try:
        manager = TaskManager(agent, store=store)
        ui = TaskInteraction(manager, IntentSession())
        assert ui.accept(f"/workflow extract {task_id}") == messages.WORKFLOW_EXTRACT_RESTORED
        ui.accept(f"/reply {task_id} execute")
        await manager.drain()
        assert "example-job" in ui.accept(f"/workflow extract {task_id}")
        await manager.close()
    finally:
        store.close()


async def test_storage_failure_before_engine_and_before_result_delivery(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    manager = TaskManager(Agent(), store=store)
    ui = TaskInteraction(manager, IntentSession())

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("test storage failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "enqueue", unavailable)
            assert ui.accept("/new rejected") == messages.TASK_STORAGE_UNAVAILABLE
            assert not manager.tasks
        with monkeypatch.context() as patch:
            patch.setattr(store, "start", unavailable)
            ui.accept("/new without engine")
            await manager.drain()
            assert not manager.agent.calls
            assert manager.events.get_nowait().state == "failed"
        with monkeypatch.context() as patch:
            patch.setattr(store, "settle", unavailable)
            ui.accept("/new execute")
            task_id = ui.focus
            await manager.drain()
            event = manager.events.get_nowait()
            assert event.error == "TASK_RESULT_NOT_SAVED"
            assert event.turn.result.run_id
            assert ui.accept(f"/reply {task_id} retry") == messages.TASK_STORAGE_UNAVAILABLE
        assert len(manager.agent.calls) == 1
        await manager.close()
    finally:
        store.close()


def test_store_lock_prevents_recovery_from_second_process(tmp_path):
    first = TaskStore(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            TaskStore(tmp_path)
        assert first.summaries() == []
    finally:
        first.close()


async def test_history_returns_original_snapshots_for_multiple_deliveries(tmp_path):
    with TemporaryDirectory(dir="/tmp") as directory:
        root = Path(directory).resolve()
        records = tmp_path / "run" / "artifacts"
        records.mkdir(parents=True)
        paths = [root / name for name in ("report.md", "data.csv", "intermediate.txt", "untracked.txt")]
        artifacts = []
        for i, path in enumerate(paths):
            path.write_text(f"original {i}")
            if i == 3:
                continue
            record = records / f"write{i}-1.json"
            record.write_text(json.dumps({
                "source_node": f"write{i}", "capability": "file.write_text@1.0.0",
                "output": {"path": str(path), "bytes_written": len(f"original {i}")},
            }))
            artifacts.append({"path": str(record), "node_id": f"write{i}", "commit_state": "committed"})
        result = RunResult(
            run_id="files", execution_status="COMPLETED", output_complete=True,
            outputs={"answer": "See the files.", "files": [str(paths[i]) for i in (0, 1, 3)]},
            artifacts=artifacts, recording={"status": "complete", "path": str(records.parent)},
        )

        class FileAgent:
            async def advance(self, session, _text, *, task_id, turn_id, **kwargs):
                return TaskTurn(
                    session, result, deliverables=store.snapshot(task_id, turn_id, result),
                )

        store = TaskStore(tmp_path / "tasks")
        manager = TaskManager(FileAgent(), store=store)
        task = manager.submit("produce files", IntentSession())
        await manager.drain()
        event = manager.events.get_nowait()
        files = [entry for entry in event.deliverables if entry["kind"] == "file"]
        assert [f["available"] for f in files] == [True, True, False]
        await manager.close()
        store.close()
    # The original directory is now gone, as can happen to /tmp across reboots.
    store = TaskStore(tmp_path / "tasks")
    try:
        manager = TaskManager(None, store=store)
        display = manager.history(task.task_id)
        assert "See the files." in display
        for i, entry in enumerate(files[:2]):
            snapshot = store.files / entry["relative_path"]
            assert str(snapshot) in display
            assert snapshot.read_text() == f"original {i}"
        assert "intermediate.txt" not in display
        assert messages.TASK_DELIVERY_MISSING.format(name="untracked.txt") in display
    finally:
        store.close()


# Exit without running any Python cleanup at three different ledger boundaries. The
# next process drives the CLI console, with a stub that records any attempted execution.
CRASH_WORKER = """
import asyncio
import os
import sys
from pathlib import Path

from karen import IntentSession, TaskTurn
from karen.task import TaskManager
from karen.task.console import run_console
from karen.task.storage import TaskStore

root, phase = Path(sys.argv[1]), sys.argv[2]

class Agent:
    async def advance(self, session, text, **kwargs):
        with (root / "calls").open("a") as calls:
            print(text, file=calls)
        if phase == "running":
            os._exit(17)
        return TaskTurn(session.model_copy(update={"reply": "delivered: " + text}))

    def record_response(self, *args, **kwargs):
        return ()

async def main():
    store = TaskStore(root / "tasks")
    manager = TaskManager(Agent(), store=store)
    session = IntentSession()
    if phase in {"recover", "bare"}:
        task_id = next(iter(manager.tasks))
        commands = iter(("/tasks", f"/task {task_id}", "/exit") if phase == "recover"
                        else (f"/reply {task_id}",))
        async def read():
            try:
                return next(commands)
            except StopIteration:
                raise EOFError from None
        await run_console(manager, session, read_input=read,
                          display=lambda value, **kwargs: print(value), json_output=True)
        store.close()
        return
    task = manager.submit("original operation", session)
    (root / "task-id").write_text(task.task_id)
    if phase != "queued":
        await manager.drain()
    os._exit(17)

asyncio.run(main())
"""


@pytest.mark.parametrize("phase", ["queued", "running", "committed"])
def test_restart_after_process_dies_without_replaying_work(tmp_path, phase):
    command = [sys.executable, "-c", CRASH_WORKER, str(tmp_path)]
    first = subprocess.run([*command, phase], capture_output=True, text=True, timeout=15)
    assert first.returncode == 17, first.stderr
    task_id = (tmp_path / "task-id").read_text()
    recovered = subprocess.run([*command, "recover"], capture_output=True, text=True, timeout=15)
    assert recovered.returncode == 0, recovered.stderr
    assert task_id in recovered.stdout and "original operation" in recovered.stdout
    calls = tmp_path / "calls"
    if phase == "committed":
        assert "delivered: original operation" in recovered.stdout
    else:
        assert messages.TASK_STATE_LABELS["interrupted"] in recovered.stdout
    if phase == "queued":
        assert not calls.exists()
    else:
        assert calls.read_text().splitlines() == ["original operation"]

    def stored_turns():
        with sqlite3.connect(tmp_path / "tasks" / "tasks.sqlite3") as db:
            return db.execute("SELECT turn_id, received_at, state FROM task_turns").fetchall()

    before = stored_turns()
    resumed = subprocess.run([*command, "bare"], capture_output=True, text=True, timeout=15)
    assert resumed.returncode == 0, resumed.stderr
    assert calls.read_text().splitlines() == ["original operation"]
    if phase == "queued":
        assert "delivered: original operation" in resumed.stdout
        assert stored_turns() == [(before[0][0], before[0][1], "completed")]
    else:
        assert stored_turns() == before
        if phase == "running":
            assert "original operation" in resumed.stdout and "外部操作可能已发生" in resumed.stdout
        else:
            assert "用法" in resumed.stdout


async def test_real_graph_file_result_is_saved_by_task_manager(tmp_path):
    from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, ModelBindings
    from dynamic_graph.tools import file_write_text_tool
    from intent_helpers import TaskIntentModel

    from karen import IntentRecognizer, Karen
    from karen.task.resources import ExecutionResources

    guard = ExecutionResources()
    writer = guard.file_tool(file_write_text_tool(), write=True)
    with TemporaryDirectory(dir="/tmp") as directory:
        path = str(Path(directory).resolve() / "report.md")
        files_schema = {"type": "array", "items": {"type": "string"}}
        goal = {
            "objective": "write report",
            "success_criteria": ["report written"],
            "inputs": {"path": path, "content": "original report"},
            "output_schema": {
                "type": "object", "properties": {"files": files_schema},
                "required": ["files"], "additionalProperties": False,
            },
        }
        graph = {
            "dsl_version": "1.0",
            "state_fields": {
                "files": {
                    "value_schema": files_schema, "update_schema": files_schema,
                    "initial": {"literal": []},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0"},
                }
            },
            "nodes": [{
                "id": "write", "kind": "tool",
                "capability": {"name": writer.name, "version": writer.version},
                "input_schema": writer.input_schema, "output_schema": writer.output_schema,
                "input_bindings": {
                    key: {"source": "input", "pointer": "/" + key} for key in ("path", "content")
                },
                "writes": [], "depends_on": [],
            }, {
                "id": "deliver", "kind": "llm", "model_role": "worker",
                "instruction": "List the requested report path as the file delivery.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "input_bindings": {}, "output_schema": goal["output_schema"],
                "writes": [{"field": "files", "output_pointer": "/files"}],
                "depends_on": ["write"],
            }],
            "outputs": {"files": {"source": "state", "field": "files", "pointer": ""}},
        }
        intent = IntentRecognizer(TaskIntentModel([{"decision": {"outcome": "ready", "goal": goal}}]))
        planner = FakeModelClient([{
            "response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": [],
        }])
        engine = DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(planner, FakeModelClient([{"files": [path]}])),
        )
        engine.register_tool(writer)
        store = TaskStore(tmp_path / "tasks")
        try:
            agent = Karen(intent=intent, engine=engine, resources=guard, snapshot_deliveries=store.snapshot)
            manager = TaskManager(agent, store=store)
            task = manager.submit("write report", IntentSession())
            await manager.drain()
            event = manager.events.get_nowait()
            assert event.state == "completed", event.turn.result if event.turn else event
            file = next(item for item in event.deliverables if item["kind"] == "file")
            Path(path).unlink()
            assert (store.files / file["relative_path"]).read_text() == "original report"
            assert "report.md" in manager.history(task.task_id)
            await manager.close()
        finally:
            store.close()
