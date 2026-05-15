from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..blocked import build_blocked
from ..evidence import attach_blocked_details, compact_evidence

_ACCEPTED_MODIFIERS = frozenset({"alt", "control", "ctrl", "shift", "win", "windows"})


async def handle_ui_drag(
    action: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    started = context.clock()
    source, blocked = _source_from_action(action)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    path, blocked = _path_from_action(action)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    drop, blocked = _drop_from_action(action)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    modifiers, blocked = _modifiers_from_action(action)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    blocked = _zero_distance_blocked(source=source, path=path, drop=drop)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    result = await context.call_adapter(
        "ui.drag",
        source=source,
        path=path,
        drop=drop,
        modifiers=modifiers,
        duration_ms=_optional_int(action.get("duration_ms")),
        step_count=_optional_int(action.get("step_count")),
        expect=dict(action.get("expect") or {}),
    )
    return _action_result(
        result,
        source=source,
        path=path,
        drop=drop,
        modifiers=modifiers,
        duration_ms=context.elapsed_ms(started),
    )


def _source_from_action(action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source = action.get("source")
    if not isinstance(source, Mapping):
        return {}, _blocked(
            reason="invalid drag source",
            requested={"source": source},
            accepted={
                "source": [
                    "selector",
                    "row_index",
                    "row_identity",
                    "cached_element",
                    "point",
                ]
            },
            next_step="Provide source as an object with one supported drag source form.",
        )
    return dict(source), None


def _path_from_action(action: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    path = action.get("path")
    if not isinstance(path, list) or not path:
        return [], _blocked(
            reason="invalid drag path",
            requested={"path": path},
            accepted={"path": "non-empty list of pointer waypoints"},
            next_step="Provide at least one pointer waypoint in path.",
        )
    normalized: list[dict[str, Any]] = []
    for index, waypoint in enumerate(path):
        if not isinstance(waypoint, Mapping):
            return [], _blocked(
                reason="invalid drag waypoint",
                requested={"path_index": index, "waypoint": waypoint},
                accepted={"waypoint": "object with relative_to, x, and y"},
                next_step="Provide each path item as a waypoint object.",
            )
        normalized.append(dict(waypoint))
    return normalized, None


def _drop_from_action(action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    drop = action.get("drop")
    if drop is None:
        return {}, None
    if not isinstance(drop, Mapping):
        return {}, _blocked(
            reason="invalid drag drop",
            requested={"drop": drop},
            accepted={"drop": "object pointer target"},
            next_step="Provide drop as a pointer target object.",
        )
    return dict(drop), None


def _modifiers_from_action(action: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    raw_modifiers = action.get("modifiers") or []
    if not isinstance(raw_modifiers, list):
        return [], _blocked(
            reason="invalid drag modifier",
            requested={"modifiers": raw_modifiers},
            accepted={"modifiers": sorted(_ACCEPTED_MODIFIERS)},
            next_step="Provide modifiers as a list of accepted key names.",
        )
    modifiers = [str(modifier).lower() for modifier in raw_modifiers]
    invalid = [modifier for modifier in modifiers if modifier not in _ACCEPTED_MODIFIERS]
    if invalid:
        return [], _blocked(
            reason="invalid drag modifier",
            requested={"modifiers": raw_modifiers, "invalid": invalid},
            accepted={"modifiers": sorted(_ACCEPTED_MODIFIERS)},
            next_step="Use ctrl, shift, alt, or win modifiers only.",
        )
    return modifiers, None


def _zero_distance_blocked(
    *,
    source: dict[str, Any],
    path: list[dict[str, Any]],
    drop: dict[str, Any],
) -> dict[str, Any] | None:
    start = _screen_point_from_source(source) or _screen_point(path[0])
    end = _screen_point(drop) or _screen_point(path[-1])
    if start is not None and end is not None and start == end:
        return _blocked(
            reason="zero-distance drag route",
            requested={"source": source, "path": path, "drop": drop},
            accepted={"route": "distinct start and drop coordinates"},
            next_step="Move the pointer beyond the platform drag threshold before dropping.",
        )
    return None


def _screen_point_from_source(source: dict[str, Any]) -> tuple[float, float] | None:
    point = source.get("point")
    return _screen_point(point) if isinstance(point, Mapping) else None


def _screen_point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Mapping):
        return None
    if str(value.get("relative_to") or "screen") != "screen":
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _action_result(
    result: dict[str, Any],
    *,
    source: dict[str, Any],
    path: list[dict[str, Any]],
    drop: dict[str, Any],
    modifiers: list[str],
    duration_ms: int,
) -> dict[str, Any]:
    status = str(result.get("status", "PASS"))
    output: dict[str, Any] = {
        "status": status,
        "route": "drag",
        "source": source,
        "path": path,
        "drop": drop,
        "modifiers": modifiers,
        "duration_ms": duration_ms,
        "backend": result.get("backend"),
        "route_evidence": compact_evidence(dict(result.get("route_evidence") or {})),
    }
    if not output["route_evidence"]:
        output["route_evidence"] = _default_route_evidence(
            path=path,
            drop=drop,
            modifiers=modifiers,
        )
    if status != "PASS":
        attach_blocked_details(output, result)
    evidence_ref = result.get("evidence_ref")
    if evidence_ref:
        output["evidence_ref"] = str(evidence_ref)
    return output


def _default_route_evidence(
    *,
    path: list[dict[str, Any]],
    drop: dict[str, Any],
    modifiers: list[str],
) -> dict[str, Any]:
    return {
            "move_points": path,
            "hold_points": [point for point in path if "hold_ms" in point],
            "final_pointer": drop or (path[-1] if path else None),
            "modifiers": modifiers,
    }


def _blocked(
    *,
    reason: str,
    requested: dict[str, Any],
    accepted: dict[str, Any],
    next_step: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        **build_blocked(
            reason=reason,
            requested=requested,
            accepted=accepted,
            next_step=next_step,
        ),
    }


def _with_duration(
    output: dict[str, Any],
    *,
    context: Any,
    started: float,
) -> dict[str, Any]:
    return {**output, "route": "drag", "duration_ms": context.elapsed_ms(started)}
