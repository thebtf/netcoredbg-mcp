from __future__ import annotations

from collections.abc import Callable
from typing import Any

CompactBuilder = Callable[[dict[str, Any]], dict[str, Any]]


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
