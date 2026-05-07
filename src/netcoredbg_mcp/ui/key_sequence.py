"""Scoped key sequence helpers for UI smoke evidence."""

from __future__ import annotations

from typing import Any

_SUPPORTED_KEYS = {
    "enter",
    "return",
    "tab",
    "escape",
    "esc",
    "backspace",
    "delete",
    "del",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pgup",
    "pgdn",
    "space",
    *(f"f{i}" for i in range(1, 13)),
}
_SUPPORTED_MODIFIERS = {"ctrl", "shift", "alt", "win"}


async def run_scoped_key_sequence(
    backend: Any,
    selector: dict[str, Any],
    *,
    modifiers: list[str],
    keys: list[str],
) -> dict[str, Any]:
    """Run a backend scoped key sequence and normalize terminal evidence."""
    normalized_modifiers = _normalize_modifiers(modifiers)
    if isinstance(normalized_modifiers, dict):
        return normalized_modifiers
    normalized_keys = _normalize_keys(keys)
    if isinstance(normalized_keys, dict):
        return normalized_keys

    result = await backend.scoped_key_sequence(
        dict(selector),
        normalized_modifiers,
        normalized_keys,
    )
    if result.get("unsupported") is True:
        return {
            **result,
            "reason": result.get("reason", "scoped key sequence unsupported"),
            "status": "BLOCKED",
        }

    final_held = list(result.get("final_held_modifiers") or [])
    if final_held:
        return {
            **result,
            "status": "FAIL",
            "reason": "modifier cleanup left held modifiers",
            "final_held_modifiers": final_held,
        }

    status = result.get("status") or "PASS"
    return {"status": status, **result, "final_held_modifiers": final_held}


def _normalize_modifiers(modifiers: list[str]) -> list[str] | dict[str, Any]:
    normalized = []
    for modifier in modifiers:
        value = modifier.strip().lower()
        if value not in _SUPPORTED_MODIFIERS:
            return {
                "status": "FAIL",
                "reason": "unknown modifier",
                "invalid_modifier": modifier,
                "sent_count": 0,
                "final_held_modifiers": [],
            }
        if value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_keys(keys: list[str]) -> list[str] | dict[str, Any]:
    normalized = []
    for key in keys:
        value = key.strip()
        lookup = value[1:-1] if value.startswith("{") and value.endswith("}") else value
        if len(lookup) == 1:
            normalized.append(value)
            continue
        if lookup.lower() not in _SUPPORTED_KEYS:
            return {
                "status": "FAIL",
                "reason": "unknown key",
                "invalid_key": key,
                "sent_count": 0,
                "final_held_modifiers": [],
            }
        normalized.append(lookup.upper())
    return normalized
