"""Task-only routing for existing execution tests; route-specific tests script real decisions."""

from copy import deepcopy

from dynamic_graph import FakeModelClient
from dynamic_graph.models.client import ModelResponse


class ClarityAwareModel(FakeModelClient):
    """Script routing/answers independently of the dedicated clarity gate.

    Gate behavior is tested with explicit real FakeModelClient responses.
    """

    async def generate(self, request):
        if request.role in {"intent_goal_review", "intent_response_review"}:
            self.requests.append(request)
            draft = deepcopy(request.input_data["draft"])
            inputs = request.input_data.get("original_input", request.input_data)
            coverage = [{
                "evidence_id": item["evidence_id"], "disposition": "context_only",
                "reason": "脚本背景；该测试验证交接契约，不验证真实模型的证据取舍。",
            } for item in inputs.get("evidence_to_consider", [])]
            if draft["decision"]["outcome"] == "ready":
                draft["decision"]["goal"]["evidence_coverage"] = coverage
            else:
                draft["evidence_coverage"] = coverage
            return ModelResponse(draft)
        if request.role == "intent_clarity":
            self.requests.append(request)
            return ModelResponse(
                {
                    "known_referents": {},
                    "references": [],
                    "selection_criteria": [],
                    "questions": [],
                    "reason": "No unresolved referents in this scripted routing test",
                }
            )
        return await super().generate(request)


class TaskIntentModel(ClarityAwareModel):
    @property
    def assessments(self):
        return [r for r in self.requests if r.role == "intent"]

    @property
    def classifications(self):
        return [r for r in self.requests if r.role == "intent_router"]

    async def generate(self, request):
        if request.role == "intent_router":
            self.requests.append(request)
            pending = request.input_data["pending_task"] is not None
            return ModelResponse(
                {
                    "input_types": ["task_control" if pending else "task_request"],
                    "handling": "assess",
                    "task_relation": "continue" if pending else "new",
                    "reason": "scripted task test",
                }
            )
        return await super().generate(request)
