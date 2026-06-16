from __future__ import annotations

import re
from typing import Any

SAFE_TRACEPOINT_EXPRESSION_GUIDANCE = (
    "read-only identifiers, property paths, indexers, literals, and simple arithmetic"
)

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_INDEXER = r"(?:\[\s*(?:\d+|\"[^\"]*\"|'[^']*')\s*\])"
_PATH_SEGMENT = rf"{_IDENTIFIER}(?:{_INDEXER})*"
_PROPERTY_PATH = rf"{_PATH_SEGMENT}(?:\.{_PATH_SEGMENT})*"
_LITERAL = r"(?:\d+(?:\.\d+)?|true|false|null|None|\"[^\"]*\"|'[^']*')"
_ATOM = rf"(?:{_PROPERTY_PATH}|{_LITERAL})"
_SAFE_EXPRESSION_RE = re.compile(rf"^\s*[+-]?\s*{_ATOM}(?:\s*[+\-*/]\s*{_ATOM})*\s*$")


def tracepoint_expression_policy_error(expression: Any) -> str | None:
    """Return a policy error for unsafe tracepoint expressions."""
    text = str(expression or "").strip()
    if not text:
        return "unsafe tracepoint expression: expression is required"
    if not _SAFE_EXPRESSION_RE.fullmatch(text):
        return "unsafe tracepoint expression"
    return None


def classify_tracepoint_logs(logs: list[Any]) -> tuple[str | None, str | None]:
    values = [str(log.get("value", "")) for log in logs if isinstance(log, dict)]
    if any(value.startswith("<error:") or value == "<timeout>" for value in values):
        return "TRACEPOINT_EXPRESSION_ERROR", "tracepoint expression evaluation failed"
    if any(value == "<rate limited>" for value in values):
        return (
            "TRACEPOINT_RATE_LIMITED",
            "tracepoint rate limit prevented reliable route evidence",
        )
    return None, None
