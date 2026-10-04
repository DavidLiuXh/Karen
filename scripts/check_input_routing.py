"""Opt-in semantic checks with synthetic inputs only, never a user's memory or trace files."""

import asyncio
import json
from pathlib import Path

from karen.intent import IntentRecognizer, IntentSession
from karen.intent.recognizer import Message
from karen.models import deepseek_client


async def main():
    cases = json.loads(
        (
            Path(__file__).resolve().parents[1] / "tests/fixtures/input_routing_cases.json"
        ).read_text()
    )
    intent = IntentRecognizer(deepseek_client())
    semaphore = asyncio.Semaphore(3)
    session = IntentSession(timezone="Asia/Shanghai")
    pending = session.model_copy(
        update={
            "messages": (
                Message(role="user", content="帮我写邮件"),
                Message(role="assistant", content="给谁写？"),
            ),
            "questions": ("给谁写？",),
        }
    )

    async def check(case):
        async with semaphore:
            try:
                route = await intent.classify(pending if case["pending"] else session, case["text"])
                passed = (
                    route.handling == case["handling"]
                    and route.task_relation == case["task_relation"]
                    and set(case["input_types"]).issubset(route.input_types)
                )
                detail = route.model_dump(mode="json")
            except Exception as error:
                passed, detail = (
                    False,
                    {"error_type": type(error).__name__, "code": getattr(error, "code", None)},
                )
        print(
            json.dumps(
                {
                    "text": case["text"],
                    "pending": case["pending"],
                    "passed": passed,
                    "actual": detail,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return passed

    results = await asyncio.gather(*(check(case) for case in cases))
    print(json.dumps({"passed": sum(results), "total": len(results)}))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
