"""Bound recorded evidence without discarding large-artifact provenance."""

import hashlib

from dynamic_graph.graph.schemas import canonical as encode


def anchors(value):
    found = []

    def visit(item):
        if isinstance(item, str) and 4 <= len(item) <= 160 and item not in found:
            found.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                if len(found) >= 96:
                    break
                visit(child)
        elif isinstance(item, list):
            for child in item[:32]:
                if len(found) >= 96:
                    break
                visit(child)

    visit(value)
    return found[:96]


def project(value, terms, *, depth=0):
    if depth >= 10:
        return {"truncated": True, "reason": "depth_budget"}
    if isinstance(value, str) and len(value) > 8192:
        excerpts = []
        for term in terms:
            start = value.find(term)
            if start >= 0:
                excerpts.append(
                    {
                        "match": term,
                        "offset": start,
                        "text": value[max(0, start - 100) : start + len(term) + 160],
                    }
                )
                if len(excerpts) == 12:
                    break
        return {
            "truncated": True,
            "preview": value[:2048],
            "tail": value[-1024:],
            "bytes": len(value.encode()),
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
            "excerpts": excerpts,
        }
    if isinstance(value, dict):
        result = {
            key: project(child, terms, depth=depth + 1) for key, child in list(value.items())[:64]
        }
        if len(value) > 64:
            result["_truncated_fields"] = True
        return result
    if isinstance(value, list):
        return [project(child, terms, depth=depth + 1) for child in value[:32]] + (
            [{"truncated": True, "reason": "list_budget"}] if len(value) > 32 else []
        )
    return value


def project_artifacts(documents, outputs):
    terms = anchors([outputs, *documents])
    artifacts, used, omitted = [], 0, 0
    for document in documents:
        bounded = project(document, terms)
        size = len(encode(bounded))
        if size > 24 * 1024:
            # Preserve node/capability metadata, explicitly omit an oversized payload.
            bounded = {key: value for key, value in bounded.items() if key != "output"}
            bounded["output"] = {"truncated": True, "reason": "artifact_budget"}
            size = len(encode(bounded))
        if used + size > 96 * 1024:
            omitted += 1
            continue
        used += size
        artifacts.append(bounded)
    return artifacts, {"omitted": omitted, "bytes": used, "projected": True}
