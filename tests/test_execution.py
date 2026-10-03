import json

import pytest
from dynamic_graph import (
    CancellationToken,
    DynamicGraphEngine,
    EngineConfig,
    EvaluatorDefinition,
    ExecutionPolicy,
    FakeModelClient,
    ModelBindings,
    ReducerDefinition,
    ToolDefinition,
)

from karen import IntentRecognizer, IntentSession, Karen


def ready():
    return {
        "decision": {
            "outcome": "ready",
            "goal": {
                "objective": "写一封中文邮件草稿",
                "success_criteria": ["输出中文邮件草稿"],
                "output_schema": {
                    "type": "object",
                    "properties": {"draft": {"type": "string"}},
                    "required": ["draft"],
                    "additionalProperties": False,
                },
            },
        }
    }


def graph_response():
    draft_schema = {"type": "string"}
    output_schema = ready()["decision"]["goal"]["output_schema"]
    return {
        "response_version": "1.0",
        "outcome": "graph",
        "diagnostics": [],
        "graph": {
            "dsl_version": "1.0",
            "state_fields": {
                "draft": {
                    "value_schema": draft_schema,
                    "update_schema": draft_schema,
                    "initial": {"literal": ""},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "kind": "llm",
                    "model_role": "worker",
                    "instruction": "根据目标撰写中文邮件草稿。",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "input_bindings": {},
                    "output_schema": output_schema,
                    "writes": [{"field": "draft", "output_pointer": "/draft"}],
                    "depends_on": [],
                }
            ],
            "outputs": {"draft": {"source": "state", "field": "draft", "pointer": ""}},
        },
    }


async def test_clarification_then_real_engine_execution_and_recording(tmp_path):
    intent_model = FakeModelClient(
        [
            {"decision": {"outcome": "needs_clarification", "questions": ["给谁写？"]}},
            ready(),
        ]
    )
    executor = FakeModelClient([graph_response(), {"draft": "客户您好，设计已经完成。"}])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(executor, executor)
    )
    agent = Karen(intent=IntentRecognizer(intent_model), engine=engine)
    turn = await agent.advance(
        IntentSession(request_id="mail-1", timezone="America/New_York"), "写邮件"
    )
    assert turn.result is None and executor.requests == []
    turn = await agent.advance(turn.session, "给客户", policy=ExecutionPolicy())
    assert turn.result.execution_status == "COMPLETED"
    assert turn.result.output_complete
    assert turn.result.request_id == "mail-1"
    assert turn.result.outputs == {"draft": "客户您好，设计已经完成。"}
    assert len(executor.requests) == 2
    saved_goal = json.loads(next(tmp_path.glob("*/goal.json")).read_text())
    assert saved_goal["objective"] == turn.session.goal.objective
    assert saved_goal["success_criteria"] == [
        {"id": "criterion_1", "description": "输出中文邮件草稿"}
    ]
    assert saved_goal["context"]["conversation"][-1]["content"] == "给客户"
    assert saved_goal["context"]["timezone"] == "America/New_York"


async def test_engine_failure_is_exposed_without_claiming_task_success(tmp_path):
    executor = FakeModelClient([graph_response()])
    agent = Karen(
        intent=IntentRecognizer(FakeModelClient([ready()])),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(executor, executor)
        ),
    )
    turn = await agent.advance(IntentSession(), "写邮件", policy=ExecutionPolicy(max_model_calls=0))
    assert turn.session.goal is not None
    assert turn.result.execution_status == "FAILED"
    assert not turn.result.output_complete
    assert turn.result.diagnostics
    assert executor.requests == []


def register_capabilities(engine, calls):
    node = graph_response()["graph"]["nodes"][0]

    async def write(data, context):
        calls.append(context.node_id)
        return {"draft": "中文邮件草稿"}

    for definition in (ToolDefinition, EvaluatorDefinition):
        engine_method = (
            engine.register_tool if definition is ToolDefinition else engine.register_evaluator
        )
        engine_method(
            definition(
                name="demo.write" if definition is ToolDefinition else "demo.evaluate",
                version="1.0.0",
                description="Return a synthetic draft",
                input_schema=node["input_schema"],
                output_schema=node["output_schema"],
                handler=write,
                read_only=definition is EvaluatorDefinition,
            )
        )
    engine.register_reducer(
        ReducerDefinition(
            name="demo.replace",
            version="1.0.0",
            description="Replace draft",
            value_schema={"type": "string"},
            update_schema={"type": "string"},
            config_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            initial_validator=lambda value: value == "",
            handler=lambda old, update, config: update,
        )
    )


def capability_graph(kind):
    response = graph_response()
    node = response["graph"]["nodes"][0]
    node.pop("instruction")
    node.pop("model_role")
    node.update(
        {
            "kind": kind,
            "capability": {
                "name": "demo.write" if kind == "tool" else "demo.evaluate",
                "version": "1.0.0",
            },
        }
    )
    response["graph"]["state_fields"]["draft"]["reducer"]["name"] = "demo.replace"
    return response


@pytest.mark.parametrize("kind", ["tool", "check"])
async def test_default_policy_authorizes_registered_tools_evaluators_and_reducers(tmp_path, kind):
    planner = FakeModelClient([capability_graph(kind)])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(planner, FakeModelClient())
    )
    agent = Karen(intent=IntentRecognizer(FakeModelClient([ready()])), engine=engine)
    calls = []
    # Registered after Karen construction: the execution policy must see these capabilities.
    register_capabilities(engine, calls)
    turn = await agent.advance(IntentSession(), "生成中文邮件草稿")
    assert turn.result.execution_status == "COMPLETED"
    assert turn.result.outputs == {"draft": "中文邮件草稿"}
    assert calls == ["write"]
    policy = json.loads(next(tmp_path.glob("*/policy.json")).read_text())
    assert policy["allowed_tools"] == ["demo.write@1.0.0"]
    assert policy["allowed_side_effect_tools"] == ["demo.write@1.0.0"]
    assert policy["allowed_evaluators"] == ["demo.evaluate@1.0.0"]
    assert set(policy["allowed_reducers"]) == {
        "demo.replace@1.0.0",
        "builtin.replace@1.0.0",
        "builtin.merge_map_strict@1.0.0",
        "builtin.merge_by_key@1.0.0",
    }


async def test_explicit_restrictive_policy_is_not_expanded(tmp_path):
    planner = FakeModelClient([capability_graph("tool")])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(planner, FakeModelClient())
    )
    calls = []
    register_capabilities(engine, calls)
    agent = Karen(intent=IntentRecognizer(FakeModelClient([ready()])), engine=engine)
    explicit = ExecutionPolicy(max_planning_rounds=1)
    turn = await agent.advance(IntentSession(), "写邮件", policy=explicit)
    assert turn.result.execution_status == "FAILED"
    assert calls == []
    policy = json.loads(next(tmp_path.glob("*/policy.json")).read_text())
    assert policy == explicit.model_dump(mode="json")
    assert explicit.allowed_tools == [] and explicit.allowed_evaluators == []


async def test_explicit_policy_without_side_effect_permission_prevents_execution(tmp_path):
    planner = FakeModelClient([capability_graph("tool")])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(planner, FakeModelClient())
    )
    calls = []
    register_capabilities(engine, calls)
    agent = Karen(intent=IntentRecognizer(FakeModelClient([ready()])), engine=engine)
    policy = ExecutionPolicy(
        allowed_tools=["demo.write@1.0.0"],
        allowed_reducers=["demo.replace@1.0.0"],
        max_planning_rounds=1,
    )
    turn = await agent.advance(IntentSession(), "写邮件", policy=policy)
    assert turn.result.execution_status == "FAILED"
    assert calls == []
    snapshot = json.loads(next(tmp_path.glob("*/capability_snapshot.json")).read_text())
    assert "demo.write" not in {c["name"] for c in snapshot["capabilities"]}
    assert policy.allowed_side_effect_tools == []


@pytest.mark.parametrize("first_status", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_next_task_has_no_previous_conversation_or_results(tmp_path, first_status):
    second_goal = ready()
    second_goal["decision"]["goal"]["objective"] = "写一封新的中文邮件"
    intent_model = FakeModelClient(
        [
            ready(),
            {"decision": {"outcome": "needs_clarification", "questions": ["新邮件的主题是什么？"]}},
            second_goal,
        ]
    )
    responses = []
    if first_status == "COMPLETED":
        responses.extend([graph_response(), {"draft": "第一项任务的结果，不得带入下一任务"}])
    responses.extend([graph_response(), {"draft": "第二项任务的结果"}])
    executor = FakeModelClient(responses)
    agent = Karen(
        intent=IntentRecognizer(intent_model),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(executor, executor)
        ),
    )
    token = CancellationToken()
    if first_status == "CANCELLED":
        token.cancel()
    first = await agent.advance(
        IntentSession(timezone="America/New_York", user_context={"language": "zh-CN"}),
        "第一项任务：写中文邮件",
        policy=ExecutionPolicy(max_model_calls=0) if first_status == "FAILED" else None,
        cancellation_token=token,
    )
    assert first.result.execution_status == first_status
    second = await agent.advance(first.session, "第二项任务：写一封新邮件")
    assert second.result is None
    assert second.session.request_id != first.session.request_id
    assert second.session.timezone == "America/New_York"
    assert second.session.user_context == {"language": "zh-CN"}
    assert intent_model.requests[1].input_data["messages"] == [
        {"role": "user", "content": "第二项任务：写一封新邮件"}
    ]
    final = await agent.advance(second.session, "新产品发布通知")
    assert final.result.execution_status == "COMPLETED"
    assert final.result.outputs == {"draft": "第二项任务的结果"}
    assert final.result.request_id == second.session.request_id
    assert final.result.parent_run_id is None
    conversation = final.session.goal.context["conversation"]
    assert [message["content"] for message in conversation] == [
        "第二项任务：写一封新邮件",
        "新邮件的主题是什么？",
        "新产品发布通知",
    ]
    planner_goal = executor.requests[-2].input_data["goal"]
    assert planner_goal["context"]["conversation"] == conversation
    assert "第一项任务" not in json.dumps(planner_goal, ensure_ascii=False)
    assert first.session.messages[0].content == "第一项任务：写中文邮件"
    assert final.session.goal.objective == "写一封新的中文邮件"


async def test_observation_links_clarification_live_nodes_and_displayed_response(tmp_path):
    import asyncio

    from karen.observability import ObservedModel, Observer
    from karen.observability.viewer import TraceStore

    observer = Observer(tmp_path / "observability")
    await observer.start()
    entered, release = asyncio.Event(), asyncio.Event()

    class Executor(FakeModelClient):
        async def generate(self, request):
            if request.role == "worker":
                entered.set()
                await release.wait()
            return await super().generate(request)

    intent = ObservedModel(
        FakeModelClient(
            [
                {
                    "decision": {
                        "outcome": "needs_clarification",
                        "questions": ["给谁写？"],
                        "reason": "缺少收件人",
                    }
                },
                ready(),
            ]
        ),
        observer,
    )
    executor = ObservedModel(Executor([graph_response(), {"draft": "中文邮件草稿"}]), observer)
    agent = Karen(
        intent=IntentRecognizer(intent, observer=observer),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(executor, executor),
        ),
        observer=observer,
    )
    first = await agent.advance(IntentSession(timezone="UTC"), "写邮件")
    running = asyncio.create_task(agent.advance(first.session, "给客户"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await observer._queue.join()
        store = TraceStore(observer.root_dir)
        tasks = store.tasks()["tasks"]
        current = next(t for t in tasks if t["status"] == "nonterminal")
        detail = store.trace(current["trace_id"])
        assert len(detail["runs"]) == 1
        assert not detail["runs"][0]["manifest"]["terminal"]
        assert detail["runs"][0]["result"] is None
        assert any(e["event_type"] == "node_started" for e in detail["events"])
        assert any(
            e["event_type"] == "decision"
            and e["data"].get("decision", {}).get("reason") == "缺少收件人"
            for e in detail["events"]
        )
        release.set()
        final = await running
        agent.record_response(final.session, "用户实际看到的邮件", trace_id=final.trace_id)
    finally:
        release.set()
        if not running.done():
            await running
        await observer.close()
    detail = TraceStore(observer.root_dir).trace(final.trace_id)
    assert detail["runs"][0]["result"]["output_complete"]
    assert (
        detail["runs"][0]["artifacts"]["artifacts/write-1.json"]["output"]["draft"]
        == "中文邮件草稿"
    )
    assert any(
        e["event_type"] == "response" and e["data"]["text"] == "用户实际看到的邮件"
        for e in detail["events"]
    )
    assert detail["checks"][0]["status"] == "pass"
    assert {t["outcome"] for t in TraceStore(observer.root_dir).tasks()["tasks"]} == {
        "needs_clarification",
        "COMPLETED",
    }


async def test_failed_observation_does_not_fail_real_engine_task(tmp_path, monkeypatch):
    from karen.observability import ObservedModel, Observer

    observer = Observer(tmp_path / "observability")
    await observer.start()

    def disk_failed(*args):
        raise OSError("observation disk offline")

    monkeypatch.setattr(observer, "_append", disk_failed)
    intent = ObservedModel(FakeModelClient([ready()]), observer)
    executor = ObservedModel(
        FakeModelClient([graph_response(), {"draft": "仍然正常完成"}]), observer
    )
    agent = Karen(
        intent=IntentRecognizer(intent, observer=observer),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(executor, executor),
        ),
        observer=observer,
    )
    try:
        turn = await agent.advance(IntentSession(timezone="UTC"), "写中文邮件")
        assert turn.result.execution_status == "COMPLETED"
        assert turn.result.outputs == {"draft": "仍然正常完成"}
        assert list((tmp_path / "runs").glob("*/result.json"))
    finally:
        await observer.close()
    assert observer.write_failures > 0
