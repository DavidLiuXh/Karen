"""Real model/module evaluation with per-case memory, execution and trace directories."""

import asyncio
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings, ModelRequest
from dynamic_graph.tools import file_read_text_tool, file_write_text_tool, tavily_search_tool

from karen.agent import Karen
from karen.context import ContextEvent, ContextMemory
from karen.intent import IntentRecognizer, IntentSession
from karen.models import deepseek_client, memory_embeddings
from karen.observability import ObservedModel, Observer
from karen.response import format_result

from .datasets import PROTOCOL_VERSION, parse_date
from .prompts import JUDGE_SCHEMA, JUDGE_SYSTEM


class MeteredModel:
    def __init__(self, client):
        self.client = client
        self.calls = []

    async def generate(self, request):
        started = time.monotonic()
        call = {"role": request.role}
        self.calls.append(call)
        try:
            response = await self.client.generate(request)
            call["usage"] = response.usage
            return response
        except Exception as error:
            call["error_code"] = getattr(error, "code", type(error).__name__)
            raise
        finally:
            call["seconds"] = round(time.monotonic() - started, 3)


def check_step(expected, actual):
    """Hard assertions cannot be overturned by an LLM grader."""
    failures = []
    for key in ("handling", "relation"):
        if key in expected and expected[key] != actual.get(key):
            failures.append(f"{key}: expected {expected[key]}, got {actual.get(key)}")
    outcome = actual["outcome"]
    if "outcome" in expected:
        valid = (
            outcome != "clarification"
            if expected["outcome"] == "complete"
            else (outcome == expected["outcome"])
        )
        if not valid:
            failures.append(f"outcome: expected {expected['outcome']}, got {outcome}")
    if actual.get("execution_status") and (
        actual["execution_status"] != "COMPLETED" or not actual["output_complete"]
    ):
        failures.append("execution did not complete")
    for text in expected.get("contains", []):
        if text.casefold() not in actual["answer"].casefold():
            failures.append(f"answer missing: {text}")
    for text in expected.get("not_contains", []):
        if text.casefold() in actual["answer"].casefold():
            failures.append(f"answer exposes/contradicts: {text}")
    goal = actual.get("goal") or {}
    # Dates/requirements must occur in operative goal fields, not just trusted context.
    operative_goal = json.dumps(
        {key: goal.get(key) for key in ("objective", "inputs", "success_criteria")},
        ensure_ascii=False,
    )
    for text in expected.get("goal_contains", []):
        if text not in operative_goal:
            failures.append(f"goal missing: {text}")
    return failures


async def judge(client, case, actual):
    gold = case["gold"]
    reference = gold.get("answer")
    if case["suite"] == "clamber" and actual["outcome"] == "clarification":
        reference = gold["clarifying_question"]
    if reference is None:
        return None
    response = await client.generate(
        ModelRequest(
            role="evaluation_judge",
            system_instruction=JUDGE_SYSTEM,
            task_instruction="Grade the candidate. The reference is visible only to this grader.",
            input_data={
                "question": case["steps"][-1]["text"],
                "category": case["category"],
                "reference": reference,
                "candidate": actual["answer"],
            },
            output_schema=JUDGE_SCHEMA,
            max_output_tokens=1024,
            timeout_seconds=60,
        )
    )
    payload = response.payload
    if (
        not isinstance(payload, dict)
        or type(payload.get("passed")) is not bool
        or not isinstance(payload.get("reason"), str)
    ):
        raise ValueError("Invalid judge response")
    return payload


def create_engine(model, directory, search_fixture):
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=directory / "execution"), models=ModelBindings(model, model)
    )
    artifacts = directory / "artifacts"
    artifacts.mkdir()
    engine.register_tool(file_read_text_tool(artifacts))
    engine.register_tool(file_write_text_tool(artifacts))
    if search_fixture:

        async def search(data, context):
            return {
                "query": data["query"],
                "results": [
                    {
                        "title": "离线天气测试数据",
                        "url": "https://weather.example.test/forecast",
                        "content": "2026-10-05 南京与大阪：多云，东北风 2–3 级，无大风预警。",
                        "score": 1.0,
                    }
                ],
            }

        engine.register_tool(replace(tavily_search_tool(api_key="offline-fixture"), handler=search))
    return engine


async def ingest(memory, case):
    receipts = []
    for event in case["history"]:
        receipts.append(
            memory.submit(
                ContextEvent(
                    conversation_id=f"history-{event['session_id']}",
                    request_id=event["session_id"],
                    event_id=hashlib.sha256(
                        f"{case['id']}:{event['session_id']}:{event['turn']}".encode()
                    ).hexdigest(),
                    occurred_at=parse_date(event["occurred_at"], case["timezone"]),
                    timezone=case["timezone"],
                    event_type=f"{event['role']}_message",
                    payload={"content": event["content"]},
                )
            )
        )
    if receipts:
        await memory.flush()
    return [(await memory.write_status(receipt)).model_dump(mode="json") for receipt in receipts]


async def evaluate_case(case, directory, *, client_factory=deepseek_client):
    directory.mkdir()
    observer = Observer(directory / "observability")
    await observer.start()
    meter = MeteredModel(ObservedModel(client_factory(), observer))
    memory = None
    started = time.monotonic()
    result = {
        "id": case["id"],
        "suite": case["suite"],
        "split": case["split"],
        "category": case["category"],
        "status": "error",
        "steps": [],
        "checks": [],
    }
    stage = "setup"
    try:
        intent = IntentRecognizer(meter, observer=observer)
        if case["suite"] != "clamber":
            memory = ContextMemory(
                root_dir=directory / "memory",
                model=meter,
                embeddings=memory_embeddings(),
                observer=observer,
            )
            await memory.start()
            stage = "ingestion"
            result["ingestion"] = await ingest(memory, case)
            # Prove persisted memory survives restart; no prior messages in the new session.
            await memory.close()
            memory = ContextMemory(
                root_dir=directory / "memory",
                model=meter,
                embeddings=memory_embeddings(),
                observer=observer,
            )
            await memory.start()
        engine = create_engine(
            meter, directory, case.get("search_fixture", case["suite"] == "karen-zh")
        )
        agent = Karen(intent=intent, engine=engine, memory=memory, observer=observer)
        session = None
        for step in case["steps"]:
            if session is None or session.completed:
                session = IntentSession(
                    conversation_id=f"eval-{case['id']}",
                    timezone=case["timezone"],
                    reference_time_utc=datetime.fromisoformat(case["reference_time"]),
                    user_context={"provided_context": case["context"]} if case["context"] else {},
                )
            stage = "interaction"
            if case["suite"] == "clamber":
                # Intent-only benchmark. A ready goal is NOT a completed task execution.
                session = await intent.advance(session, step["text"])
                actual = {
                    "outcome": "clarification"
                    if session.questions
                    else ("goal" if session.goal else "reply"),
                    "answer": session.reply or "\n".join(session.questions),
                    "goal": session.goal.model_dump(mode="json") if session.goal else None,
                }
            else:
                turn = await agent.advance(session, step["text"])
                session = turn.session
                actual = {
                    "outcome": "execution"
                    if turn.result
                    else ("reply" if turn.response else "clarification"),
                    "answer": format_result(turn.result)
                    if turn.result
                    else (turn.response or "\n".join(session.questions)),
                    "goal": session.goal.model_dump(mode="json") if session.goal else None,
                    "memory": turn.memory_result.model_dump(mode="json")
                    if turn.memory_result
                    else None,
                }
                if turn.result:
                    actual.update(
                        execution_status=turn.result.execution_status,
                        output_complete=turn.result.output_complete,
                        diagnostics=[d.model_dump(mode="json") for d in turn.result.diagnostics],
                    )
                if turn.response or turn.result:
                    agent.record_response(session, actual["answer"], trace_id=turn.trace_id)
            actual.update(handling=session.routing.handling, relation=session.routing.task_relation)
            result["steps"].append(actual)
            result["checks"].extend(check_step(step["expected"], actual))
        stage = "grading"
        # Do not spend judge calls on cases already failing a hard contract.
        result["judge"] = None if result["checks"] else await judge(meter, case, actual)
        if result["judge"] and not result["judge"]["passed"]:
            result["checks"].append(result["judge"]["reason"])
        result["status"] = "failed" if result["checks"] else "passed"
    except Exception as error:
        result.update(
            status="judge_error" if stage == "grading" else "error",
            stage=stage,
            error_code=getattr(error, "code", type(error).__name__),
        )
        if hasattr(error, "statuses"):
            result["ingestion_failures"] = [s.model_dump(mode="json") for s in error.statuses]
    finally:
        if memory is not None:
            await memory.close()
        await observer.close()
        result["seconds"] = round(time.monotonic() - started, 3)
        result["model_calls"] = meter.calls
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    return result


def summarize(results):
    groups = {}
    for row in results:
        key = f"{row['suite']}/{row['split']}"
        groups.setdefault(key, Counter())[row["status"]] += 1
    return {
        key: {
            **dict(counts),
            "total": sum(counts.values()),
            "pass_rate_all": counts["passed"] / sum(counts.values()),
        }
        for key, counts in groups.items()
    }


def revision(path):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


async def run(manifest_path, output, *, split="all", suites=(), case_ids=(), concurrency=3):
    manifest_path, output = Path(manifest_path), Path(output)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["protocol_version"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported evaluation protocol")
    cases = [
        case
        for case in manifest["cases"]
        if (split == "all" or case["split"] == split)
        and (not suites or case["suite"] in suites)
        and (not case_ids or case["id"] in case_ids)
    ]
    if not cases:
        raise ValueError("No selected cases")
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite a baseline.
    (output / "manifest.json").write_bytes(manifest_bytes)
    project = Path(__file__).resolve().parents[3]
    import dynamic_graph

    dag_project = Path(dynamic_graph.__file__).resolve().parents[2]
    (output / "run.json").write_text(
        json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "karen_revision": revision(project),
                "dag_revision": revision(dag_project),
                "karen_dirty": subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=project, text=True
                ),
                "dag_source_sha256": source_hash(dag_project / "src"),
                "karen_source_sha256": source_hash(project / "src"),
                "model": "deepseek-chat",
                "embedding": "bge-m3:latest",
                "judge": "deepseek-chat (custom grader, not official LongMemEval gpt-4o score)",
                "selected_ids": [c["id"] for c in cases],
                "concurrency": concurrency,
                "source_integrity": "running",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    semaphore = asyncio.Semaphore(concurrency)
    results = []

    async def evaluate(case):
        async with semaphore:
            print(f"START {case['id']}", flush=True)
            result = await evaluate_case(case, output / case["id"])
            results.append(result)
            with (output / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(f"{result['status'].upper()} {case['id']} {result['checks']}", flush=True)
            (output / "summary.json").write_text(json.dumps(summarize(results), indent=2) + "\n")

    await asyncio.gather(*(evaluate(case) for case in cases))
    metadata_path = output / "run.json"
    metadata = json.loads(metadata_path.read_text())
    changed = {}
    for name, root in (("karen", project), ("dag", dag_project)):
        final_hash = source_hash(root / "src")
        metadata[f"{name}_final_source_sha256"] = final_hash
        changed[name] = final_hash != metadata[f"{name}_source_sha256"]
    metadata["source_changed_during_run"] = changed
    metadata["source_integrity"] = "changed" if any(changed.values()) else "stable"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    if any(changed.values()):
        raise RuntimeError("SOURCE_CHANGED_DURING_EVALUATION; results retained, not comparable")
    return results


def source_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and path.suffix in {".py", ".txt", ".json"}:
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
