"""Public dataset adapters. Gold annotations never enter runtime inputs."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROTOCOL_VERSION = "1"
REFERENCE_TIME = "2026-10-04T08:00:00+00:00"


def read_rows(path):
    path = Path(path)
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        # The official CLAMBER file encodes each record as a JSON string.
        return [json.loads(row) if isinstance(row, str) else row for row in rows]
    return json.loads(path.read_text())


def parse_date(value, timezone):
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        date = datetime.strptime(value, "%Y/%m/%d (%a) %H:%M")
    return date.replace(tzinfo=ZoneInfo(timezone)) if date.tzinfo is None else date


def clamber(row, index):
    label = row["require_clarification"]
    if label not in (0, 1):
        raise ValueError("Invalid CLAMBER label")
    return {
        "id": f"clamber-{index:04d}",
        "suite": "clamber",
        "category": f"{row['category']}/{label}",
        "timezone": "UTC",
        "reference_time": REFERENCE_TIME,
        "context": row["context"],
        "history": [],
        "steps": [
            {
                "text": row["question"],
                "expected": {
                    "outcome": "clarification" if label else "complete",
                },
            }
        ],
        "gold": {"clarifying_question": row["clarifying_question"]},
    }


def longmemeval(row, profile, timezone="UTC"):
    dates, ids, sessions = (
        row[k] for k in ("haystack_dates", "haystack_session_ids", "haystack_sessions")
    )
    if not (len(dates) == len(ids) == len(sessions)):
        raise ValueError("Misaligned LongMemEval sessions")
    question_date = parse_date(row["question_date"], timezone)
    history = []
    for date, session_id, turns in sorted(
        zip(dates, ids, sessions), key=lambda item: parse_date(item[0], timezone)
    ):
        timestamp = parse_date(date, timezone)
        if timestamp > question_date:
            raise ValueError("History contains a future session")
        for index, turn in enumerate(turns):
            if turn["role"] not in {"user", "assistant"}:
                raise ValueError("Unknown source role")
            history.append(
                {
                    "session_id": session_id,
                    "turn": index,
                    "occurred_at": timestamp.isoformat(),
                    "role": turn["role"],
                    "content": turn["content"],
                }
            )
    return {
        "id": row["question_id"],
        "suite": f"longmemeval-{profile}",
        "category": "abstention" if row["question_id"].endswith("_abs") else row["question_type"],
        "timezone": timezone,
        "reference_time": question_date.isoformat(),
        "context": "",
        "history": history,
        "steps": [{"text": row["question"], "expected": {"outcome": "complete"}}],
        "gold": {"answer": row["answer"], "answer_session_ids": row["answer_session_ids"]},
    }


def prepare(data_dir, chinese_path, destination):
    """Freeze two examples per category, without observing model outputs."""
    data_dir, destination = Path(data_dir), Path(destination)
    cases, sources, excluded = [], {}, []
    for name, adapter in [
        ("clamber.jsonl", clamber),
        ("longmemeval_oracle.json", lambda row, _: longmemeval(row, "oracle")),
    ]:
        path = data_dir / name
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        groups = defaultdict(list)
        for index, row in enumerate(read_rows(path)):
            try:
                case = adapter(row, index)
            except ValueError as error:
                excluded.append(
                    {"source": name, "id": row.get("question_id", index), "reason": str(error)}
                )
                continue
            groups[case["category"]].append(case)
        for category in sorted(groups):
            # Oracle initial diagnostics intentionally prefer short complete histories.
            # Every original turn is preserved; this is NOT a representative full-S score.
            ordered = sorted(groups[category], key=lambda c: (len(c["history"]), c["id"]))
            for split, case in zip(("development", "heldout"), ordered[:2]):
                cases.append({**case, "split": split})
    chinese = Path(chinese_path)
    sources[chinese.name] = hashlib.sha256(chinese.read_bytes()).hexdigest()
    cases.extend(read_rows(chinese))
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evaluation IDs")
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "sources_sha256": sources,
        "excluded_invalid_records": excluded,
        "cases": cases,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def prepare_next(data_dir, previous_manifests, destination):
    """Freeze a disjoint expansion batch using IDs, never answers or outcomes.

    Four CLAMBER and two oracle records per category broaden the first diagnostic
    sample. Hash ordering is reproducible and does not favor short histories.
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("A frozen manifest must not be overwritten")
    used, memory_families, previous = set(), set(), {}
    for filename in previous_manifests:
        path = Path(filename)
        contents = path.read_bytes()
        manifest = json.loads(contents)
        if manifest["protocol_version"] != PROTOCOL_VERSION:
            raise ValueError("Unsupported previous evaluation protocol")
        used.update(case["id"] for case in manifest["cases"])
        memory_families.update(
            case["id"].removesuffix("_abs")
            for case in manifest["cases"]
            if case.get("suite", "").startswith("longmemeval-")
        )
        for name, digest in manifest.get("sources_sha256", {}).items():
            if name in previous and previous[name] != digest:
                raise ValueError(f"Previous manifests disagree on source: {name}")
            previous[name] = digest
    if not used:
        raise ValueError("Previous manifests must contain cases")
    cases, sources, excluded, counts = [], {}, [], {}
    for name, adapter, count in [
        ("clamber.jsonl", clamber, 4),
        ("longmemeval_oracle.json", lambda row, _: longmemeval(row, "oracle"), 2),
    ]:
        path = Path(data_dir) / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if previous.get(name) != digest:
            raise ValueError(f"Source changed since previous selection: {name}")
        sources[name] = digest
        groups = defaultdict(list)
        for index, row in enumerate(read_rows(path)):
            try:
                case = adapter(row, index)
            except ValueError as error:
                excluded.append(
                    {"source": name, "id": row.get("question_id", index), "reason": str(error)}
                )
                continue
            if case["id"] not in used and not (
                case["suite"].startswith("longmemeval-")
                and case["id"].removesuffix("_abs") in memory_families
            ):
                groups[case["category"]].append(case)
        for category, candidates in sorted(groups.items()):
            counts[f"{name}/{category}"] = len(candidates)
            ordered = sorted(
                candidates,
                key=lambda case: hashlib.sha256(
                    f"karen-expansion-v1:{case['id']}".encode()
                ).hexdigest(),
            )
            for index, case in enumerate(ordered[:count]):
                cases.append({**case, "split": "development" if index % 2 == 0 else "heldout"})
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "selection_rule": "karen-expansion-v1: sha256(ID), 4 CLAMBER / 2 oracle per category",
        "previous_manifest_sha256": [
            hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in previous_manifests
        ],
        "sources_sha256": sources,
        "eligible_remaining_by_category": counts,
        "excluded_invalid_records": excluded,
        "cases": cases,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
