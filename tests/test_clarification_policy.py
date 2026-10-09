"""All clarification exits share necessity checks; lookup never grants side effects."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    ModelBindings,
)
from dynamic_graph.contracts import default_output_schema
from dynamic_graph.tools import file_write_text_tool, tavily_search_tool
from test_clarification_state import clarity, gap
from test_execution import graph_response, ready
from test_input_routing import clarify, reply, routing

from karen import IntentRecognizer, IntentSession, Karen
from karen.observability import ObservedModel, Observer


@pytest.mark.parametrize("origin", ["intent", "intent_response", "intent_goal_review", "intent_response_review"])
async def test_every_downstream_question_is_checked_and_its_origin_is_recorded(tmp_path, origin):
    direct = "response" in origin
    reviewed = origin.endswith("review")
    responses = [routing("respond" if direct else "assess", types=["question"] if direct else None), clarity()]
    if reviewed:
        responses.append(reply("草稿") if direct else ready())
    responses.extend([clarify("具体要处理哪个对象？"), clarity(gap("请指定要处理的对象。"))])
    model = FakeModelClient(responses)
    observer = Observer(tmp_path / "traces")
    await observer.start()
    try:
        result = await IntentRecognizer(model, observer=observer).advance(
            IntentSession(), "处理那个对象。", memory_context={"m2": [{"memory": {}}]} if reviewed else None,
        )
    finally:
        await observer.close()
    assert result.questions == ("请指定要处理的对象。",)
    assert result.goal is None and result.reply is None
    events = [json.loads(line) for path in (observer.root_dir / "traces").glob("*.jsonl")
              for line in path.read_text().splitlines()]
    exit_event = next(e for e in events if e["event_type"] == "intent.clarification_decision")
    assert exit_event["data"]["origin"] == origin
    assert exit_event["data"]["gaps"][0]["resolution_source"] == "user"
    assert exit_event["data"]["policy_version"] == "1"


@pytest.mark.parametrize("direct", [True, False])
async def test_unnecessary_downstream_question_is_rejected_then_original_request_is_fulfilled(direct):
    responses = [routing("respond" if direct else "assess", types=["question"] if direct else None), clarity(),
                 clarify("你希望标题多长？"), clarity(), reply("已提供正文") if direct else ready()]
    model = FakeModelClient(responses)
    original = IntentSession()
    result = await IntentRecognizer(model).advance(original, "只给正文，说明设计已完成。")
    assert not result.questions and (result.reply or result.goal)
    assert result.clarification_items == () and original.messages == ()
    assert model.requests[-1].input_data["clarification_review"]["rejected"] is True
    assert model.requests[-1].timeout_seconds < model.requests[2].timeout_seconds


async def test_repeated_rejected_question_fails_closed_instead_of_looping_or_asking_user():
    from dynamic_graph import ModelCallError

    model = FakeModelClient([routing(), clarity(), clarify("可选标题是什么？"), clarity(),
                             clarify("可选标题是什么？")])
    session = IntentSession()
    with pytest.raises(ModelCallError, match="rejected clarification"):
        await IntentRecognizer(model).advance(session, "只输出正文。")
    assert len(model.requests) == 5 and not session.questions and session.messages == ()


def lookup_graph(tool):
    schema = default_output_schema()
    def field(value_schema, initial):
        return {"value_schema": value_schema, "update_schema": value_schema,
                "initial": {"literal": initial},
                "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}}}
    return {"response_version": "1.0", "outcome": "graph", "diagnostics": [], "graph": {
        "dsl_version": "1.0", "state_fields": {
            "search": field(tool.output_schema, {"query": "", "results": []}),
            "lookup": field(schema, {"answer": "", "evidence": [], "limitations": []}),
        }, "nodes": [
            {"id": "lookup_search", "kind": "tool", "capability": {"name": tool.name, "version": tool.version},
             "input_schema": tool.input_schema, "output_schema": tool.output_schema,
             "input_bindings": {"query": {"literal": "Neralis 公开项目"}},
             "writes": [{"field": "search", "output_pointer": ""}], "depends_on": []},
            {"id": "summarize", "kind": "llm", "model_role": "worker", "instruction": "仅汇总查询结果。",
             "input_schema": {"type": "object", "properties": {"source": tool.output_schema},
                              "required": ["source"], "additionalProperties": False},
             "input_bindings": {"source": {"source": "state", "field": "search", "pointer": ""}},
             "output_schema": schema, "writes": [{"field": "lookup", "output_pointer": ""}],
             "depends_on": ["lookup_search"]},
        ], "outputs": {key: {"source": "state", "field": "lookup", "pointer": "/" + key}
                        for key in schema["properties"]},
    }}


@pytest.mark.parametrize("ambiguous", [True, False])
async def test_public_lookup_rechecks_same_request_before_business_execution(tmp_path, ambiguous):
    calls = []
    async def search(data, context):
        calls.append(data)
        return {"query": data["query"], "results": [{"title": "项目目录", "url": "https://example.test/project",
                "content": "同名项目分为甲、乙两个，由不同机构维护。", "score": 1.0}]}
    tool = replace(tavily_search_tool(api_key="fixture"), handler=search)
    before = clarity()
    before["external_information_needed"] = ["核查 Neralis 项目身份。"]
    after = clarity(gap("你指甲机构还是乙机构的同名项目？")) if ambiguous else clarity()
    intent_model = FakeModelClient([routing(), before, ready(), after] + ([] if ambiguous else [ready()]))
    summary = {"answer": "发现同名项目甲和乙。", "evidence": [{"source": "https://example.test/project",
               "text": "两个机构分别维护同名项目。"}], "limitations": []}
    executor = FakeModelClient([lookup_graph(tool), summary] + ([] if ambiguous else [graph_response(), {"draft": "正文"}]))
    observer = Observer(tmp_path / "observation")
    await observer.start()
    engine = DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"),
                                models=ModelBindings(ObservedModel(executor, observer), ObservedModel(executor, observer)))
    engine.register_tool(tool)
    (tmp_path / "private").mkdir()
    engine.register_tool(file_write_text_tool(tmp_path / "private"))
    initial = IntentSession(reference_time_utc=datetime(2026, 10, 9, tzinfo=UTC),
                            user_context={"private_note": "must not be sent to public lookup"})
    agent = Karen(intent=IntentRecognizer(intent_model, observer=observer), engine=engine, observer=observer)
    try:
        turn = await agent.advance(initial, "为 Neralis 项目起草说明。")
    finally:
        await observer.close()
    assert len(calls) == 1
    assert turn.session.request_id == initial.request_id
    assert turn.session.reference_time_utc == initial.reference_time_utc
    assert [m.content for m in turn.session.messages if m.role == "user"] == ["为 Neralis 项目起草说明。"]
    assert intent_model.requests[3].input_data["public_lookup"]["status"] == "completed"
    assert turn.session.public_lookup and initial.public_lookup is None
    saved = [json.loads(p.read_text()) for p in (tmp_path / "runs").glob("*/goal.json")]
    preparation = next(g for g in saved if "questions" in g["inputs"])
    assert "private_note" not in json.dumps(preparation)
    events = [json.loads(line) for path in (observer.root_dir / "traces").glob("*.jsonl")
              for line in path.read_text().splitlines()]
    policy = next(e["data"]["policy"] for e in events if e["event_type"] == "lookup.started")
    assert policy["allowed_tools"] == ["tavily.search@1.0.0"]
    assert policy["allowed_side_effect_tools"] == []
    if ambiguous:
        assert turn.session.questions and turn.result is None and len(executor.requests) == 2
    else:
        assert not turn.session.questions and turn.result.execution_status == "COMPLETED"
        assert turn.session.goal.context["public_lookup"]["run_id"]
        assert len(executor.requests) == 4
    # Session serialization retains evidence for a genuine follow-up; a new task clears it.
    assert IntentSession.model_validate_json(turn.session.model_dump_json()).public_lookup
    assert turn.session.new_request().public_lookup is None


async def test_lookup_respects_callers_tool_authorization_and_does_not_ask_for_the_answer(tmp_path):
    before = clarity()
    before["external_information_needed"] = ["核查公共实体。"]
    model = FakeModelClient([routing(), before, ready()])
    executor = FakeModelClient()
    engine = DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"), models=ModelBindings(executor, executor))
    engine.register_tool(tavily_search_tool(api_key="fixture"))
    turn = await Karen(intent=IntentRecognizer(model), engine=engine).advance(
        IntentSession(), "查询公开信息。", policy=ExecutionPolicy(allowed_tools=[]),
    )
    assert not turn.session.questions and turn.result is None
    assert "PUBLIC_LOOKUP_UNAVAILABLE" in turn.response and "尚未执行" in turn.response
    assert executor.requests == []


async def test_downstream_necessity_review_shares_the_original_deadline(monkeypatch):
    import asyncio

    from dynamic_graph import ModelCallError

    from karen.intent import recognizer

    original_request = recognizer.ModelRequest
    monkeypatch.setattr(recognizer, "ModelRequest", lambda **kwargs: replace(
        original_request(**kwargs), timeout_seconds=0.15,
    ))
    class SlowModel(FakeModelClient):
        async def generate(self, request):
            if request.role == "intent":
                await asyncio.sleep(0.08)
            if request.input_data.get("proposed_clarification"):
                await asyncio.sleep(0.1)
            return await super().generate(request)
    model = SlowModel([routing(), clarity(), clarify("可选标题是什么？"), clarity()])
    session = IntentSession()
    with pytest.raises(ModelCallError) as error:
        await IntentRecognizer(model).advance(session, "仅输出正文。")
    assert error.value.code == "MODEL_TIMEOUT"
    assert session.messages == () and not session.questions


@pytest.mark.parametrize("failure", ["forbidden_write", "tool_failure", "no_tool_call"])
async def test_lookup_failure_never_executes_business_actions_or_becomes_user_ambiguity(tmp_path, failure):
    from dynamic_graph.execution.errors import ToolCallError

    calls = []
    async def search(data, context):
        calls.append(data)
        raise ToolCallError("fixture outage", retryable=False)
    tool = replace(tavily_search_tool(api_key="fixture"), handler=search)
    graph = lookup_graph(tool)
    target = tmp_path / "must-not-exist.txt"
    writer = file_write_text_tool(tmp_path)
    if failure == "forbidden_write":
        graph["graph"]["nodes"][0].update({
            "capability": {"name": writer.name, "version": writer.version},
            "input_schema": writer.input_schema, "output_schema": writer.output_schema,
            "input_bindings": {"path": {"literal": str(target)}, "content": {"literal": "unauthorized"}},
            "writes": [],
        })
    elif failure == "no_tool_call":
        graph["graph"]["nodes"] = graph["graph"]["nodes"][1:]
        graph["graph"]["nodes"][0].update({
            "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "input_bindings": {}, "depends_on": [],
        })
    before = clarity()
    before["external_information_needed"] = ["核查公共资料。"]
    model = FakeModelClient([routing(), before, ready()])
    summary = {"answer": "未经工具核查的自称结果。", "evidence": [], "limitations": []}
    executor = FakeModelClient([graph, summary])
    engine = DynamicGraphEngine(config=EngineConfig(runs_dir=tmp_path / "runs"), models=ModelBindings(executor, executor))
    engine.register_tool(tool)
    engine.register_tool(writer)
    turn = await Karen(intent=IntentRecognizer(model), engine=engine).advance(
        IntentSession(), "查询公开项目后生成说明。", policy=ExecutionPolicy(
            allowed_tools=["tavily.search@1.0.0", "file.write_text@1.0.0"],
            allowed_side_effect_tools=["file.write_text@1.0.0"], max_planning_rounds=1,
        ),
    )
    assert not target.exists() and not turn.session.questions and turn.result is None
    assert "尚未执行" in turn.response
    assert turn.session.public_lookup["status"] == "failed"
    assert len(model.requests) == 3  # No false user clarification or business reassessment.
    if failure == "tool_failure":
        assert len(calls) == 1
    else:
        assert calls == []
    if failure == "no_tool_call":
        assert "PUBLIC_LOOKUP_UNCONFIRMED" in turn.response
