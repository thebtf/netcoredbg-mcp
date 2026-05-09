from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

CompactBuilder = Callable[[dict[str, Any]], dict[str, Any]]
MAX_COMPACT_TEXT_LENGTH = 240
MAX_COMPACT_LIST_ITEMS = 8


def finalize_result(
    *,
    status: str,
    reason: str,
    elapsed_ms: int,
    action_count: int,
    completed_steps: list[dict[str, Any]],
    failed_assertions: list[dict[str, Any]],
    cleanup: dict[str, Any],
    evidence_refs: list[Any],
    compact_builder: CompactBuilder,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    terminal_status = status
    terminal_reason = reason
    if cleanup["status"] == "FAIL" and terminal_status not in {"FAIL", "IMPASSE"}:
        terminal_status = "FAIL"
        terminal_reason = "teardown failed"

    result = {
        "status": terminal_status,
        "reason": terminal_reason,
        "elapsed_ms": elapsed_ms,
        "action_count": action_count,
        "completed_steps": completed_steps,
        "failed_assertions": failed_assertions,
        "cleanup": cleanup,
        "evidence_refs": evidence_refs,
    }
    if extra:
        result.update(extra)
    result["compact"] = compact_builder(result)
    return result


def compact_runtime_smoke_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a pasteable bounded runtime smoke result."""
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "failed_assertions": compact_value(result.get("failed_assertions", [])),
        "cleanup": compact_value(result.get("cleanup", {})),
        "evidence_refs": compact_value(result.get("evidence_refs", [])),
        "completed_steps": compact_value(result.get("completed_steps", [])),
    }


def compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        omitted: list[str] = []
        for key, item in value.items():
            if isinstance(item, str) and len(item) > MAX_COMPACT_TEXT_LENGTH:
                omitted.append(key)
                compact[f"{key}_length"] = len(item)
                continue
            compact[key] = compact_value(item)
        if omitted:
            compact["omitted_fields"] = omitted
        return compact
    if isinstance(value, list):
        compact_items = [compact_value(item) for item in value[:MAX_COMPACT_LIST_ITEMS]]
        if len(value) > MAX_COMPACT_LIST_ITEMS:
            compact_items.append({
                "omitted_count": len(value) - MAX_COMPACT_LIST_ITEMS,
            })
        return compact_items
    if isinstance(value, str) and len(value) > MAX_COMPACT_TEXT_LENGTH:
        return {
            "text_length": len(value),
        }
    return value


def compact_json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
