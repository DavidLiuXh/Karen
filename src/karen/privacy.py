"""Credential redaction shared by memory capture and runtime observation."""

import re

from dynamic_graph.execution.privacy import redact_sensitive


def redact(value, *, sensitive_values=(), paths=None, prefix=""):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            path = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if re.search(
                r"(?i)(?:^|[_-])(api[_-]?key|authorization|password|access[_-]?token|secret|private[_-]?key)$",
                key,
            ):
                result[key] = "[REDACTED]"
                if paths is not None:
                    paths.append(path)
            else:
                result[key] = redact(
                    item, sensitive_values=sensitive_values, paths=paths, prefix=path
                )
        return result
    if isinstance(value, list):
        return [
            redact(item, sensitive_values=sensitive_values, paths=paths, prefix=f"{prefix}/{index}")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        redacted = redact_sensitive(value, sensitive_values)
        redacted = re.sub(
            r"(?i)(\b(?:[a-z0-9]+_)?(?:api[_-]?key|password|access[_-]?token|authorization)\s*[:=]\s*)(?:Bearer\s+)?\S+",
            r"\1[REDACTED]",
            redacted,
        )
        if redacted != value and paths is not None:
            paths.append(prefix)
        return redacted
    return value
