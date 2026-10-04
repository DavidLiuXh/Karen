"""Personal fact recall stays independent of historical task collection."""

import pytest
from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings
from dynamic_graph.contracts import default_output_schema
from dynamic_graph.models.client import ModelResponse
from intent_helpers import ClarityAwareModel as FakeModelClient, TaskIntentModel
from test_context import LocalEmbeddings, MemoryModel, event, query

from karen import IntentRecognizer, IntentSession, Karen
from karen.context import ContextMemory
from karen.observability import Observer
from karen.observability.viewer import TraceStore
from karen.prompts import RESPONSE_INSTRUCTION
from karen.response import format_result


class HobbyModel(MemoryModel):
    def __init__(self):
        super().__init__()
        self.kind = "facts"

    async def generate(self, request):
        if request.role == "memory_extract":
            response = await super().generate(request)
            data = request.input_data
            source = next(e for e in data["events"] if e["event_id"] == data["new_event_id"])
            text = source["payload"].get("content", "")
            if source["event_type"] == "user_message" and text.startswith("我喜欢"):
                response.payload["facts"] = [
                    {
                        "candidate_id": "hobby",
                        "fact_key": "interest." + text[3:],
                        "value": text[3:],
                        "text": text,
                        "evidence": [
                            {
                                "event_id": source["event_id"],
                                "pointer": "/payload/content",
                                "quote": text,
                            }
                        ],
                    }
                ]
            return response
        if request.role == "memory_verify":
            self.requests.append(request)
            return ModelResponse(
                {
                    "decisions": [
                        {
                            "candidate_id": f["candidate_id"],
                            "verification": "supported",
                            "operation": "new",
                            "matched_ids": [],
                            "reason": "direct user preference",
                        }
                        for f in request.input_data["candidates"]
                    ]
                }
            )
        return await super().generate(request)


def answer_graph():
    schema = default_output_schema()
    properties = schema["properties"]
    return {
        "response_version": "1.0",
        "outcome": "graph",
        "diagnostics": [],
        "graph": {
            "dsl_version": "1.0",
            "state_fields": {
                name: {
                    "value_schema": field,
                    "update_schema": field,
                    "initial": {"literal": "" if name == "answer" else []},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
                }
                for name, field in properties.items()
            },
            "nodes": [
                {
                    "id": "render",
                    "kind": "llm",
                    "model_role": "worker",
                    "instruction": "依据 goal.context.memory 中的当前长期事实回答用户，遵守 response_instruction。",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "input_bindings": {},
                    "output_schema": schema,
                    "writes": [
                        {"field": name, "output_pointer": "/" + name} for name in properties
                    ],
                    "depends_on": [],
                }
            ],
            "outputs": {
                name: {"source": "state", "field": name, "pointer": ""} for name in properties
            },
        },
    }


async def test_hobbies_survive_restart_answer_directly_and_keep_observable_evidence(tmp_path):
    root = tmp_path / "context"
    service = ContextMemory(root_dir=root, model=HobbyModel(), embeddings=LocalEmbeddings())
    await service.start()
    try:
        for i, text in enumerate(["我喜欢历史和考古", "我喜欢运动，比如骑行。"]):
            await service.flush(service.submit(event(text, request=f"preferences-{i}")))
        # Bulky, unrelated m2 summaries must not crowd personal facts out of rerank.
        for i in range(16):
            await service.flush(
                service.submit(event("独立文件任务" + "资料" * 600, request=f"noise-{i}"))
            )
    finally:
        await service.close()

    observer = Observer(tmp_path / "observability")
    await observer.start()
    backend = HobbyModel()
    restarted = ContextMemory(
        root_dir=root, model=backend, embeddings=LocalEmbeddings(), observer=observer
    )
    await restarted.start()
    answer = "我记得你喜欢历史和考古，也喜欢运动，比如骑行。"
    outputs = {
        "answer": answer,
        "evidence": [{"source": "internal-memory-id", "text": "用户直接陈述"}],
        "limitations": [],
    }
    intent_model = TaskIntentModel(
        [
            {
                "decision": {
                    "outcome": "ready",
                    "goal": {
                        "objective": "回答用户当前有哪些爱好",
                        "success_criteria": ["列出记忆中当前有效的爱好"],
                    },
                }
            }
        ]
    )
    executor = FakeModelClient([answer_graph(), outputs])
    agent = Karen(
        intent=IntentRecognizer(intent_model, observer=observer),
        engine=DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp_path / "runs"),
            models=ModelBindings(executor, executor),
        ),
        memory=restarted,
        observer=observer,
    )
    try:
        turn = await agent.advance(IntentSession(timezone="Asia/Shanghai"), "我有哪些爱好？")
        assert turn.result.execution_status == "COMPLETED" and turn.result.output_complete, (
            turn.result.diagnostics
        )
        assert not turn.session.questions
        recalled = turn.session.goal.context["memory"]
        assert recalled["collection"] is None
        assert recalled["status"] == "ok" and recalled["m2"] == []
        assert {h["memory"]["value"] for h in recalled["m1"] if not h["evidence_only"]} == {
            "历史和考古",
            "运动，比如骑行。",
        }
        assert recalled["history"]["messages"] == []
        assert turn.session.goal.context["response_instruction"] == RESPONSE_INSTRUCTION
        assert RESPONSE_INSTRUCTION in intent_model.assessments[0].system_instruction
        assert format_result(turn.result) == answer
        assert turn.result.outputs == outputs
        agent.record_response(turn.session, answer)
        await restarted.flush()
    finally:
        await restarted.close()
        await observer.close()
    trace = TraceStore(observer.root_dir).trace(turn.trace_id)
    execution = next(e for e in trace["events"] if e["event_type"] == "execution.result")
    assert execution["data"]["outputs"] == outputs
    assert any(e["event_type"] == "memory.recalled" for e in trace["events"])


async def test_fact_list_is_not_limited_to_five_roots(tmp_path):
    model = HobbyModel()
    service = ContextMemory(root_dir=tmp_path, model=model, embeddings=LocalEmbeddings())
    await service.start()
    try:
        for hobby in ["历史", "考古", "骑行", "游泳", "摄影", "音乐"]:
            await service.flush(service.submit(event("我喜欢" + hobby, request=hobby)))
        result = await service.recall(query("我有哪些爱好？"))
        assert len(result.m1) == 6 and result.m2 == [] and result.collection is None
        assert result.status == "ok"
    finally:
        await service.close()


@pytest.mark.parametrize(
    "change,state", [("我搬到上海", "superseded"), ("我住在上海", "conflicted")]
)
async def test_fact_queries_keep_current_versions_and_conflict_bundles(tmp_path, change, state):
    model = MemoryModel()
    model.kind = "facts"
    service = ContextMemory(root_dir=tmp_path, model=model, embeddings=LocalEmbeddings())
    await service.start()
    try:
        for text in ["我住在北京", change]:
            await service.flush(service.submit(event(text, request=text)))
        result = await service.recall(query("我住在哪里"))
        old = next(h for h in result.m1 if h.memory.value == "北京")
        assert old.memory.state == state and old.evidence_only
        if state == "superseded":
            assert [h.memory.value for h in result.m1 if not h.evidence_only] == ["上海"]
        else:
            assert all(h.evidence_only for h in result.m1)
        assert result.collection is None
    finally:
        await service.close()
