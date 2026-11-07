"""Task lifecycle behavior without real model, memory, user directories or tools."""

import asyncio
from types import SimpleNamespace

import pytest
from intent_helpers import NewTaskRouter

from karen import IntentSession, TaskTurn
from karen.intent.recognizer import ClarificationItem, Message
from karen.message import messages
from karen.task import TaskInteraction, TaskManager


class Agent:
    intent = NewTaskRouter()
    def __init__(self):
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.inputs = []

    async def advance(self, session, text, **kwargs):
        self.inputs.append((session, text, kwargs))
        if text == "clarify":
            return TaskTurn(
                session.model_copy(
                    update={
                        "messages": (Message(role="user", content=text),),
                        "clarification_items": (
                            ClarificationItem(question_id="q1", text="Which?", asked_after=1),
                        ),
                    }
                )
            )
        if text == "fail":
            raise ValueError("bad response")
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
            return TaskTurn(session.model_copy(update={"reply": text}))
        finally:
            self.active -= 1


async def next_state(manager, state):
    async with asyncio.timeout(2):
        while True:
            event = await manager.events.get()
            manager.events.task_done()
            if event.state == state:
                return event


async def test_concurrency_bound_and_independent_sessions():
    agent = Agent()
    manager = TaskManager(agent, max_concurrent=2)
    template = IntentSession()
    tasks = [manager.submit(str(i), template) for i in range(4)]
    try:
        await asyncio.sleep(0.01)
        assert agent.active == 2 and [t.state for t in tasks].count("queued") == 2
        assert len({t.session.request_id for t in tasks}) == 4
        agent.release.set()
        await manager.drain()
        assert agent.peak == 2 and all(t.state == "completed" for t in tasks)
        assert template.messages == () and template.reply is None
    finally:
        await manager.close()


async def test_waiting_releases_slot_and_answer_preserves_own_chain():
    agent = Agent()
    manager = TaskManager(agent, max_concurrent=1)
    first = manager.submit("clarify", IntentSession())
    await next_state(manager, "waiting")
    second = manager.submit("other", IntentSession())
    await asyncio.sleep(0.01)
    assert second.state == "running" and first.state == "waiting"
    manager.submit("answer", IntentSession(), task_id=first.task_id)
    assert first.state == "queued"
    agent.release.set()
    await manager.drain()
    answered = next(s for s, text, _ in agent.inputs if text == "answer")
    other = next(s for s, text, _ in agent.inputs if text == "other")
    assert answered.questions == ("Which?",) and not other.questions
    await manager.close()


async def test_cancel_queued_and_running_does_not_launch_queued_work():
    agent = Agent()
    manager = TaskManager(agent, max_concurrent=1)
    first = manager.submit("running", IntentSession())
    await asyncio.sleep(0.01)
    queued = manager.submit("queued", IntentSession())
    manager.cancel(queued.task_id)
    manager.cancel(first.task_id)
    await manager.drain()
    assert [text for _, text, _ in agent.inputs] == ["running"]
    assert first.state == queued.state == "cancelled"
    assert first.token.cancelled and queued.token.cancelled
    assert len(first.turns) == len(queued.turns) == 1
    assert queued.turns[0].text == "queued"
    assert any(m.role == "user" and m.content == "queued" for m in queued.session.messages)
    await manager.close()


async def test_ambiguous_answer_kept_until_explicit_task_selection():
    agent = Agent()
    manager = TaskManager(agent)
    session = IntentSession()
    tasks = [manager.submit("clarify", session) for _ in range(2)]
    await manager.drain()
    ui = TaskInteraction(manager, session)
    assert ui.accept("tomorrow") == messages.TASK_SELECT_OWNER
    assert ui.accept("another") == messages.TASK_SELECT_PENDING
    answer = ui.accept("/use " + tasks[1].task_id)
    assert tasks[1].task_id in answer and ui.pending is None
    agent.release.set()
    await manager.drain()
    assert tasks[0].state == "waiting" and tasks[1].state == "completed"
    assert agent.inputs[-1][1] == "tomorrow"
    assert agent.inputs[-1][0].request_id == tasks[1].session.request_id
    await manager.close()


async def test_failure_isolated_and_close_rejects_new_work():
    agent = Agent()
    manager = TaskManager(agent)
    failed = manager.submit("fail", IntentSession())
    good = manager.submit("good", IntentSession())
    agent.release.set()
    await manager.drain()
    assert failed.state == "failed" and failed.error == "ValueError"
    assert good.state == "completed"
    await manager.close()
    with pytest.raises(ValueError, match="CLOSED"):
        manager.submit("new", IntentSession())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_concurrency(limit):
    with pytest.raises(ValueError):
        TaskManager(SimpleNamespace(), max_concurrent=limit)


async def test_restored_single_channel_question_can_continue_and_be_cancelled():
    agent = Agent()
    manager = TaskManager(agent)
    first = manager.submit("clarify", IntentSession())
    await manager.drain()
    restored = manager.restore_waiting(first.session)
    assert restored.session.request_id == first.session.request_id
    manager.cancel(restored.task_id)
    assert not restored.session.questions
    assert restored.state == "cancelled"
    await manager.close()


async def test_completed_followup_preserves_task_and_previous_round():
    agent = Agent()
    manager = TaskManager(agent)
    agent.release.set()
    first = manager.submit("one", IntentSession())
    await manager.drain()
    second = manager.submit("feedback", IntentSession(), task_id=first.task_id)
    await manager.drain()
    assert second.task_id == first.task_id and first.turns[0].event.turn.response == "one"
    assert agent.inputs[-1][0].reply == "one"
    await manager.close()


async def test_karen_runs_isolated_engines_in_parallel(tmp_path):
    from dynamic_graph import GoalSpec, RunResult

    from karen import Karen
    from karen.intent import InputRouting, IntentRecognizer

    class Intent:
        def __init__(self):
            self.understood = 0

        async def classify(self, session, text):
            return InputRouting(input_types=["task_request"], handling="assess",
                                task_relation="new", reason="Explicit request")

        prepare_session = staticmethod(IntentRecognizer.prepare_session)

        async def advance(self, session, text, **kwargs):
            self.understood += 1
            return session.model_copy(update={"goal": GoalSpec(objective=text)})

    class Engine:
        config = SimpleNamespace(runs_dir=tmp_path)

        def __init__(self):
            self.release = asyncio.Event()
            self.active = 0
            self.peak = 0

        def list_capabilities(self):
            return []

        async def run(self, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await self.release.wait()
                return RunResult(run_id="fixture", execution_status="COMPLETED", output_complete=True,
                                 outputs={"answer": "done"})
            finally:
                self.active -= 1

    engine, intent = Engine(), Intent()
    manager = TaskManager(Karen(intent=intent, engine=engine))
    tasks = [manager.submit(text, IntentSession()) for text in ["one", "two"]]
    try:
        await asyncio.sleep(0.02)
        assert intent.understood == 2 and engine.active == 2
        engine.release.set()
        await manager.drain()
        assert engine.peak == 2 and all(t.state == "completed" for t in tasks)
    finally:
        await manager.close()


async def test_console_reads_a_second_request_before_first_task_finishes():
    from karen.task.console import run_console

    agent = Agent()
    agent.record_response = lambda *args, **kwargs: ()
    manager = TaskManager(agent)
    reads = 0
    displayed = []

    async def read_input():
        nonlocal reads
        reads += 1
        if reads == 1:
            return "one"
        async with asyncio.timeout(2):
            while agent.active < reads - 1:
                await asyncio.sleep(0)
        if reads == 2:
            assert not agent.release.is_set()
            return "/new two"
        agent.release.set()
        raise EOFError

    async with asyncio.timeout(3):
        code = await run_console(manager, IntentSession(), read_input=read_input,
                                 display=lambda text, **kwargs: displayed.append(text))
    assert code == 0 and agent.peak == 2
    assert sum("本轮完成" in text for text in displayed) == 2


async def test_blank_input_does_not_poison_pending_task_selection():
    manager = TaskManager(Agent())
    task = manager.submit("clarify", IntentSession())
    await manager.drain()
    ui = TaskInteraction(manager, IntentSession())
    assert "EMPTY_TASK_INPUT" in ui.accept("   ")
    assert ui.pending is None and task.state == "waiting"
    await manager.close()


async def test_unassigned_input_can_become_a_new_task_without_retyping():
    agent = Agent()
    manager = TaskManager(agent)
    session = IntentSession()
    first = manager.submit("clarify", session)
    await manager.drain()
    ui = TaskInteraction(manager, session)
    assert ui.accept("independent request") == messages.TASK_SELECT_OWNER
    ui.accept("/new")
    assert ui.pending is None and len(manager.tasks) == 2
    agent.release.set()
    await manager.drain()
    assert first.state == "waiting"
    assert agent.inputs[-1][1] == "independent request" and not agent.inputs[-1][0].questions
    await manager.close()


class ConversationAgent:
    """A three-turn conversation that needs its original request and both answers."""

    intent = NewTaskRouter()

    def __init__(self):
        self.inputs = []

    async def advance(self, session, text, **kwargs):
        self.inputs.append((session, text, kwargs))
        if session.completed:
            session = session.next_turn()
        history = (*session.messages, Message(role="user", content=text))
        if text in {"completed", "second detail"}:
            return TaskTurn(session.model_copy(update={"messages": history, "reply": "done"}))
        return TaskTurn(session.model_copy(update={
            "messages": history,
            "clarification_items": (
                ClarificationItem(question_id="next", text="Next detail?", asked_after=len(history)),
            ),
        }))

    def record_response(self, *args, **kwargs):
        return ()


@pytest.mark.parametrize("addressing", ["select", "reply"])
async def test_followup_preserves_task_history_and_round_records(addressing):
    manager = TaskManager(ConversationAgent())
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new completed")
    await manager.drain()
    task = manager.tasks[ui.focus]
    old_request = task.session.request_id
    old_round = task.turns[0]
    if addressing == "select":
        ui.accept(f"/use {task.task_id}")
        await ui.accept_input("new request")
    else:
        ui.accept(f"/reply {task.task_id} new request")
    await manager.drain()
    for text in ["first detail", "second detail"]:
        await ui.accept_input(text)
        await manager.drain()
    assert len(manager.tasks) == 1 and task.state == "completed"
    assert not task.closed and len(task.turns) == 4
    assert len({r.turn_id for r in task.turns}) == 4
    assert task.session.request_id != old_request
    assert task.turns[0] is old_round and old_round.event.turn.response == "done"
    assert [m.content for m in task.session.messages if m.role == "user"] == [
        "completed", "new request", "first detail", "second detail"]
    assert any(m.role == "assistant" and m.content == "done" for m in task.session.messages)
    await manager.close()


async def test_new_topic_is_routed_to_isolated_task():
    manager = TaskManager(ConversationAgent())
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new completed")
    await manager.drain()
    first = ui.focus
    await ui.accept_input("independent request")
    await manager.drain()
    assert ui.focus != first and len(manager.tasks) == 2
    assert [m.content for m in manager.tasks[ui.focus].session.messages if m.role == "user"] == ["independent request"]
    await manager.close()


async def test_finished_task_rejects_followups_and_failed_round_can_continue():
    manager = TaskManager(ConversationAgent())
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new completed")
    await manager.drain()
    task = manager.tasks[ui.focus]
    task.state = "failed"
    ui.accept(f"/reply {task.task_id} new request")
    await manager.drain()
    assert len(manager.tasks) == 1 and task.state == "waiting"
    ui.accept(f"/finish {task.task_id}")
    assert task.closed and ui.focus is None
    assert ui.accept(f"/reply {task.task_id} another") == messages.TASK_NOT_WAITING
    assert ui.accept(f"/use {task.task_id}") == messages.TASK_NOT_WAITING
    await manager.close()


async def test_busy_selected_task_rejects_input_without_changing_owner():
    manager = TaskManager(Agent())
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new work")
    task_id = ui.focus
    assert ui.accept("premature answer") == messages.TASK_NOT_WAITING
    assert ui.accept(f"/finish {task_id}") == messages.TASK_NOT_WAITING
    assert ui.focus == task_id and len(manager.tasks) == 1
    await manager.close()


async def test_explicit_clarification_assignment_cannot_be_reset_by_model_routing():
    from datetime import UTC, datetime

    from karen import Karen
    from karen.intent import InputRouting, IntentRecognizer

    original_time = datetime(2026, 11, 3, tzinfo=UTC)
    original = IntentSession(
        messages=(Message(role="user", content="original task"),),
        clarification_items=(ClarificationItem(question_id="q1", text="Which?", asked_after=1),),
        reference_time_utc=original_time,
    )
    seen = []

    class Intent:
        prepare_session = staticmethod(IntentRecognizer.prepare_session)

        async def classify(self, session, text):
            return InputRouting(input_types=["question"], handling="assess",
                                task_relation="new", reason="Model misclassified a short reply")

        async def advance(self, session, text, *, routing, **kwargs):
            seen.append((session, routing))
            return session.model_copy(update={"reply": "done"})

    manager = TaskManager(Karen(intent=Intent(), engine=SimpleNamespace()))
    restored = manager.restore_waiting(original)
    ui = TaskInteraction(manager, IntentSession())
    try:
        ui.accept(f"/reply {restored.task_id} short answer")
        await manager.drain()
        assert restored.state == "completed" and len(manager.tasks) == 1
        session, routing = seen[0]
        assert routing.task_relation == "continue"
        assert session.request_id == original.request_id
        assert session.messages == original.messages and session.questions == original.questions
        assert session.reference_time_utc == original_time
    finally:
        await manager.close()


@pytest.mark.parametrize("command", [
    "/tasks", "/tasks 列表", "/tasks list", "  /tasks   列表  ", "/tasks\tlist",
])
def test_task_list_command_never_submits_or_calls_agent(command):
    class ReadOnlyManager:
        tasks = {
            "existing": SimpleNamespace(task_id="existing", state="waiting", title="Existing task", closed=False),
        }

        def submit(self, *args, **kwargs):
            pytest.fail("Listing tasks must never submit work")

    ui = TaskInteraction(ReadOnlyManager(), IntentSession())
    ui.focus = "existing"
    ui.pending = ("unassigned answer", None, None)
    pending = ui.pending
    result = ui.accept(command)
    assert "existing" in result and "Existing task" in result
    assert ui.focus == "existing" and ui.pending is pending
    assert ui.submitted_source is None


@pytest.mark.parametrize("command", ["/tasks", "/tasks 列表", "/tasks list"])
def test_task_list_reports_empty_without_submitting(command):
    ui = TaskInteraction(SimpleNamespace(tasks={}), IntentSession())
    assert ui.accept(command) == messages.TASK_LIST_EMPTY


@pytest.mark.parametrize("command", ["/tasks running", "/tasks 列表 extra"])
def test_invalid_task_list_arguments_show_usage_instead_of_submitting(command):
    ui = TaskInteraction(SimpleNamespace(tasks={}), IntentSession())
    assert ui.accept(command) == messages.TASK_LIST_USAGE


async def test_closed_pending_selection_keeps_focus_message_and_original_source():
    from datetime import UTC, datetime

    manager = TaskManager(ConversationAgent())
    ui = TaskInteraction(manager, IntentSession())
    failed = manager.submit("completed", IntentSession())
    waiting = manager.submit("new request", IntentSession())
    await manager.drain()
    failed.closed = True
    source, received_at = {"message_id": "original"}, datetime.now(UTC)
    try:
        ui.accept("first detail", source=source, received_at=received_at)
        assert ui.accept(f"/use {failed.task_id}") == messages.TASK_NOT_WAITING
        assert ui.focus is None and ui.pending == ("first detail", received_at, source)
        ui.accept(f"/use {waiting.task_id}", source={"message_id": "selection"})
        assert ui.submitted_source is source
        await manager.drain()
        assert manager.agent.inputs[-1][2]["received_at"] == received_at
        assert ui.focus == waiting.task_id and ui.pending is None
    finally:
        await manager.close()




async def test_automatic_followup_uses_previous_reply_without_memory_and_routes_once():
    from intent_helpers import ClarityAwareModel
    from test_input_routing import reply, routing

    from karen import IntentRecognizer, Karen

    model = ClarityAwareModel([
        routing("respond", types=["question"]), reply("方案甲成本 12 元，方案乙成本 19 元。"),
        routing("respond", types=["question"], relation="continue"), reply("方案乙贵 7 元。"),
        routing("respond", types=["question"]), reply("新的独立回答。"),
    ])
    manager = TaskManager(Karen(intent=IntentRecognizer(model), engine=SimpleNamespace()))
    ui = TaskInteraction(manager, IntentSession())
    await ui.accept_input("两个方案分别多少钱？")
    await manager.drain()
    first = manager.tasks[ui.focus]
    first_request = first.session.request_id
    await ui.accept_input("那乙比甲贵多少？")
    await manager.drain()
    assert ui.focus == first.task_id and first.turn.response == "方案乙贵 7 元。"
    assert first.session.request_id != first_request and len(first.turns) == 2
    classifiers = [r for r in model.requests if r.role == "intent_router"]
    assert len(classifiers) == 2
    assert any(m["content"] == "方案甲成本 12 元，方案乙成本 19 元。"
               for m in classifiers[-1].input_data["pending_task"]["messages"])
    await ui.accept_input("另一个问题")
    await manager.drain()
    assert len(manager.tasks) == 2 and ui.focus != first.task_id
    latest = manager.tasks[ui.focus]
    assert all("方案" not in m.content for m in latest.session.messages)
    await manager.close()


async def test_routing_failure_does_not_submit_or_reuse_previous_message_source():
    from dynamic_graph.models.client import ModelCallError

    class BrokenRouter:
        async def classify(self, session, text):
            raise ModelCallError("MODEL_TIMEOUT", "timeout")

    agent = ConversationAgent()
    agent.intent = BrokenRouter()
    manager = TaskManager(agent)
    ui = TaskInteraction(manager, IntentSession())
    ui.accept("/new completed", source={"id": 1})
    await manager.drain()
    task = manager.tasks[ui.focus]
    assert await ui.accept_input("继续", source={"id": 2}) == messages.TASK_ROUTING_FAILED
    assert ui.submitted_source is None and len(task.turns) == 1
    assert task.state == "completed"
    await manager.close()


async def test_cancelled_round_can_continue_with_fresh_token_and_saved_failure():
    agent = Agent()
    manager = TaskManager(agent)
    task = manager.submit("work", IntentSession())
    await asyncio.sleep(0)
    manager.cancel(task.task_id)
    await manager.drain()
    assert task.state == "cancelled" and len(task.turns) == 1
    agent.release.set()
    manager.submit("retry", IntentSession(), task_id=task.task_id)
    await manager.drain()
    assert task.state == "completed" and not task.token.cancelled
    assert len(task.turns) == 2 and task.turns[0].event.state == "cancelled"
    await manager.close()
