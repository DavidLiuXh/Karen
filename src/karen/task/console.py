"""Read new CLI input while completed task events are delivered independently."""

import asyncio
import json
import sqlite3

from ..message import messages
from ..model.response import format_task_event
from .interaction import TaskInteraction


async def run_console(manager, session, *, read_input, display, json_output=False, schedules=None):
    interaction = TaskInteraction(manager, session)
    exit_code = 0

    async def deliver():
        nonlocal exit_code
        while True:
            event = await manager.events.get()
            try:
                if schedules is not None:
                    schedules.record_event(event)
                text = format_task_event(event, json_output=json_output)
                schedule = schedules.for_task(event.task_id) if schedules is not None else None
                if json_output:
                    payload = {
                        "type": "task",
                        "task_id": event.task_id,
                        "turn_id": event.turn_id,
                        "state": event.state,
                        "answer": text,
                    }
                    if event.turn is not None and event.turn.result is not None:
                        payload["result"] = event.turn.result.model_dump(mode="json")
                    if schedule is not None:
                        payload["schedule_id"] = schedule.schedule_id
                    display(json.dumps(payload, ensure_ascii=False), json_output=True)
                else:
                    output = messages.TASK_EVENT.format(
                        task_id=event.task_id, state=messages.TASK_STATE_LABELS[event.state], text=text
                    )
                    if schedule is not None:
                        output = messages.SCHEDULE_EVENT.format(
                            schedule_id=schedule.schedule_id, name=schedule.name, text=output,
                        )
                    display(output)
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

        if schedules is not None:
            try:
                await schedules.start()
            except (OSError, ValueError, sqlite3.Error):
                notice(messages.SCHEDULE_START_FAILED)
                return 1
        notice(messages.TASK_HELP)
        while True:
            reader = asyncio.create_task(read_input())
            try:
                watched = {reader, consumer}
                if schedules is not None:
                    watched.add(schedules.runner)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                for task in done - {reader}:
                    await task
                text = await reader
            except EOFError:
                if schedules is not None:
                    await schedules.stop()
                await manager.drain()
                await flush_events()
                return exit_code
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if text.strip() == "/exit":
                if schedules is not None:
                    await schedules.stop()
                await manager.close()
                await flush_events()
                return exit_code
            response = await schedules.handle(text) if schedules is not None else None
            notice(response if response is not None else await interaction.accept_input(text))
            # Fairness also matters for redirected input that returns without yielding.
            await asyncio.sleep(0)
    finally:
        if schedules is not None:
            await schedules.stop()
        await manager.close()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
