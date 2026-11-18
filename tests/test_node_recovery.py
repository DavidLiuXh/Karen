"""Crash recovery through Karen's task identity and the actual DynamicAgentGraph engine."""

import json
import sqlite3
import subprocess
import sys

import pytest

WORKER = """
import asyncio
import json
import os
import sys
from pathlib import Path

from dynamic_graph import DynamicGraphEngine, EngineConfig, FakeModelClient, GoalSpec, ModelBindings, ToolDefinition
from karen import IntentRecognizer, IntentSession, Karen
from karen.intent import InputRouting
from karen.task import TaskManager
from karen.task.console import run_console
from karen.task.storage import TaskStore

root, phase, scenario = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text_schema = {"type": "string"}
schema = {"type": "object", "properties": {"value": text_schema},
          "required": ["value"], "additionalProperties": False}

class Intent:
    prepare_session = staticmethod(IntentRecognizer.prepare_session)

    async def classify(self, session, text):
        assert phase == "start", "Resume must not invoke intent"
        return InputRouting(input_types=["task_request"], handling="assess",
                            task_relation="new", reason="Scripted test")

    async def advance(self, session, text, **kwargs):
        return session.model_copy(update={"goal": GoalSpec(
            objective=text, request_id=session.request_id, output_schema=schema,
        )})

async def first(data, context):
    with (root / "calls").open("a") as log:
        print("first", file=log)
    return {"value": "committed value"}

async def second(data, context):
    with (root / "calls").open("a") as log:
        print("second", file=log)
    if scenario == "effect":
        with (root / "external").open("a") as output:
            print(data["value"], file=output)
    if phase == "start":
        os._exit(33)
    return {"value": data["value"] + " and resumed"}

def field():
    return {"value_schema": text_schema, "update_schema": text_schema, "initial": {"literal": ""},
            "reducer": {"name": "builtin.replace", "version": "1.0.0"}}

def node(name, definition, writes, dependencies, bindings):
    return {"id": name, "kind": "tool", "capability": {"name": definition.name, "version": definition.version},
            "input_schema": definition.input_schema, "output_schema": schema,
            "input_bindings": bindings, "writes": [{"field": writes, "output_pointer": "/value"}],
            "depends_on": dependencies}

async def main():
    first_tool = ToolDefinition("test.first", "1.0.0", "First fixture",
                                {"type": "object", "properties": {}, "additionalProperties": False},
                                schema, first)
    second_tool = ToolDefinition("test.second", "1.0.0", "Second fixture", schema, schema,
                                 second, read_only=scenario != "effect")
    graph = {
        "dsl_version": "1.0", "state_fields": {"saved": field(), "final": field()},
        "nodes": [
            node("first", first_tool, "saved", [], {}),
            node("second", second_tool, "final", ["first"],
                 {"value": {"source": "state", "field": "saved", "pointer": ""}}),
        ],
        "outputs": {"value": {"source": "state", "field": "final", "pointer": ""}},
    }
    planner = FakeModelClient([{"response_version": "1.0", "outcome": "graph",
                                "graph": graph, "diagnostics": []}] if phase == "start" else [])
    engine = DynamicGraphEngine(config=EngineConfig(runs_dir=root / "runs"),
                                models=ModelBindings(planner, FakeModelClient()))
    engine.register_tool(first_tool)
    if phase != "missing":
        engine.register_tool(second_tool)
    store = TaskStore(root / "tasks")
    agent = Karen(intent=Intent(), engine=engine, link_run=store.link_run)
    manager = TaskManager(agent, store=store)
    if phase == "start":
        commands = iter(("/new run experiment",))
    else:
        task_id = next(iter(manager.tasks))
        commands = iter((f"/reply {task_id}",))
    async def read():
        try:
            return next(commands)
        except StopIteration:
            raise EOFError from None
    await run_console(manager, IntentSession(), read_input=read,
                      display=lambda value, **kwargs: print(value, flush=True), json_output=True)
    store.close()

asyncio.run(main())
"""


def turn(root):
    with sqlite3.connect(root / "tasks" / "tasks.sqlite3") as db:
        return db.execute("SELECT task_id, turn_id, run_id, state FROM task_turns").fetchone()


@pytest.mark.parametrize("scenario", ["safe", "effect"])
def test_bare_reply_resumes_safe_nodes_and_blocks_uncertain_side_effects(tmp_path, scenario):
    def worker(phase):
        return subprocess.run([sys.executable, "-c", WORKER, str(tmp_path), phase, scenario],
                              capture_output=True, text=True, timeout=25)

    first = worker("start")
    assert first.returncode == 33, (first.stdout, first.stderr)
    identity = turn(tmp_path)
    assert identity[2] and identity[3] == "running"

    if scenario == "safe":
        missing = worker("missing")
        assert missing.returncode == 0, missing.stderr
        assert "RESUME_CAPABILITIES_CHANGED" in missing.stdout
        assert turn(tmp_path) == (*identity[:3], "interrupted")
        assert (tmp_path / "calls").read_text().splitlines() == ["first", "second"]

    resumed = worker("resume")
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    event = next(json.loads(line) for line in resumed.stdout.splitlines()
                 if json.loads(line)["type"] == "task")
    assert (event["task_id"], event["turn_id"]) == identity[:2]
    calls = (tmp_path / "calls").read_text().splitlines()
    if scenario == "effect":
        assert event["state"] == "interrupted" and "second" in event["answer"]
        assert turn(tmp_path) == (*identity[:3], "interrupted")
        assert (tmp_path / "external").read_text().splitlines() == ["committed value"]
        assert calls == ["first", "second"]
    else:
        assert event["state"] == "completed", event
        assert event["result"]["outputs"] == {"value": "committed value and resumed"}
        assert event["result"]["usage"]["tool_calls"] == 3
        assert event["result"]["usage"]["model_calls"] == 1
        assert turn(tmp_path) == (*identity[:3], "completed")
        assert calls == ["first", "second", "second"]
