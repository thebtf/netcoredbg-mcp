from __future__ import annotations

from collections.abc import Callable
from typing import Any

SELECTOR_KEYS = ("automation_id", "name", "control_type", "root_id", "xpath")


def build_blocked(
    *,
    reason: str,
    requested: dict[str, Any],
    accepted: dict[str, Any],
    next_step: str | Callable[[], str],
) -> dict[str, Any]:
    if not reason:
        raise ValueError("blocked reason is required")
    resolved_next_step = next_step() if callable(next_step) else next_step
    if not resolved_next_step:
        raise ValueError("blocked next_step is required")
    if not accepted:
        raise ValueError("blocked accepted guidance is required")
    return {
        "reason": reason,
        "requested": requested,
        "accepted": accepted,
        "next_step": resolved_next_step,
    }


def selector_guidance() -> dict[str, Any]:
    return {"selector_keys": list(SELECTOR_KEYS)}
