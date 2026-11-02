"""Read new CLI input while completed task events are delivered independently."""

import asyncio
import json

from ..message import messages
from ..model.response import format_task_event
from .interaction import TaskInteraction


async def run_console(manager, session, *, read_input, display, json_output=False):
    interaction = TaskInteraction(manager, session)
    exit_code = 0

    async def deliver():
        nonlocal exit_code
        while True:
            event = await manager.events.get()
            try:
                text = format_task_event(event, json_output=json_output)
                if json_output:
                    payload = {
                        "type": "task",
                        "task_id": event.task_id,
                        "state": event.state,
                        "answer": text,
                    }
                    if event.turn is not None and event.turn.result is not None:
                        payload["result"] = event.turn.result.model_dump(mode="json")
                    display(json.dumps(payload, ensure_ascii=False), json_output=True)
                else:
                    display(
                        messages.TASK_EVENT.format(
                            task_id=event.task_id, state=messages.TASK_STATE_LABELS[event.state], text=text
                        )
                    )
                turn = event.turn
                if turn is not None and (turn.response is not None or turn.result is not None):
                    warnings = (
                        *turn.memory_warnings,
                        *manager.agent.record_response(turn.session, text, trace_id=turn.trace_id),
                    )
                    for warning in dict.fromkeys(warnings):
                        notice(messages.MEMORY_WRITE_FAILED.format(warning=warning))
                if event.state == "failed":
                    exit_code = 1
            finally:
                manager.events.task_done()

    async def flush_events():
        joined = asyncio.create_task(manager.events.join())
        try:
            done, _ = await asyncio.wait({joined, consumer}, return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                await consumer
            await joined
        finally:
            joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)

    consumer = asyncio.create_task(deliver())
    try:

        def notice(text):
            if json_output:
                display(
                    json.dumps({"type": "notice", "message": text}, ensure_ascii=False),
                    json_output=True,
                )
            else:
                display(text)

        notice(messages.TASK_HELP)
        while True:
            try:
                text = await read_input()
            except EOFError:
                await manager.drain()
                await flush_events()
                return exit_code
            if text.strip() == "/exit":
                await manager.close()
                await flush_events()
                return exit_code
            notice(interaction.accept(text))
            # Fairness also matters for redirected input that returns without yielding.
            await asyncio.sleep(0)
    finally:
        await manager.close()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
