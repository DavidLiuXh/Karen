"""Task-only routing for existing execution tests; route-specific tests script real decisions."""

from dynamic_graph import FakeModelClient
from dynamic_graph.models.client import ModelResponse


class ClarityAwareModel(FakeModelClient):
    """Script routing/answers independently of the dedicated clarity gate.

    Gate behavior is tested with explicit real FakeModelClient responses.
    """

    async def generate(self, request):
        if request.role in {"intent_goal_review", "intent_response_review"}:
            self.requests.append(request)
            return ModelResponse(request.input_data["draft"])
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
