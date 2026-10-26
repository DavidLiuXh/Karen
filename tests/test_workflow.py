"""Reusable workflow behavior with recorded runs and isolated fake models/tools."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    FakeModelClient,
    GoalSpec,
    GraphSpec,
    ModelBindings,
    ModelResponse,
    ToolDefinition,
)

from karen import IntentSession, Karen
from karen.capabilities import all_capabilities_policy
from karen.intent import InputRouting
from karen.intent.recognizer import IntentRecognizer
from karen.observability import ObservedModel, Observer
from karen.observability.viewer import TraceStore
from karen.workflow import CompletedWorkflowRun, ReusableWorkflows, WorkflowRequest
from karen.workflow.contracts import Proposal
from karen.workflow.templates import parameterized_graph

DAY = "2026-10-24"
TEXT = {"type": "string"}
OBJECT = {
    "type": "object",
    "properties": {"value": TEXT},
    "required": ["value"],
    "additionalProperties": False,
}
ANSWER = {
    "type": "object",
    "properties": {"answer": TEXT},
    "required": ["answer"],
    "additionalProperties": False,
}


def graph():
    return GraphSpec.model_validate(
        {
            "dsl_version": "1.0",
            "state_fields": {
                "value": {
                    "value_schema": TEXT,
                    "update_schema": TEXT,
                    "initial": {"literal": ""},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0"},
                },
                "answer": {
                    "value_schema": TEXT,
                    "update_schema": TEXT,
                    "initial": {"literal": ""},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0"},
                },
            },
            "nodes": [
                {
                    "id": "fetch",
                    "kind": "tool",
                    "capability": {"name": "demo.fetch", "version": "1.0.0"},
                    "input_schema": OBJECT,
                    "input_bindings": {"value": {"source": "input", "pointer": "/date"}},
                    "output_schema": OBJECT,
                    "writes": [{"field": "value", "output_pointer": "/value"}],
                    "depends_on": [],
                },
                {
                    "id": "compose",
                    "kind": "llm",
                    "model_role": "worker",
                    "instruction": "Return the supplied value as answer.",
                    "input_schema": OBJECT,
                    "input_bindings": {
                        "value": {"source": "state", "field": "value", "pointer": ""}
                    },
                    "output_schema": ANSWER,
                    "writes": [{"field": "answer", "output_pointer": "/answer"}],
                    "depends_on": ["fetch"],
                },
            ],
            "outputs": {"answer": {"source": "state", "field": "answer", "pointer": ""}},
        }
    )


def proposal():
    return {
        "reusable": True,
        "reason": "Repeat fetching and summarizing a daily value",
        "name": "Daily value",
        "purpose": "Fetch a daily value and summarize it",
        "applicability": ["One daily value"],
        "exclusions": ["Additional delivery destinations"],
        "parameters": [
            {
                "name": "date",
                "description": "Local calendar date",
                "schema": TEXT,
                "source_value": DAY,
                "input_field": "date",
                "sites": [],
                "default_rule": "today",
            }
        ],
    }


class WorkflowModel:
    def __init__(self):
        self.requests = []
        self.proposal = proposal()
        self.verification = {
            "generalizable": True,
            "reason": "Inputs separated",
            "criteria": [
                {
                    "criterion_id": "date",
                    "verdict": "supported",
                    "evidence": ["/evidence/artifacts/0/output/value"],
                    "reason": "Recorded tool value matches requested date",
                }
            ],
        }
        self.match = None
        self.gate = None

    async def generate(self, request):
        self.requests.append(request)
        if self.gate is not None and request.role == "workflow_extract":
            await self.gate.wait()
        if request.role == "workflow_extract":
            return ModelResponse(deepcopy(self.proposal))
        if request.role == "workflow_verify":
            return ModelResponse(deepcopy(self.verification))
        if request.role == "workflow_match":
            item = request.input_data["candidates"][0]
            return ModelResponse(
                self.match
                or {
                    "workflow_id": item["workflow_id"],
                    "compatible": True,
                    "reason": "Same task",
                    "parameters": {},
                    "questions": {},
                }
            )
        raise AssertionError(request.role)


async def source_run(tmp_path, *, side_effect=False):
    calls = []

    async def fetch(data, context):
        calls.append(deepcopy(data))
        return data

    executor = FakeModelClient([{"answer": DAY}, {"answer": "2026-10-25"}])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path / "runs"), models=ModelBindings(executor, executor)
    )
    engine.register_tool(
        ToolDefinition(
            "demo.fetch", "1.0.0", "Fetch value", OBJECT, OBJECT, fetch, read_only=not side_effect
        )
    )
    goal = GoalSpec(
        request_id="source",
        objective="Fetch daily value",
        inputs={"date": DAY},
        success_criteria=[{"id": "date", "description": "Return requested date"}],
        output_schema=ANSWER,
    )
    result = await engine.run_graph(
        goal=goal, graph_spec=graph(), policy=all_capabilities_policy(engine)
    )
    assert result.execution_status == "COMPLETED", result
    return engine, goal, result, calls, executor


def request(engine, *, pending=None, text="Fetch daily value"):
    return WorkflowRequest(
        text=text,
        request_id="new",
        conversation_id="conversation",
        received_at=datetime(2026, 10, 24, 17, tzinfo=UTC),
        timezone="Asia/Shanghai",
        policy=all_capabilities_policy(engine),
        capabilities=[f"{c.name}@{c.version}" for c in engine.list_capabilities()],
        pending=pending,
    )


async def discover(tmp_path, *, model=None, observer=None, side_effect=False):
    engine, goal, result, calls, executor = await source_run(tmp_path, side_effect=side_effect)
    model = model or WorkflowModel()
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows",
        runs_dir=engine.config.runs_dir,
        model=ObservedModel(model, observer) if observer else model,
        observer=observer,
    )
    await workflows.start()
    trace_id = uuid4().hex
    with workflows.observer.span("turn", trace_id=trace_id, request_id="source"):
        receipt = workflows.submit(
            CompletedWorkflowRun(
                text="Fetch today daily value",
                messages=[],
                goal=goal,
                result=result,
                source_trace_id=trace_id,
            )
        )
    assert receipt["accepted"]
    await workflows._queue.join()
    return workflows, engine, model, calls, executor


async def test_discovery_restart_match_and_real_prepared_execution(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    workflows, engine, model, calls, executor = await discover(tmp_path, observer=observer)
    try:
        resolution = await workflows.resolve(request(engine))
        assert resolution.status == "matched"
        assert resolution.parameters == {"date": "2026-10-25"}
        assert resolution.goal.context["timezone"] == "Asia/Shanghai"
        assert resolution.goal.context.get("memory") is None
        assert resolution.goal.parent_run_id is None
        result = await engine.run_graph(
            goal=resolution.goal,
            graph_spec=resolution.graph_spec,
            policy=all_capabilities_policy(engine),
        )
        assert result.outputs["answer"] == "2026-10-25"
        assert calls == [{"value": DAY}, {"value": "2026-10-25"}]
        assert len(executor.requests) == 2 and all(r.role == "worker" for r in executor.requests)
        workflows.record_outcome(
            workflow_id=resolution.workflow_id,
            version=resolution.version,
            result=result,
            request_id="new",
        )
    finally:
        await workflows.close()
        await observer.close()
    store = TraceStore(tmp_path / "observability")
    listing = store.tasks()["workflows"]
    assert listing["definitions"][0]["status"] == "enabled"
    assert listing["definitions"][0]["uses"] == 1
    assert all(j["state"] == "completed" for j in listing["jobs"])
    source_trace = next((tmp_path / "observability" / "traces").glob("*.jsonl"))
    events = [
        e
        for p in source_trace.parent.glob("*.jsonl")
        for e in map(json.loads, p.read_text().splitlines())
    ]
    assert {
        "workflow.discovery",
        "workflow.extraction",
        "workflow.validation",
        "workflow.saved",
        "model.request",
    } <= {e["event_type"] for e in events}
    source_id = next(e["trace_id"] for e in events if e.get("stage") == "turn")
    assert any(e["event_type"] == "workflow.saved" for e in store.trace(source_id)["events"])
    # Reopen definitions; restart does not require the source task or conversation.
    reopened = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=tmp_path / "runs", model=WorkflowModel()
    )
    await reopened.start()
    try:
        assert (await reopened.resolve(request(engine))).status == "matched"
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "case", ["unknown", "wrong_pointer", "missing_criterion", "stale_instruction"]
)
async def test_insufficient_proof_remains_candidate(tmp_path, case):
    model = WorkflowModel()
    if case == "unknown":
        model.verification["criteria"][0]["verdict"] = "unknown"
    elif case == "wrong_pointer":
        model.verification["criteria"][0]["evidence"] = ["/does_not_exist"]
    elif case == "missing_criterion":
        model.verification["criteria"] = []
    else:
        model.proposal["parameters"][0]["source_value"] = "wrong-source"
    workflows, engine, _, _, _ = await discover(tmp_path, model=model)
    try:
        with workflows.storage.connection() as db:
            assert db.execute("SELECT status FROM workflows").fetchone()[0] == "candidate"
        assert (await workflows.resolve(request(engine))).status == "no_match"
    finally:
        await workflows.close()


async def test_missing_parameter_then_followup_binds_same_workflow(tmp_path):
    model = WorkflowModel()
    model.proposal["parameters"][0]["default_rule"] = "none"
    workflows, engine, _, _, _ = await discover(tmp_path, model=model)
    try:
        definition = workflows.storage.candidates("daily value")[0]
        model.match = {
            "workflow_id": definition.workflow_id,
            "compatible": True,
            "reason": "Date needed",
            "parameters": {},
            "questions": {"date": "Which date?"},
        }
        first = await workflows.resolve(request(engine))
        assert first.status == "needs_parameters" and first.questions == ["Which date?"]
        pending = {
            "workflow_id": first.workflow_id,
            "version": first.version,
            "parameters": first.parameters,
        }
        model.match["parameters"] = {"date": "2026-10-25"}
        model.match["questions"] = {}
        second = await workflows.resolve(request(engine, pending=pending, text="Tomorrow"))
        assert second.status == "matched" and second.workflow_id == first.workflow_id
    finally:
        await workflows.close()


@pytest.mark.parametrize(
    "case",
    [
        "authorization",
        "side_effect_authorization",
        "version",
        "corrupt",
        "extra_parameter",
        "incompatible",
    ],
)
async def test_unsafe_or_incompatible_match_never_executes(tmp_path, case):
    workflows, engine, model, calls, _ = await discover(tmp_path, side_effect=True)
    try:
        req = request(engine)
        if case == "authorization":
            req.policy.allowed_tools = []
        elif case == "side_effect_authorization":
            req.policy.allowed_side_effect_tools = []
        elif case == "version":
            req.capabilities = []
        elif case == "corrupt":
            definition_path = next((tmp_path / "workflows" / "definitions").rglob("*.json"))
            definition_path.write_text("{}")
        else:
            definition = workflows.storage.candidates("daily value")[0]
            model.match = {
                "workflow_id": definition.workflow_id,
                "compatible": case != "incompatible",
                "reason": "Different scope" if case == "incompatible" else "",
                "parameters": {"undeclared": "value"} if case == "extra_parameter" else {},
                "questions": {},
            }
        assert (await workflows.resolve(req)).status == "no_match"
        assert len(calls) == 1
    finally:
        await workflows.close()


async def test_background_does_not_block_and_close_drains(tmp_path):
    engine, goal, result, _, _ = await source_run(tmp_path)
    model = WorkflowModel()
    model.gate = asyncio.Event()
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=engine.config.runs_dir, model=model
    )
    await workflows.start()
    assert workflows.submit(
        CompletedWorkflowRun(
            text="daily value", messages=[], goal=goal, result=result, source_trace_id=uuid4().hex
        )
    )["accepted"]
    await asyncio.sleep(0.03)
    closing = asyncio.create_task(workflows.close())
    await asyncio.sleep(0.03)
    assert not closing.done()
    model.gate.set()
    await closing
    assert workflows.storage.candidates("daily value")
    assert not workflows.submit(
        CompletedWorkflowRun(
            text="daily value", messages=[], goal=goal, result=result, source_trace_id=uuid4().hex
        )
    )["accepted"]


async def test_source_hash_failure_and_duplicate_source_are_observable(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    workflows, engine, _, _, _ = await discover(tmp_path, observer=observer)
    try:
        _, goal, result, _, _ = await source_run(tmp_path / "second")
        # A supplied recording cannot escape the configured source directory.
        assert workflows.submit(
            CompletedWorkflowRun(
                text="daily value",
                messages=[],
                goal=goal,
                result=result,
                source_trace_id=uuid4().hex,
            )
        )["accepted"]
        await workflows._queue.join()
        with workflows.storage.connection() as db:
            assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 1
    finally:
        await workflows.close()
        await observer.close()
    assert any(
        j["state"] == "failed" for j in TraceStore(observer.root_dir).tasks()["workflows"]["jobs"]
    )


@pytest.mark.parametrize("case", ["wrong_source", "extra_site", "duplicate", "instruction"])
def test_parameter_promotion_has_strict_structural_boundary(case):
    goal = GoalSpec(objective="daily value", inputs={"date": DAY})
    p = proposal()
    spec = graph()
    if case == "wrong_source":
        p["parameters"][0]["source_value"] = "not-source"
    elif case == "extra_site":
        p["parameters"][0]["sites"] = [{"kind": "state", "owner": "value", "binding": ""}]
    elif case == "duplicate":
        p["parameters"] *= 2
    else:
        spec.nodes[1].instruction += DAY
    with pytest.raises((ValueError, KeyError)):
        parameterized_graph(goal, spec, Proposal.model_validate(p))


class FastIntent:
    prepare_session = staticmethod(IntentRecognizer.prepare_session)

    def __init__(self, goal):
        self.goal = goal
        self.calls = 0
        self.cancel = False

    async def classify(self, session, text):
        if self.cancel:
            return InputRouting(
                input_types=["task_control"],
                handling="cancel",
                task_relation="continue",
                reason="Cancel",
            )
        return InputRouting(
            input_types=["task_request"],
            handling="assess",
            task_relation="continue" if session.questions else "new",
            reason="Task request",
        )

    async def advance(self, session, text, **kwargs):
        self.calls += 1
        if self.cancel:
            return session.model_copy(update={"reply": "Cancelled"})
        return session.model_copy(update={"goal": self.goal})


async def test_agent_fast_path_parameter_dialogue_and_raw_memory_capture(tmp_path):
    model = WorkflowModel()
    model.proposal["parameters"][0]["default_rule"] = "none"
    workflows, engine, _, calls, _ = await discover(tmp_path, model=model)
    definition = workflows.storage.candidates("daily value")[0]
    model.match = {
        "workflow_id": definition.workflow_id,
        "compatible": True,
        "reason": "",
        "parameters": {},
        "questions": {"date": "Which date?"},
    }
    intent = FastIntent(definition.goal_template)
    captured = []

    class Memory:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def foreground(self):
            yield

        def submit(self, event):
            captured.append(event)
            return None

        async def recall(self, query):
            return None

    agent = Karen(intent=intent, engine=engine, workflows=workflows, memory=Memory())
    try:
        first = await agent.advance(IntentSession(timezone="Asia/Shanghai"), "Fetch daily value")
        assert first.session.questions == ("Which date?",)
        assert first.result is None and intent.calls == 0
        model.match.update(parameters={"date": "2026-10-25"}, questions={})
        second = await agent.advance(first.session, "2026-10-25")
        assert second.result.execution_status == "COMPLETED"
        assert second.session.request_id == first.session.request_id
        assert intent.calls == 0 and len(calls) == 2
        assert {"user_message", "assistant_message", "goal_created", "task_result"} <= {
            e.event_type for e in captured
        }
    finally:
        await workflows.close()


async def test_agent_nonmatch_uses_normal_intent_once(tmp_path):
    workflows, engine, model, calls, _ = await discover(tmp_path)
    definition = workflows.storage.candidates("daily value")[0]
    model.match = {
        "workflow_id": None,
        "compatible": False,
        "reason": "Additional destination",
        "parameters": {},
        "questions": {},
    }
    intent = FastIntent(definition.goal_template.model_copy(update={"request_id": "new"}))
    agent = Karen(intent=intent, engine=engine, workflows=workflows)
    try:
        # Normal planner would be invoked; isolate it from live/fake planner output.
        async def run(**kwargs):
            from dynamic_graph import RunResult

            return RunResult(run_id=uuid4().hex, execution_status="FAILED")

        engine.run = run
        await agent.advance(IntentSession(timezone="UTC"), "Fetch daily value and send it")
        assert intent.calls == 1 and len(calls) == 1
    finally:
        await workflows.close()


async def test_duplicate_submission_does_not_duplicate_definitions(tmp_path):
    workflows, engine, model, _, _ = await discover(tmp_path)
    try:
        with workflows.storage.connection() as db:
            row = db.execute("SELECT source_run_id FROM workflows").fetchone()
        directory = engine.config.runs_dir / row[0]
        from dynamic_graph import RunResult

        result = RunResult.model_validate_json((directory / "result.json").read_text())
        goal = GoalSpec.model_validate_json((directory / "goal.json").read_text())
        workflows.submit(
            CompletedWorkflowRun(
                text="daily value",
                messages=[],
                goal=goal,
                result=result,
                source_trace_id=uuid4().hex,
            )
        )
        await workflows._queue.join()
        with workflows.storage.connection() as db:
            assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 1
        assert sum(r.role == "workflow_extract" for r in model.requests) == 1
    finally:
        await workflows.close()


async def test_queue_capacity_rejection_is_nonblocking(tmp_path):
    engine, goal, result, _, _ = await source_run(tmp_path)
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=engine.config.runs_dir, model=WorkflowModel()
    )
    # Fill before the worker starts, without making disk/model calls.
    workflows._accepting = True
    run = CompletedWorkflowRun(
        text="daily value", messages=[], goal=goal, result=result, source_trace_id=uuid4().hex
    )
    assert all(workflows.submit(run)["accepted"] for _ in range(32))
    assert workflows.submit(run) == {"accepted": False, "reason": "queue_full"}


@pytest.mark.parametrize(
    "reject,quote", [(True, "wrong"), (False, ""), (True, "not actually said")]
)
async def test_only_explicit_source_feedback_pauses_workflow(tmp_path, reject, quote):
    workflows, engine, model, _, _ = await discover(tmp_path)
    definition = workflows.storage.candidates("daily value")[0]
    original = model.generate

    async def generate(request):
        if request.role == "workflow_feedback":
            return ModelResponse(
                {
                    "rejects_previous_result": reject,
                    "evidence_quote": quote,
                    "reason": "Feedback assessment",
                }
            )
        return await original(request)

    model.generate = generate
    try:
        workflows.review_feedback(
            workflow={"id": definition.workflow_id, "version": 1},
            objective="daily value",
            text="That result is wrong",
        )
        await workflows._queue.join()
        with workflows.storage.connection() as db:
            status = db.execute("SELECT status FROM workflows").fetchone()[0]
        assert status == ("paused" if reject and quote == "wrong" else "enabled")
    finally:
        await workflows.close()


async def test_saved_definition_is_immutable_and_publication_failure_not_executable(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    workflows, engine, _, _, _ = await discover(tmp_path)
    try:
        definition = workflows.storage.candidates("daily value")[0]
        path = tmp_path / "workflows" / "definitions" / definition.workflow_id / "1.json"
        original = path.read_bytes()
        with pytest.raises(FileExistsError):
            workflows.storage.save(definition, "enabled")
        assert path.read_bytes() == original
        orphan = definition.model_copy(
            update={
                "workflow_id": uuid4().hex,
                "source": {**definition.source, "run_id": uuid4().hex},
            }
        )
        connection = workflows.storage.connection

        @contextmanager
        def failed_transaction():
            with connection() as db:
                yield db
                raise OSError("simulated transaction failure")

        # The connection's exception path rolls back before publication.
        monkeypatch.setattr(workflows.storage, "connection", failed_transaction)
        with pytest.raises(OSError):
            workflows.storage.save(orphan, "enabled")
        monkeypatch.setattr(workflows.storage, "connection", connection)
        with pytest.raises(ValueError, match="workflow_unavailable"):
            workflows.storage.load(orphan.workflow_id)
        assert not list(path.parent.glob("*.tmp"))
    finally:
        await workflows.close()


async def test_workflow_pending_can_cancel_without_execution(tmp_path):
    workflows, engine, model, calls, _ = await discover(tmp_path)
    definition = workflows.storage.candidates("daily value")[0]
    agent = Karen(intent=FastIntent(definition.goal_template), engine=engine, workflows=workflows)
    try:
        from karen.intent.recognizer import ClarificationItem, Message

        pending = IntentSession(
            timezone="UTC",
            messages=(Message(role="user", content="daily value"),),
            workflow_pending={
                "workflow_id": definition.workflow_id,
                "version": 1,
                "parameters": {},
            },
            clarification_items=(
                ClarificationItem(question_id="date", text="Which date?", asked_after=1),
            ),
        )
        agent.intent.cancel = True
        turn = await agent.advance(pending, "Cancel")
        assert turn.response == "Cancelled" and turn.result is None and len(calls) == 1
    finally:
        await workflows.close()


def test_existing_input_sites_and_literal_sites_use_one_binding_contract():
    goal = GoalSpec(objective="Daily value", inputs={"date": DAY})
    spec = graph()
    p = proposal()
    p["parameters"][0]["sites"] = [{"kind": "node", "owner": "fetch", "binding": "value"}]
    promoted = parameterized_graph(goal, spec, Proposal.model_validate(p))
    assert promoted.nodes[0].input_bindings["value"].pointer == "/workflow_parameters/date"
    # The same promotion works when the source planner used a literal instead.
    from dynamic_graph.graph.spec import LiteralBinding

    spec.nodes[0].input_bindings["value"] = LiteralBinding(literal=DAY)
    promoted = parameterized_graph(goal, spec, Proposal.model_validate(p))
    assert promoted.nodes[0].input_bindings["value"].pointer == "/workflow_parameters/date"


@pytest.mark.parametrize("truncated", [False, True])
async def test_model_contract_recovery_is_bounded_and_expands_only_truncation(tmp_path, truncated):
    from dynamic_graph import ModelCallError

    from karen.workflow.contracts import Match

    response = {
        "workflow_id": None,
        "compatible": False,
        "reason": "No match",
        "parameters": {},
        "questions": {},
    }
    model = FakeModelClient(
        [ModelCallError("MODEL_RESPONSE_TRUNCATED", "limit") if truncated else {}, response]
    )
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=tmp_path / "runs", model=model
    )
    match = await workflows._ask("workflow_match", "Match", {}, Match)
    assert not match.compatible and len(model.requests) == 2
    assert model.requests[1].max_output_tokens == (16384 if truncated else 8192)


async def test_reuse_business_failure_pauses_template_without_reexecuting(tmp_path):
    workflows, engine, model, calls, _ = await discover(tmp_path)
    try:
        resolution = await workflows.resolve(request(engine))
        result = await engine.run_graph(
            goal=resolution.goal,
            graph_spec=resolution.graph_spec,
            policy=all_capabilities_policy(engine),
        )
        model.verification["criteria"][0]["verdict"] = "failed"
        workflows.record_outcome(
            workflow_id=resolution.workflow_id,
            version=resolution.version,
            result=result,
            request_id="new",
        )
        await workflows._queue.join()
        with workflows.storage.connection() as db:
            row = db.execute("SELECT status,uses FROM workflows").fetchone()
            outcome = json.loads(db.execute("SELECT result FROM outcomes").fetchone()[0])
        assert row["status"] == "paused" and row["uses"] == 1
        assert outcome["business_acceptance"] == "failed"
        assert len(calls) == 2  # Source and reuse only; no automatic replay.
    finally:
        await workflows.close()


async def test_failed_side_effect_reuse_is_not_replanned(tmp_path):
    workflows, engine, model, calls, _ = await discover(tmp_path, side_effect=True)
    definition = workflows.storage.candidates("daily value")[0]
    intent = FastIntent(definition.goal_template)
    agent = Karen(intent=intent, engine=engine, workflows=workflows)
    planner_calls = []
    original = engine.run_graph

    async def fails(**kwargs):
        result = await original(**kwargs)
        result.execution_status = "FAILED"
        result.output_complete = False
        return result

    async def unexpected_planning(**kwargs):
        planner_calls.append(kwargs)
        raise AssertionError("Must not replay an executed graph")

    engine.run_graph = fails
    engine.run = unexpected_planning
    try:
        turn = await agent.advance(IntentSession(timezone="Asia/Shanghai"), "Fetch daily value")
        assert turn.result.execution_status == "FAILED"
        assert not planner_calls and intent.calls == 0 and len(calls) == 2
    finally:
        await workflows.close()


async def test_no_lexical_overlap_still_has_bounded_semantic_candidates(tmp_path):
    workflows, engine, _, _, _ = await discover(tmp_path)
    try:
        resolution = await workflows.resolve(
            request(engine, text="按上次的办法处理一下这一天的数据")
        )
        assert resolution.status == "matched"
    finally:
        await workflows.close()


async def test_verification_receives_the_prepared_graph_and_matching_input_schema(tmp_path):
    model = WorkflowModel()
    # Source has a literal; prepared graph must expose its promoted reference.
    model.proposal["parameters"][0]["sites"] = []
    workflows, engine, model, _, _ = await discover(tmp_path, model=model)
    try:
        verification = next(r for r in model.requests if r.role == "workflow_verify")
        prepared = verification.input_data["prepared_graph"]
        inputs = verification.input_data["prepared_goal"]["inputs"]
        assert (
            verification.input_data["graph"]["nodes"][0]["input_bindings"]["value"]["pointer"]
            == "/date"
        )
        assert (
            prepared["nodes"][0]["input_bindings"]["value"]["pointer"]
            == "/workflow_parameters/date"
        )
        assert inputs == {"workflow_parameters": {"date": DAY}}
        assert verification.input_data["structural_error"] == ""
        resolution = await workflows.resolve(request(engine))
        assert (
            resolution.goal.input_schema.document()
            == verification.input_data["prepared_goal"]["input_schema"]
        )
    finally:
        await workflows.close()
