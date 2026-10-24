"""Versioned grading calibration and regrading, without invoking the agent."""

import asyncio
import hashlib
import json
from pathlib import Path

from ..intent.policy import CLARIFICATION_POLICY_VERSION, TASK_GAP_INSTRUCTION
from ..model.models import deepseek_client
from .clarification import (
    JUDGE_INSTRUCTION,
    QUALITY_SCHEMA,
    assess_steps,
    grade_quality,
    summarize_dimensions,
    trace_origins,
)
from .runner import MeteredModel, check_step, source_hash, summarize


def freeze_v2(source, destination):
    source, destination = Path(source), Path(destination)
    raw = source.read_bytes()
    manifest = json.loads(raw)
    if manifest["protocol_version"] != "1":
        raise ValueError("Expected an original protocol 1 manifest")
    manifest.update(protocol_version="2", source_protocol="1",
                    source_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                    clarification_policy_version=CLARIFICATION_POLICY_VERSION)
    # Exclusive creation protects both the source and previous frozen manifests.
    with destination.open("x") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


async def calibrate(cases_path, output, *, client_factory=None, concurrency=2):
    """Evaluate curated reference verdicts; this is judge agreement, not agent accuracy."""
    cases_path, output = Path(cases_path), Path(output)
    fixture_bytes = cases_path.read_bytes()
    fixture = json.loads(fixture_bytes)
    if fixture["protocol_version"] != "2":
        raise ValueError("Calibration requires protocol 2")
    rows = fixture["cases"]
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Calibration cases must have unique IDs")
    output.mkdir(parents=True, exist_ok=False)
    (output / "references.json").write_bytes(fixture_bytes)
    (output / "grader.json").write_text(json.dumps({
        "instruction": JUDGE_INSTRUCTION, "schema": QUALITY_SCHEMA,
        "policy_version": CLARIFICATION_POLICY_VERSION, "product_policy": TASK_GAP_INSTRUCTION,
    }, ensure_ascii=False, indent=2) + "\n")
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(row):
        async with semaphore:
            client = MeteredModel((client_factory or (lambda: deepseek_client(thinking=False)))())
            try:
                grade = await grade_quality(client, row["case"], row["actual_steps"], policy=True)
                matched = bool(grade and grade["verdict"] == row["expected_verdict"]
                               and grade["repeats_answered_information"] == row["expected_repetition"])
                result = {"id": row["id"], "matched": matched, "grade": grade}
            except Exception as error:
                result = {"id": row["id"], "matched": False,
                          "error_code": getattr(error, "code", type(error).__name__)}
            result["model_calls"] = client.calls
            with (output / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(f"CALIBRATION {row['id']} {'MATCH' if result['matched'] else 'MISMATCH'}", flush=True)
            return result

    results = await asyncio.gather(*(evaluate(row) for row in rows))
    matched = sum(row["matched"] for row in results)
    report = {"protocol_version": "2", "mode": "judge_calibration",
              "reference_provenance": fixture["provenance"],
              "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
              "grader_source_sha256": source_hash(Path(__file__).parent),
              "matched": matched, "total": len(results), "agreement": matched / len(results),
              "calibrated_on_curated_set": matched == len(results),
              "independent_human_review": False,
              "training_reward_approved": False}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


async def regrade(source, output, *, client_factory=None, suites=(), concurrency=2):
    """New grades of saved responses, explicitly not a rerun of current product code."""
    source, output = Path(source), Path(output)
    manifest_bytes = (source / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    cases = {case["id"]: case for case in manifest["cases"] if not suites or case["suite"] in suites}
    originals = [json.loads(line) for line in (source / "results.jsonl").read_text().splitlines() if line.strip()]
    originals = [row for row in originals if row["id"] in cases]
    if not originals or len({r["id"] for r in originals}) != len(originals):
        raise ValueError("Expected unique saved evaluation results")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source-manifest.json").write_bytes(manifest_bytes)
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(original):
        async with semaphore:
            case = cases[original["id"]]
            client = MeteredModel((client_factory or (lambda: deepseek_client(thinking=False)))())
            row = {"id": original["id"], "suite": original["suite"], "split": original["split"],
                   "source_status": original["status"], "steps": original["steps"],
                   "expected_steps": [{k: step[k] for k in ("expected", "policy_expectation") if k in step}
                                      for step in case["steps"]],
                   "clarification_origins": trace_origins(source / original["id"])}
            row["dimensions"] = await assess_steps(client, case, row["steps"])
            row["checks"] = [failure for step, actual in zip(case["steps"], row["steps"])
                             for failure in check_step(step["expected"], actual)]
            grades = [d["public_quality"] for d in row["dimensions"] if d["public_quality"]]
            row["checks"].extend(g["reason"] for g in grades if g["verdict"] == "fail")
            row["status"] = ("error" if len(row["steps"]) != len(case["steps"]) else
                             "failed" if row["checks"] else
                             "judge_error" if any(g["verdict"] == "error" for g in grades) else
                             "disputed" if any(g["verdict"] == "disputed" for g in grades) else "passed")
            row["model_calls"] = client.calls
            with (output / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"REGRADE {row['id']} {row['status']}", flush=True)
            return row

    results = await asyncio.gather(*(evaluate(row) for row in originals))
    report = {"protocol_version": "2", "mode": "regrade_saved_responses",
              "source_run": str(source.resolve()),
              "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "source_results_sha256": hashlib.sha256((source / "results.jsonl").read_bytes()).hexdigest(),
              "grader_source_sha256": source_hash(Path(__file__).parent),
              "groups": summarize(results), "dimensions": summarize_dimensions(results)}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
