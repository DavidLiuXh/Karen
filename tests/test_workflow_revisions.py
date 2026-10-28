"""Parameter slots, immutable upgrades, migration and source-backed acceptance."""

import hashlib
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
    ModelResponse,
    ToolDefinition,
)
from dynamic_graph.graph.spec import LiteralBinding
from test_workflow import ANSWER, DAY, OBJECT, TEXT, WorkflowModel, graph, proposal, request

from karen import IntentSession, Karen
from karen.capabilities import all_capabilities_policy
from karen.intent import InputRouting
from karen.intent.recognizer import IntentRecognizer
from karen.observability import Observer
from karen.observability.viewer import TraceStore
from karen.workflow import CompletedWorkflowRun, ReusableWorkflows
from karen.workflow.contracts import Definition, Proposal, Verification
from karen.workflow.prompts import PROMPT_VERSION
from karen.workflow.storage import encode
from karen.workflow.templates import acceptance_supported, parameterized_graph


async def recorded_source(tmp_path, *, spec=None, goal=None):
    calls = []

    async def fetch(data, context):
        calls.append(deepcopy(data))
        return data

    worker = FakeModelClient([{"answer": DAY}, {"answer": "2026-10-27"}])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path / "runs"),
        models=ModelBindings(FakeModelClient(), worker),
    )
    engine.register_tool(
        ToolDefinition("demo.fetch", "1.0.0", "Fetch a value", OBJECT, OBJECT, fetch)
    )
    goal = goal or GoalSpec(
        request_id="origin",
        objective="Fetch daily value",
        inputs={"date": DAY},
        output_schema=ANSWER,
        success_criteria=[{"id": "date", "description": "The displayed value is correct"}],
    )
    spec = spec or graph()
    result = await engine.run_graph(
        goal=goal, graph_spec=spec, policy=all_capabilities_policy(engine)
    )
    assert result.execution_status == "COMPLETED", result.diagnostics
    run = CompletedWorkflowRun(
        text="Fetch today daily value",
        messages=[],
        goal=goal,
        result=result,
        source_trace_id=uuid4().hex,
    )
    return engine, run, calls


async def service(tmp_path, engine, model, observer=None):
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows",
        runs_dir=engine.config.runs_dir,
        model=model,
        observer=observer,
    )
    await workflows.start()
    return workflows


def instruction_proposal():
    p = proposal()
    p["parameters"][0]["text_slots"] = [
        {"kind": "instruction", "owner": "compose", "binding": "", "occurrence": 0}
    ]
    return p


def write_legacy_candidate(root, run):
    definition = Definition(
        workflow_id=uuid4().hex,
        version=1,
        created_at=datetime.now(UTC),
        proposal=Proposal.model_validate(proposal()),
        goal_template=run.goal,
        graph_spec=graph(),
        required_capabilities=["demo.fetch@1.0.0", "builtin.replace@1.0.0"],
        acceptance=Verification(
            generalizable=False, reason="Old unparameterized instruction", criteria=[]
        ),
        source={
            "run_id": run.result.run_id,
            "graph_hash": run.result.graph_hash,
            "request_id": run.goal.request_id,
            "trace_id": run.source_trace_id,
            "prompt_version": "workflow_v2",
        },
    )
    # Keep the source graph, including the historical fixed string.
    definition.graph_spec = type(definition.graph_spec).model_validate_json(
        (Path(run.result.recording["path"]) / "graph.json").read_text()
    )
    data = definition.model_dump(mode="json", by_alias=True)
    data.pop("acceptance_evidence")
    data["proposal"].pop("fixed_inputs")
    for parameter in data["proposal"]["parameters"]:
        parameter.pop("text_slots")
    raw = encode(data)
    directory = root / "definitions" / definition.workflow_id
    directory.mkdir(parents=True)
    path = directory / "1.json"
    path.write_bytes(raw)
    with sqlite3.connect(root / "index.sqlite") as db:
        db.executescript("""CREATE TABLE workflows (
            workflow_id TEXT,version INTEGER,status TEXT,path TEXT,digest TEXT,source_run_id TEXT,
            source_graph_hash TEXT,name TEXT,created_at TEXT,uses INTEGER DEFAULT 0,last_used TEXT,
            PRIMARY KEY(workflow_id,version),UNIQUE(source_run_id,source_graph_hash));
            CREATE VIRTUAL TABLE search USING fts5(workflow_id UNINDEXED,text);""")
        db.execute(
            "INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                definition.workflow_id,
                1,
                "candidate",
                str(path.relative_to(root)),
                hashlib.sha256(raw).hexdigest(),
                run.result.run_id,
                run.result.graph_hash,
                "Daily value",
                definition.created_at.isoformat(),
                0,
                None,
            ),
        )
        db.execute("INSERT INTO search VALUES(?,?)", (definition.workflow_id, "daily value"))
    return definition, path, raw


async def test_instruction_slot_uses_new_structured_data_and_keeps_source_immutable(tmp_path):
    spec = graph()
    spec.nodes[1].instruction += " The target date is " + DAY
    engine, run, calls = await recorded_source(tmp_path, spec=spec)
    model = WorkflowModel()
    model.proposal = instruction_proposal()
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        definition = workflows.storage.candidates("daily value")[0]
        model.match = {
            "workflow_id": definition.workflow_id,
            "compatible": True,
            "reason": "Same goal",
            "parameters": {"date": "2026-10-27"},
            "questions": {},
        }
        resolution = await workflows.resolve(request(engine))
        assert resolution.status == "matched"
        node = resolution.graph_spec.nodes[1]
        assert DAY not in node.instruction and "2026-10-27" not in node.instruction
        assert "{{input._workflow_parameter_date}}" in node.instruction
        assert (
            node.input_bindings["_workflow_parameter_date"].pointer == "/workflow_parameters/date"
        )
        result = await engine.run_graph(
            goal=resolution.goal,
            graph_spec=resolution.graph_spec,
            policy=all_capabilities_policy(engine),
        )
        assert result.execution_status == "COMPLETED"
        assert calls[-1]["value"] == "2026-10-27"
        assert DAY in definition.graph_spec.nodes[1].instruction
        assert "_workflow_parameter_date" not in definition.graph_spec.nodes[1].input_bindings
    finally:
        await workflows.close()


def composed_proposal():
    p = proposal()
    p["parameters"][0]["text_slots"] = [
        {"kind": "node_literal", "owner": "fetch", "binding": "value", "occurrence": 0}
    ]
    p["parameters"].append(
        {
            "name": "directory",
            "description": "Destination directory",
            "schema": TEXT,
            "input_field": "directory",
            "source_value": "/tmp",
            "text_slots": [
                {"kind": "node_literal", "owner": "fetch", "binding": "value", "occurrence": 0}
            ],
        }
    )
    p["fixed_inputs"] = {"open": True}
    return p


def test_composed_literal_and_fixed_behavior_have_separate_contracts():
    spec = graph()
    spec.nodes[0].input_bindings["value"] = LiteralBinding(literal="/tmp/report-" + DAY + ".html")
    goal = GoalSpec(
        objective="Daily value", inputs={"date": DAY, "directory": "/tmp", "open": True}
    )
    p = Proposal.model_validate(composed_proposal())
    compiled = parameterized_graph(goal, spec, p, {"date": "2026-10-27", "directory": "/tmp/new"})
    assert compiled.nodes[0].input_bindings["value"].literal == "/tmp/new/report-2026-10-27.html"
    assert spec.nodes[0].input_bindings["value"].literal == "/tmp/report-" + DAY + ".html"
    assert compiled.nodes[0].input_schema == spec.nodes[0].input_schema
    # A flag represented by a fixed step is not an adjustable parameter.
    bad = composed_proposal()
    bad["fixed_inputs"] = {}
    bad["parameters"].append(
        {
            "name": "open",
            "description": "Unused switch",
            "schema": {"type": "boolean"},
            "input_field": "open",
            "source_value": True,
        }
    )
    with pytest.raises(ValueError, match="unused_parameter"):
        parameterized_graph(goal, spec, Proposal.model_validate(bad))


@pytest.mark.parametrize("case", ["occurrence", "overlap", "reserved_field", "non_string"])
def test_text_slots_cannot_rewrite_arbitrary_instructions(case):
    spec = graph()
    spec.nodes[1].instruction += " Target " + DAY
    goal = GoalSpec(objective="Daily value", inputs={"date": DAY})
    p = instruction_proposal()
    if case == "occurrence":
        p["parameters"][0]["text_slots"][0]["occurrence"] = 1
    elif case == "overlap":
        p["parameters"][0]["text_slots"] *= 2
    elif case == "reserved_field":
        spec.nodes[1].input_bindings["_workflow_parameter_date"] = LiteralBinding(literal=DAY)
    else:
        p["parameters"][0]["source_value"] = 1
        p["parameters"][0]["schema"] = {"type": "integer"}
        goal.inputs["date"] = 1
        goal.input_schema.properties["date"] = type(goal.input_schema).model_validate(
            {"type": "integer"}
        )
    with pytest.raises(ValueError):
        parameterized_graph(goal, spec, Proposal.model_validate(p))


async def test_legacy_candidate_migrates_and_upgrades_without_tools_or_rewriting_old_json(tmp_path):
    spec = graph()
    spec.nodes[1].instruction += " Target " + DAY
    engine, run, calls = await recorded_source(tmp_path, spec=spec)
    old, path, raw = write_legacy_candidate(tmp_path / "workflows", run)
    model = WorkflowModel()
    model.proposal = instruction_proposal()
    workflows = await service(tmp_path, engine, model)
    try:
        await workflows._queue.join()
        assert len(calls) == 1  # Re-review never executes a task tool.
        assert path.read_bytes() == raw
        with workflows.storage.connection() as db:
            rows = [
                tuple(r)
                for r in db.execute("SELECT version,status FROM workflows ORDER BY version")
            ]
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert rows == [(1, "superseded"), (2, "enabled")]
        current = workflows.storage.load(old.workflow_id)
        assert current.version == 2 and current.source["prompt_version"] == PROMPT_VERSION
        assert (
            workflows.storage.load(old.workflow_id, 1, False).validation_errors
            == old.validation_errors
        )
    finally:
        await workflows.close()
    reopened_model = WorkflowModel()
    reopened = await service(tmp_path, engine, reopened_model)
    try:
        await reopened._queue.join()
        assert not reopened_model.requests
    finally:
        await reopened.close()


async def test_current_algorithm_candidate_is_not_rechecked_on_every_start(tmp_path):
    engine, run, _ = await recorded_source(tmp_path)
    model = WorkflowModel()
    model.verification["criteria"][0]["verdict"] = "unknown"
    workflows = await service(tmp_path, engine, model)
    workflows.submit(run)
    await workflows.close()
    second = await service(tmp_path, engine, model)
    try:
        await second._queue.join()
        assert sum(r.role == "workflow_extract" for r in model.requests) == 1
        assert second.storage.status_counts() == {"candidate": 1}
    finally:
        await second.close()


async def test_review_that_rejects_reusability_does_not_repeat_each_start(tmp_path):
    engine, run, _ = await recorded_source(tmp_path)
    write_legacy_candidate(tmp_path / "workflows", run)
    model = WorkflowModel()
    model.proposal["reusable"] = False
    for _ in range(2):
        workflows = await service(tmp_path, engine, model)
        await workflows.close()
    assert sum(r.role == "workflow_extract" for r in model.requests) == 1


async def test_failed_version_publication_preserves_enabled_version_and_skips_orphan(
    tmp_path, monkeypatch
):
    engine, run, _ = await recorded_source(tmp_path)
    workflows = await service(tmp_path, engine, WorkflowModel())
    try:
        workflows.submit(run)
        await workflows._queue.join()
        first = workflows.storage.candidates("daily value")[0]
        second = first.model_copy(
            update={"version": 2, "source": {**first.source, "review_key": "test_revision"}}
        )
        connection = workflows.storage.connection

        @contextmanager
        def fail_commit():
            with connection() as db:
                yield db
                raise OSError("simulated index failure")

        monkeypatch.setattr(workflows.storage, "connection", fail_commit)
        with pytest.raises(OSError):
            workflows.storage.save(second, "enabled")
        monkeypatch.setattr(workflows.storage, "connection", connection)
        assert workflows.storage.load(first.workflow_id).version == 1
        assert workflows.storage.next_version(first.workflow_id) == 3
        third = second.model_copy(update={"version": 3})
        workflows.storage.save(third, "enabled")
        assert workflows.storage.load(first.workflow_id).version == 3
        assert workflows.storage.load(first.workflow_id, 1, False).source == first.source
    finally:
        await workflows.close()


class AcceptanceModel(WorkflowModel):
    def __init__(self):
        super().__init__()
        self.verification["criteria"][0]["verdict"] = "unknown"
        self.quote = "The displayed value is correct."
        self.ids = ["date"]

    async def generate(self, request):
        if request.role == "workflow_feedback":
            self.requests.append(request)
            return ModelResponse(
                {
                    "rejects_previous_result": False,
                    "accepted_criteria": self.ids,
                    "evidence_quote": self.quote,
                    "reason": "Explicitly confirms this criterion",
                }
            )
        if request.role == "workflow_verify" and request.input_data["evidence"].get(
            "user_feedback"
        ):
            self.requests.append(request)
            verification = deepcopy(self.verification)
            verification["criteria"][0].update(
                verdict="supported", evidence=["/evidence/user_feedback/0/quote"]
            )
            return ModelResponse(verification)
        return await super().generate(request)


async def test_source_linked_acceptance_rechecks_and_enables_a_new_version(tmp_path):
    engine, run, calls = await recorded_source(tmp_path)
    model = AcceptanceModel()
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        assert workflows.storage.status_counts() == {"candidate": 1}
        receipt = workflows.review_feedback(
            objective=run.goal.objective,
            text=model.quote,
            source_run_id=run.result.run_id,
            source_event_id="event-approval",
            request_id="approval-request",
        )
        assert receipt["accepted"]
        await workflows._queue.join()
        latest = workflows.storage.latest_source(run.result.run_id)
        definition = workflows.storage.load(latest["workflow_id"])
        assert definition.version == 2
        evidence = definition.acceptance_evidence[0]
        assert evidence["source_event_id"] == "event-approval" and evidence["source_role"] == "user"
        assert evidence["source_run_id"] == run.result.run_id and evidence["criterion_ids"] == [
            "date"
        ]
        assert len(calls) == 1
        # Re-delivery of the same approval cannot publish another version.
        workflows.review_feedback(
            objective=run.goal.objective,
            text=model.quote,
            source_run_id=run.result.run_id,
            source_event_id="event-approval",
            request_id="approval-request",
        )
        await workflows._queue.join()
        assert workflows.storage.load(definition.workflow_id).version == 2
    finally:
        await workflows.close()


@pytest.mark.parametrize(
    "case", ["unknown_source", "wrong_quote", "wrong_criterion", "failed_criterion"]
)
async def test_unlinked_or_unsupported_acceptance_cannot_enable(tmp_path, case):
    engine, run, _ = await recorded_source(tmp_path)
    model = AcceptanceModel()
    if case == "failed_criterion":
        model.verification["criteria"][0]["verdict"] = "failed"
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        if case == "wrong_quote":
            model.quote = "Not said"
        if case == "wrong_criterion":
            model.ids = ["other"]
        workflows.review_feedback(
            objective=run.goal.objective,
            text="The displayed value is correct.",
            source_run_id=uuid4().hex if case == "unknown_source" else run.result.run_id,
        )
        await workflows._queue.join()
        assert workflows.storage.status_counts() == {"candidate": 1}
    finally:
        await workflows.close()


def test_user_quote_cannot_be_used_for_an_unapproved_criterion():
    goal = GoalSpec(
        objective="value", success_criteria=[{"id": "date", "description": "value is correct"}]
    )
    verification = Verification(
        generalizable=True,
        reason="",
        criteria=[
            {
                "criterion_id": "date",
                "verdict": "supported",
                "evidence": ["/evidence/user_feedback/0/quote"],
                "reason": "",
            }
        ],
    )
    evidence = {
        "user_feedback": [{"source_role": "user", "criterion_ids": ["other"], "quote": "Nice"}]
    }
    assert not acceptance_supported(goal, verification, evidence)


def test_invariant_explanatory_text_is_explicit_and_not_replaced():
    spec = graph()
    spec.nodes[1].instruction += " Target " + DAY + ". Historical baseline " + DAY + "."
    p = instruction_proposal()
    p["parameters"][0]["text_slots"].append(
        {
            "kind": "instruction",
            "owner": "compose",
            "occurrence": 1,
            "role": "invariant",
            "reason": "The historical baseline date is fixed independently of the current target.",
        }
    )
    goal = GoalSpec(objective="Daily value", inputs={"date": DAY})
    compiled = parameterized_graph(goal, spec, Proposal.model_validate(p), {"date": "2026-10-27"})
    assert compiled.nodes[1].instruction.count(DAY) == 1
    assert "Target {{input._workflow_parameter_date}}" in compiled.nodes[1].instruction
    p["parameters"][0]["text_slots"][1]["reason"] = ""
    with pytest.raises(ValueError, match="invariant text needs an explanation"):
        Proposal.model_validate(p)


def test_fixed_values_do_not_coerce_boolean_to_integer():
    p = proposal()
    p["fixed_inputs"] = {"open": 1}
    with pytest.raises(ValueError, match="fixed_input_mismatch"):
        parameterized_graph(
            GoalSpec(objective="Daily value", inputs={"date": DAY, "open": True}),
            graph(),
            Proposal.model_validate(p),
        )


@pytest.mark.parametrize("with_run_id", [True, False])
async def test_approval_of_a_normal_completed_task_links_raw_event_and_does_not_leak_to_next_topic(
    tmp_path,
    with_run_id,
):
    from types import SimpleNamespace

    engine, run, calls = await recorded_source(tmp_path)
    model = AcceptanceModel()
    workflows = await service(tmp_path, engine, model)
    workflows.submit(run)
    await workflows._queue.join()
    captured = []

    class Memory:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def foreground(self):
            yield

        def submit(self, event):
            captured.append(event)
            return SimpleNamespace(event_id="raw-" + event.request_id)

        async def recall(self, query):
            return None

    class ReplyIntent:
        prepare_session = staticmethod(IntentRecognizer.prepare_session)

        async def classify(self, session, text):
            return InputRouting(
                input_types=["conversation"],
                handling="respond",
                task_relation="new",
                reason="Acknowledgement",
            )

        async def advance(self, session, text, **kwargs):
            return session.model_copy(update={"reply": "Thanks"})

    agent = Karen(intent=ReplyIntent(), engine=engine, workflows=workflows, memory=Memory())
    try:
        prior = IntentSession(
            timezone="UTC",
            goal=run.goal,
            completed_run_id=run.result.run_id if with_run_id else None,
        )
        turn = await agent.advance(prior, model.quote)
        await workflows._queue.join()
        row = workflows.storage.latest_source(run.result.run_id)
        definition = workflows.storage.load(row["workflow_id"])
        assert definition.version == 2
        assert (
            definition.acceptance_evidence[0]["source_event_id"] == "raw-" + turn.session.request_id
        )
        feedback_count = sum(r.role == "workflow_feedback" for r in model.requests)
        assert feedback_count == 1 and len(calls) == 1
        await agent.advance(turn.session, "A completely unrelated topic")
        await workflows._queue.join()
        assert sum(r.role == "workflow_feedback" for r in model.requests) == feedback_count
    finally:
        await workflows.close()


async def test_review_events_and_versions_are_visible_without_reopening_source_task(tmp_path):
    observer = Observer(tmp_path / "observability")
    await observer.start()
    engine, run, _ = await recorded_source(tmp_path)
    old, _, _ = write_legacy_candidate(tmp_path / "workflows", run)
    with observer.span("turn", trace_id=run.source_trace_id, request_id=run.goal.request_id):
        observer.emit("turn.input", data={"text": "Daily report"})
    workflows = await service(tmp_path, engine, WorkflowModel(), observer)
    await workflows.close()
    await observer.close()
    store = TraceStore(observer.root_dir)
    listing = store.tasks()
    assert listing["tasks"][0]["status"] == "ok"
    assert {(d["version"], d["status"]) for d in listing["workflows"]["definitions"]} == {
        (1, "superseded"),
        (2, "enabled"),
    }
    events = store.trace(run.source_trace_id)["events"]
    assert {"workflow.review", "workflow.validation", "workflow.saved"} <= {
        e["event_type"] for e in events
    }
    assert (
        next(e for e in events if e["event_type"] == "workflow.saved")["data"]["previous_version"]
        == old.version
    )


async def test_structural_feedback_repairs_binding_coverage_once_without_replaying_tools(tmp_path):
    spec = graph()
    spec.nodes[1].instruction += " Target " + DAY
    engine, run, calls = await recorded_source(tmp_path, spec=spec)
    model = WorkflowModel()
    good = instruction_proposal()
    broken = deepcopy(good)
    broken["parameters"][0]["input_field"] = None
    original = model.generate

    async def generate(request):
        if request.role == "workflow_extract":
            model.requests.append(request)
            return ModelResponse(good if request.input_data.get("validation_error") else broken)
        return await original(request)

    model.generate = generate
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        assert workflows.storage.status_counts() == {"enabled": 1}
        assert len(calls) == 1
        attempts = [r for r in model.requests if r.role == "workflow_extract"]
        assert (
            len(attempts) == 2
            and attempts[1].input_data["validation_error"] == "unparameterized_inputs"
        )
        fixed_schema = attempts[0].output_schema["properties"]["fixed_inputs"]
        assert fixed_schema["type"] == "array" and fixed_schema["items"]["oneOf"][0]["properties"][
            "field"
        ]["enum"] == ["date"]
    finally:
        await workflows.close()


async def test_schema_cannot_invent_fixed_input_keys(tmp_path):
    engine, run, _ = await recorded_source(tmp_path)
    model = WorkflowModel()
    original = model.generate
    count = 0

    async def generate(request):
        nonlocal count
        if request.role == "workflow_extract":
            model.requests.append(request)
            count += 1
            result = proposal()
            if count == 1:
                result["fixed_inputs"] = {"invented_url": "https://example.com"}
            return ModelResponse(result)
        return await original(request)

    model.generate = generate
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        assert count == 2 and workflows.storage.status_counts() == {"enabled": 1}
    finally:
        await workflows.close()


async def test_lock_is_acquired_before_any_schema_migration(tmp_path, monkeypatch):
    engine, _, _ = await recorded_source(tmp_path)
    first = await service(tmp_path, engine, WorkflowModel())
    second = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=engine.config.runs_dir, model=WorkflowModel()
    )

    def unexpected_migration():
        raise AssertionError("Schema migration must not run without owning the lock")

    monkeypatch.setattr(second.storage, "start", unexpected_migration)
    try:
        with pytest.raises(BlockingIOError):
            await second.start()
        assert second._lock_fd is None and second._worker is None
    finally:
        await first.close()


async def test_interrupted_migration_rolls_back_schema_and_preserves_definitions(
    tmp_path, monkeypatch
):
    engine, run, _ = await recorded_source(tmp_path)
    _, path, raw = write_legacy_candidate(tmp_path / "workflows", run)
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=engine.config.runs_dir, model=WorkflowModel()
    )
    connection = workflows.storage.connection

    @contextmanager
    def fail_commit():
        with connection() as db:
            yield db
            raise OSError("simulated migration commit failure")

    monkeypatch.setattr(workflows.storage, "connection", fail_commit)
    with pytest.raises(OSError):
        await workflows.start()
    assert workflows._lock_fd is None and workflows._worker is None
    with sqlite3.connect(tmp_path / "workflows/index.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "review_key" not in [r[1] for r in db.execute("PRAGMA table_info(workflows)")]
    assert path.read_bytes() == raw
    monkeypatch.setattr(workflows.storage, "connection", connection)
    await workflows.start()
    await workflows.close()


async def test_old_memory_is_not_sent_to_extraction_or_verification(tmp_path):
    goal = GoalSpec(
        request_id="origin",
        objective="Daily value",
        inputs={"date": DAY},
        output_schema=ANSWER,
        success_criteria=[{"id": "date", "description": "correct value"}],
        context={"memory": {"fact": "UNRELATED_PRIVATE_HISTORY"}},
    )
    engine, run, _ = await recorded_source(tmp_path, goal=goal)
    model = WorkflowModel()
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        assert workflows.storage.status_counts() == {"enabled": 1}
        assert all("UNRELATED_PRIVATE_HISTORY" not in str(r.input_data) for r in model.requests)
    finally:
        await workflows.close()


async def test_semantic_refinement_is_bounded_and_does_not_relax_unknown_acceptance(tmp_path):
    engine, run, _ = await recorded_source(tmp_path)
    model = WorkflowModel()
    original = model.generate
    verifications = 0

    async def generate(request):
        nonlocal verifications
        if request.role == "workflow_verify":
            model.requests.append(request)
            verifications += 1
            result = deepcopy(model.verification)
            result["generalizable"] = verifications > 1
            result["reason"] = (
                "Parameter/fixed behavior mismatch"
                if verifications == 1
                else "Template is now valid"
            )
            result["criteria"][0]["verdict"] = "unknown"
            return ModelResponse(result)
        return await original(request)

    model.generate = generate
    workflows = await service(tmp_path, engine, model)
    try:
        workflows.submit(run)
        await workflows._queue.join()
        assert verifications == 2
        attempts = [r for r in model.requests if r.role == "workflow_extract"]
        assert len(attempts) == 2 and attempts[1].input_data["validation_error"].startswith(
            "generalization_rejected:"
        )
        assert workflows.storage.status_counts() == {"candidate": 1}
    finally:
        await workflows.close()


async def test_large_artifact_keeps_metadata_and_bounded_grounding_instead_of_disappearing(
    tmp_path,
):
    from karen.workflow.evidence import project_artifacts

    source = "X" * 90000 + " owner/example " + "Y" * 90000
    documents = [
        {
            "source_node": "fetch",
            "capability": "demo.fetch@1.0.0",
            "output": {
                "url": "https://example.com/catalog",
                "fetched_at": "2026-10-27T10:00:00Z",
                "content": source,
            },
        },
        {"source_node": "parse", "capability": None, "output": {"name": "owner/example"}},
    ]
    artifacts, coverage = project_artifacts(documents, {"answer": "owner/example"})
    assert len(artifacts) == 2
    output = artifacts[0]["output"]
    assert output["url"] == "https://example.com/catalog"
    assert output["fetched_at"] == "2026-10-27T10:00:00Z"
    assert output["content"]["truncated"] is True
    assert output["content"]["sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert any("owner/example" in excerpt["text"] for excerpt in output["content"]["excerpts"])
    assert coverage["bytes"] <= 96 * 1024
    assert len(encode(artifacts)) < 16000


async def test_unknown_storage_version_is_not_downgraded(tmp_path):
    engine, run, _ = await recorded_source(tmp_path)
    _, path, raw = write_legacy_candidate(tmp_path / "workflows", run)
    with sqlite3.connect(tmp_path / "workflows/index.sqlite") as db:
        db.execute("PRAGMA user_version=999")
    workflows = ReusableWorkflows(
        root_dir=tmp_path / "workflows", runs_dir=engine.config.runs_dir, model=WorkflowModel()
    )
    with pytest.raises(ValueError, match="unsupported_workflow_storage_version"):
        await workflows.start()
    assert workflows._lock_fd is None
    assert path.read_bytes() == raw
    with sqlite3.connect(tmp_path / "workflows/index.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999
