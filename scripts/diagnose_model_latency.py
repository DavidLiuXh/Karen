"""Replay one fully recorded DeepSeek JSON-mode request without running task tools.

Print timing/count metadata only; never print prompt text, reasoning or generated content.
This script makes a billable model call. It does not write memories or execute the result.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from httpx import URL

from karen.model.models import deepseek_client


def truncated(value):
    if isinstance(value, dict):
        return value.get("truncated") is True or any(truncated(v) for v in value.values())
    return isinstance(value, list) and any(truncated(v) for v in value)


async def replay(event, *, thinking, timeout):
    data = event["data"]
    if data.get("model", {}).get("mode") != "json_mode" or truncated(data):
        raise ValueError("Replay requires a complete JSON-mode model.request event")
    model = data["model"]
    client = deepseek_client(
        model=model["model"],
        thinking=thinking,
        reasoning_effort=model.get("reasoning_effort") or "low",
    )
    api = client._chat.root_async_client
    messages = [
        {"role": "system", "content": data["system_instruction"]},
        {
            "role": "user",
            "content": json.dumps(
                {"task_instruction": data["task_instruction"], "input_data": data["input"]},
                ensure_ascii=False,
                allow_nan=False,
            ),
        },
        {
            "role": "user",
            "content": "Return JSON matching this schema: "
            + json.dumps(
                {
                    **data["output_schema"],
                    "title": "StructuredResponse",
                    "description": "The required structured response.",
                }
            ),
        },
    ]
    started = time.monotonic()
    metrics = {
        "thinking": thinking,
        "max_output_tokens": data["max_output_tokens"],
        "first_event_seconds": None,
        "first_reasoning_seconds": None,
        "first_content_seconds": None,
        "reasoning_chars": 0,
        "content_chars": 0,
        "finish_reason": None,
        "usage": None,
        "json_valid": False,
    }
    pool = api._client._transport_for_url(URL(str(api.base_url)))._pool
    metrics["transport_pool"] = type(pool).__name__
    output = []
    try:
        async with asyncio.timeout(timeout):
            stream = await api.chat.completions.create(
                model=model["model"],
                messages=messages,
                reasoning_effort=model.get("reasoning_effort") or "low",
                extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
                response_format={"type": "json_object"},
                max_tokens=data["max_output_tokens"],
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                elapsed = round(time.monotonic() - started, 3)
                if metrics["first_event_seconds"] is None:
                    metrics["first_event_seconds"] = elapsed
                if chunk.usage is not None:
                    metrics["usage"] = chunk.usage.model_dump()
                for choice in chunk.choices:
                    if choice.finish_reason is not None:
                        metrics["finish_reason"] = choice.finish_reason
                    reasoning = (choice.delta.model_extra or {}).get("reasoning_content") or ""
                    if reasoning:
                        if metrics["first_reasoning_seconds"] is None:
                            metrics["first_reasoning_seconds"] = elapsed
                        metrics["reasoning_chars"] += len(reasoning)
                    if choice.delta.content:
                        if metrics["first_content_seconds"] is None:
                            metrics["first_content_seconds"] = elapsed
                        metrics["content_chars"] += len(choice.delta.content)
                        output.append(choice.delta.content)
        try:
            json.loads("".join(output))
            metrics["json_valid"] = True
        except ValueError:
            metrics["error"] = "INVALID_JSON"
    except Exception as exc:
        metrics["error"] = type(exc).__name__
    finally:
        metrics["total_seconds"] = round(time.monotonic() - started, 3)
        await api.close()
    return metrics


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    event = next(
        e
        for e in map(json.loads, args.trace.read_text().splitlines())
        if e.get("event_id") == args.event_id and e.get("event_type") == "model.request"
    )
    print(json.dumps(await replay(event, thinking=not args.no_thinking, timeout=args.timeout)))


if __name__ == "__main__":
    asyncio.run(main())
