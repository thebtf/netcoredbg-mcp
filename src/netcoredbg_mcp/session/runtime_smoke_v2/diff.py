from __future__ import annotations

from typing import Any


def compute_diff(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        key: {"from": before.get(key), "to": value}
        for key, value in after.items()
        if before.get(key) != value
    }
