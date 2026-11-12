"""Dynamic graph isolation and actual handler-boundary file lease behavior."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from dynamic_graph import (
    CancellationToken,
    DynamicGraphEngine,
    EngineConfig,
    FakeModelClient,
    GoalSpec,
    GraphSpec,
    ModelBindings,
    ToolDefinition,
)
from dynamic_graph.execution.errors import ToolCallError

from karen.capabilities import all_capabilities_policy
from karen.task.resources import ExecutionResources

TEXT = {"type": "string"}
OBJECT = {
    "type": "object",
    "properties": {"text": TEXT},
    "required": ["text"],
    "additionalProperties": False,
}


def context(name):
    return SimpleNamespace(
        run_id=name,
        node_id="node",
        deadline=time.monotonic() + 10,
        cancellation_token=CancellationToken(),
    )


def tool(handler):
    return ToolDefinition("demo.file", "1.0.0", "Fixture", {}, {}, handler, read_only=False)


async def test_different_files_progress_concurrently_and_same_file_waits_until_run_ends(tmp_path):
    guard = ExecutionResources()
    calls = []

    async def handler(data, ctx):
        calls.append(ctx.run_id)
        return data

    wrapped = guard.file_tool(tool(handler), root=tmp_path, write=True)
    written = asyncio.Event()
    finish = asyncio.Event()

    async def first():
        async with guard.run([]):
            await wrapped.handler({"path": "same"}, context("first"))
            written.set()
            await finish.wait()  # The handler ended; its run still owns the path.

    async def other(path, name):
        async with guard.run([]):
            await wrapped.handler({"path": path}, context(name))

    a = asyncio.create_task(first())
    await written.wait()
    b = asyncio.create_task(other("same", "blocked"))
    c = asyncio.create_task(other("different", "independent"))
    try:
        await asyncio.wait_for(c, 1)
        assert calls == ["first", "independent"] and not b.done()
        finish.set()
        await asyncio.wait_for(asyncio.gather(a, b), 1)
        assert calls[-1] == "blocked"
    finally:
        finish.set()
        await asyncio.gather(a, b, c, return_exceptions=True)


async def test_waiter_cancellation_does_not_release_another_runs_lease(tmp_path):
    guard = ExecutionResources()
    calls = []

    async def handler(data, ctx):
        calls.append(ctx.run_id)

    wrapped = guard.file_tool(tool(handler), root=tmp_path, write=True)
    async with guard.run([]):
        await wrapped.handler({"path": "same"}, context("owner"))

        async def waiter():
            async with guard.run([]):
                await wrapped.handler({"path": "same"}, context("waiter"))

        pending = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert calls == ["owner"]
    async with guard.run([]):
        await wrapped.handler({"path": "same"}, context("after"))
    assert calls == ["owner", "after"]


async def test_symlink_alias_and_cross_resource_cycle_are_detected(tmp_path):
    guard = ExecutionResources()
    (tmp_path / "alias").symlink_to(tmp_path / "a")
    ready = [asyncio.Event(), asyncio.Event()]

    async def handler(data, ctx):
        return data

    wrapped = guard.file_tool(tool(handler), root=tmp_path, write=True)

    async def run(index, first, second):
        async with guard.run([]):
            await wrapped.handler({"path": first}, context(str(index)))
            ready[index].set()
            await ready[1 - index].wait()
            await wrapped.handler({"path": second}, context(str(index)))

    results = await asyncio.wait_for(
        asyncio.gather(run(0, "a", "b"), run(1, "b", "alias"), return_exceptions=True), 1
    )
    assert sum(isinstance(r, ToolCallError) for r in results) == 1
    assert "RESOURCE_WAIT_CYCLE" in str(next(r for r in results if isinstance(r, Exception)))


async def test_unknown_side_effect_uses_exclusive_fallback():
    guard = ExecutionResources()
    info = SimpleNamespace(
        kind="tool", name="unknown", version="1", to_dict=lambda: {"read_only": False}
    )
    entered = asyncio.Event()
    finish = asyncio.Event()
    second = asyncio.Event()

    async def first():
        async with guard.run([info]):
            entered.set()
            await finish.wait()

    async def other():
        async with guard.run([]):
            second.set()

    a = asyncio.create_task(first())
    await entered.wait()
    b = asyncio.create_task(other())
    await asyncio.sleep(0)
    assert not second.is_set()
    finish.set()
    await asyncio.wait_for(asyncio.gather(a, b), 1)
    assert second.is_set()


async def test_issued_file_work_finishes_before_cancelled_run_releases_lease(tmp_path):
    guard = ExecutionResources()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handler(data, ctx):
        entered.set()
        await finish.wait()

    wrapped = guard.file_tool(tool(handler), root=tmp_path, write=True)

    async def run():
        async with guard.run([]):
            await wrapped.handler({"path": "a"}, context("one"))

    pending = asyncio.create_task(run())
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    finish.set()
    result = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    async with guard.run([]):
        await wrapped.handler({"path": "a"}, context("after"))


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_engine_concurrent_runs_keep_state_cancellation_and_artifacts_isolated(
    tmp_path, cancel
):
    entered = set()
    both = asyncio.Event()
    release = asyncio.Event()

    async def handler(data, ctx):
        entered.add(data["text"])
        if len(entered) == 2:
            both.set()
        await release.wait()
        if data["text"] == "one" and not cancel:
            raise ToolCallError("fixture_failure")
        return data

    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path / "runs"),
        models=ModelBindings(FakeModelClient(), FakeModelClient()),
    )
    engine.register_tool(ToolDefinition("demo.read", "1.0.0", "Fixture", OBJECT, OBJECT, handler))
    graph = GraphSpec.model_validate(
        {
            "dsl_version": "1.0",
            "state_fields": {
                "value": {
                    "value_schema": TEXT,
                    "update_schema": TEXT,
                    "initial": {"literal": ""},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0"},
                }
            },
            "nodes": [
                {
                    "id": "read",
                    "kind": "tool",
                    "capability": {"name": "demo.read", "version": "1.0.0"},
                    "input_schema": OBJECT,
                    "output_schema": OBJECT,
                    "input_bindings": {"text": {"source": "input", "pointer": "/text"}},
                    "writes": [{"field": "value", "output_pointer": "/text"}],
                    "depends_on": [],
                }
            ],
            "outputs": {"answer": {"source": "state", "field": "value", "pointer": ""}},
        }
    )
    policy = all_capabilities_policy(engine)
    tokens = [CancellationToken(), CancellationToken()]

    async def run(index, text):
        return await engine.run_graph(
            goal=GoalSpec(
                objective=text,
                inputs={"text": text},
                input_schema=OBJECT,
                output_schema={
                    "type": "object",
                    "properties": {"answer": TEXT},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            ),
            graph_spec=graph,
            policy=policy,
            cancellation_token=tokens[index],
        )

    a, b = asyncio.create_task(run(0, "one")), asyncio.create_task(run(1, "two"))
    try:
        await asyncio.wait_for(both.wait(), 2)
        if cancel:
            tokens[0].cancel()
            first = await asyncio.wait_for(a, 2)
            assert not b.done()
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(a, b), 2)
        assert first.execution_status == ("CANCELLED" if cancel else "FAILED")
        assert second.execution_status == "COMPLETED" and second.outputs["answer"] == "two"
        assert first.run_id != second.run_id
        assert (tmp_path / "runs" / first.run_id / "result.json").exists()
        assert (tmp_path / "runs" / second.run_id / "result.json").exists()
        assert graph.state_fields["value"].initial.literal == ""
    finally:
        release.set()
        await asyncio.gather(a, b, return_exceptions=True)


async def test_real_wrapped_file_tools_propagate_leases_through_langgraph(tmp_path):
    from dynamic_graph.tools import file_write_text_tool

    guard = ExecutionResources()
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path / "runs"),
        models=ModelBindings(FakeModelClient(), FakeModelClient()),
    )
    writer = guard.file_tool(file_write_text_tool(root=tmp_path), root=tmp_path, write=True)
    entered = set()
    both, release = asyncio.Event(), asyncio.Event()
    path_schema = {
        "type": "object",
        "properties": {"path": TEXT},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def hold(data, ctx):
        entered.add(data["path"])
        if len(entered) == 2:
            both.set()
        await release.wait()
        return data

    gate = guard.independent_read(
        ToolDefinition("demo.hold", "1.0.0", "Fixture", path_schema, path_schema, hold)
    )
    engine.register_tool(writer)
    engine.register_tool(gate)
    graph = GraphSpec.model_validate(
        {
            "dsl_version": "1.0",
            "state_fields": {
                "path": {
                    "value_schema": TEXT,
                    "update_schema": TEXT,
                    "initial": {"literal": ""},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0"},
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "kind": "tool",
                    "capability": {"name": writer.name, "version": writer.version},
                    "input_schema": writer.input_schema,
                    "output_schema": writer.output_schema,
                    "input_bindings": {
                        "path": {"source": "input", "pointer": "/path"},
                        "content": {"source": "input", "pointer": "/content"},
                    },
                    "writes": [{"field": "path", "output_pointer": "/path"}],
                    "depends_on": [],
                },
                {
                    "id": "hold",
                    "kind": "tool",
                    "capability": {"name": gate.name, "version": gate.version},
                    "input_schema": path_schema,
                    "output_schema": path_schema,
                    "input_bindings": {"path": {"source": "state", "field": "path", "pointer": ""}},
                    "writes": [],
                    "depends_on": ["write"],
                },
            ],
            "outputs": {"answer": {"source": "state", "field": "path", "pointer": ""}},
        }
    )

    async def run(name):
        async with guard.run(engine.list_capabilities()):
            return await engine.run_graph(
                goal=GoalSpec(
                    objective=name,
                    inputs={"path": str(tmp_path / name), "content": name},
                    output_schema={
                        "type": "object",
                        "properties": {"answer": TEXT},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                ),
                graph_spec=graph,
                policy=all_capabilities_policy(engine),
            )

    a, b = asyncio.create_task(run("one.txt")), asyncio.create_task(run("two.txt"))
    try:
        await asyncio.wait_for(both.wait(), 2)
        assert not a.done() and not b.done()
        assert (tmp_path / "one.txt").read_text() == "one.txt"
        assert (tmp_path / "two.txt").read_text() == "two.txt"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(a, b), 2)
        assert all(r.execution_status == "COMPLETED" for r in results)
    finally:
        release.set()
        await asyncio.gather(a, b, return_exceptions=True)


async def test_shared_read_leases_delay_a_writer_until_all_readers_finish(tmp_path):
    guard = ExecutionResources()
    ready = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    wrote = asyncio.Event()

    async def read(data, ctx):
        return data

    async def write(data, ctx):
        wrote.set()

    reader = guard.file_tool(tool(read), root=tmp_path)
    writer = guard.file_tool(tool(write), root=tmp_path, write=True)

    async def reading(index):
        async with guard.run([]):
            await reader.handler({"path": "a"}, context(str(index)))
            ready[index].set()
            await release[index].wait()

    async def writing():
        async with guard.run([]):
            await writer.handler({"path": "a"}, context("writer"))

    a, b = asyncio.create_task(reading(0)), asyncio.create_task(reading(1))
    await asyncio.wait_for(asyncio.gather(*(r.wait() for r in ready)), 1)
    c = asyncio.create_task(writing())
    try:
        await asyncio.sleep(0)
        assert not wrote.is_set()
        release[0].set()
        await a
        assert not wrote.is_set()
        release[1].set()
        await asyncio.wait_for(asyncio.gather(b, c), 1)
        assert wrote.is_set()
    finally:
        for event in release:
            event.set()
        await asyncio.gather(a, b, c, return_exceptions=True)


async def test_file_scope_and_independent_read_declaration_do_not_expand_permissions(tmp_path):
    guard = ExecutionResources()
    called = []

    async def handler(data, ctx):
        called.append(True)

    definition = tool(handler)
    with pytest.raises(ValueError):
        guard.independent_read(definition)
    wrapped = guard.file_tool(definition, root=tmp_path, write=True)
    async with guard.run([]):
        with pytest.raises(ToolCallError, match="RESOURCE_PATH_INVALID"):
            await wrapped.handler({"path": "../outside"}, context("escape"))
    assert not called
    assert wrapped.input_schema == definition.input_schema
    assert wrapped.output_schema == definition.output_schema
    assert wrapped.read_only == definition.read_only


async def test_file_ranges_preserve_unicode_and_do_not_return_whole_file(tmp_path):
    import time
    from types import SimpleNamespace

    import pytest
    from dynamic_graph import CancellationToken, DynamicGraphEngine, FakeModelClient, ModelBindings
    from dynamic_graph.execution.errors import ToolCallError
    from jsonschema import validate

    from karen.tools.file_range import READ_RANGE_BYTES, file_read_text_range_tool

    tool = file_read_text_range_tool(tmp_path)
    engine = DynamicGraphEngine(models=ModelBindings(FakeModelClient(), FakeModelClient()))
    engine.register_tool(tool)
    file = tmp_path / "text.txt"
    content = "x" * (READ_RANGE_BYTES - 1) + "资料" + "z" * READ_RANGE_BYTES
    file.write_text(content, encoding="utf-8")
    context = SimpleNamespace(cancellation_token=CancellationToken(), deadline=time.monotonic() + 2)
    args = {"path": str(file)}
    chunks = []
    while True:
        chunk = await tool.handler(args, context)
        validate(chunk, tool.output_schema)
        assert len(chunk["content"].encode()) <= READ_RANGE_BYTES
        chunks.append(chunk["content"])
        if chunk["next_offset_bytes"] is None:
            break
        args["offset_bytes"] = chunk["next_offset_bytes"]
    assert len(chunks) == 3 and "".join(chunks) == content
    with pytest.raises(ToolCallError):
        await tool.handler({"path": str(file), "offset_bytes": READ_RANGE_BYTES}, context)
