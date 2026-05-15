from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..blocked import build_blocked
from ..evidence import attach_blocked_details, compact_evidence

_ACCEPTED_MODIFIERS = frozenset({"alt", "control", "ctrl", "shift", "win", "windows"})
_SOURCE_KINDS = ("row_index", "row_identity", "cached_element", "point")


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

    cancel, blocked = _cancel_from_action(action)
    if blocked is not None:
        return _with_duration(blocked, context=context, started=started)

    identity, blocked = _identity_from_action(action)
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
        cancel=cancel,
        identity=identity,
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
        cancel=cancel,
        identity=identity,
        expect=dict(action.get("expect") or {}),
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
    return _normalize_source(dict(source))


def _normalize_source(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    selector = source.get("selector")
    if selector is not None and not isinstance(selector, Mapping):
        return {}, _blocked(
            reason="invalid drag source selector",
            requested={"selector": selector},
            accepted={"selector": "object selector for the source element or grid"},
            next_step="Provide source.selector as an object.",
        )
    selector_dict = dict(selector) if isinstance(selector, Mapping) else None

    present_kinds = [kind for kind in _SOURCE_KINDS if source.get(kind) is not None]
    if not present_kinds and selector_dict is not None:
        return {"kind": "selector", "selector": selector_dict}, None
    if len(present_kinds) != 1:
        return {}, _blocked(
            reason="ambiguous drag source",
            requested={"source": source},
            accepted={
                "source": ["row_index", "row_identity", "cached_element", "point", "selector"]
            },
            next_step="Provide exactly one drag source form.",
        )

    kind = present_kinds[0]
    if kind in {"row_index", "row_identity"} and selector_dict is None:
        return {}, _blocked(
            reason="invalid drag source",
            requested={"source": source},
            accepted={kind: "requires source.selector for grid disambiguation"},
            next_step=f"Provide source.selector with source.{kind}.",
        )
    if kind == "row_index":
        row_index, blocked = _row_index_from_source(source)
        if blocked is not None:
            return {}, blocked
        return {
            "kind": kind,
            "selector": selector_dict,
            "row_index": row_index,
        }, None
    if kind == "row_identity":
        row_identity = str(source.get("row_identity") or "")
        if not row_identity:
            return {}, _blocked(
                reason="invalid drag source",
                requested={"row_identity": source.get("row_identity")},
                accepted={"row_identity": "non-empty visible row identity"},
                next_step="Provide source.row_identity as a non-empty string.",
            )
        return {
            "kind": kind,
            "selector": selector_dict,
            "row_identity": row_identity,
        }, None
    if kind == "point":
        point = source.get("point")
        if not isinstance(point, Mapping):
            return {}, _blocked(
                reason="invalid drag source",
                requested={"point": point},
                accepted={"point": "object with relative_to, x, and y"},
                next_step="Provide source.point as a pointer coordinate object.",
            )
        return {"kind": kind, "point": dict(point)}, None

    cached_element = source.get("cached_element")
    if not cached_element:
        return {}, _blocked(
            reason="invalid drag source",
            requested={"cached_element": cached_element},
            accepted={"cached_element": "non-empty backend element reference"},
            next_step="Provide source.cached_element from earlier UI evidence.",
        )
    return {"kind": kind, "cached_element": cached_element}, None


def _identity_from_action(action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_identity = action.get("identity")
    if raw_identity is None:
        return {}, None
    if not isinstance(raw_identity, Mapping):
        return {}, _blocked(
            reason="invalid drag identity",
            requested={"identity": raw_identity},
            accepted={"identity": "object with column or columns fields"},
            next_step="Provide identity as an object when row identity evidence is required.",
        )
    return dict(raw_identity), None


def _row_index_from_source(source: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    raw_row_index = source.get("row_index")
    if raw_row_index is None:
        return 0, _blocked(
            reason="invalid drag source",
            requested={"row_index": raw_row_index},
            accepted={"row_index": "non-negative integer"},
            next_step="Provide source.row_index as a non-negative integer.",
        )
    if isinstance(raw_row_index, bool):
        return 0, _blocked(
            reason="invalid drag source",
            requested={"row_index": raw_row_index},
            accepted={"row_index": "non-negative integer"},
            next_step="Provide source.row_index as a non-negative integer.",
        )
    try:
        row_index = int(raw_row_index)
    except (TypeError, ValueError):
        return 0, _blocked(
            reason="invalid drag source",
            requested={"row_index": raw_row_index},
            accepted={"row_index": "non-negative integer"},
            next_step="Provide source.row_index as a non-negative integer.",
        )
    if row_index < 0:
        return 0, _blocked(
            reason="invalid drag source",
            requested={"row_index": raw_row_index},
            accepted={"row_index": "non-negative integer"},
            next_step="Provide source.row_index as a non-negative integer.",
        )
    return row_index, None


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


def _cancel_from_action(action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    cancel = action.get("cancel")
    if cancel is None:
        return {}, None
    if not isinstance(cancel, Mapping):
        return {}, _blocked(
            reason="invalid drag cancel request",
            requested={"cancel": cancel},
            accepted={"cancel": {"key": "escape"}},
            next_step="Provide cancel as an object with key: escape.",
        )
    normalized = dict(cancel)
    key = normalized.get("key")
    if key is None:
        return {}, None
    key_text = str(key).lower()
    if key_text not in {"escape", "esc"}:
        return {}, _blocked(
            reason="unsupported drag cancel key",
            requested={"cancel": normalized},
            accepted={"cancel.key": "escape"},
            next_step="Use cancel.key: escape for path-aware drag cancellation.",
        )
    normalized["key"] = "escape"
    return normalized, None


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
    cancel: dict[str, Any],
    identity: dict[str, Any],
    expect: dict[str, Any],
    duration_ms: int,
) -> dict[str, Any]:
    status = str(result.get("status", "PASS"))
    selected_payload = _selected_payload_from_result(result)
    no_op = _no_op_from_result(result)
    cleanup = _cleanup_from_result(result)
    route_evidence = _route_evidence_from_result(result)
    output: dict[str, Any] = {
        "status": status,
        "route": "drag",
        "source": source,
        "path": path,
        "drop": drop,
        "modifiers": modifiers,
        "duration_ms": duration_ms,
        "backend": result.get("backend"),
        "route_evidence": route_evidence,
    }
    if identity:
        output["identity"] = identity
    if cancel:
        output["cancel"] = cancel
    if output["route_evidence"]:
        output["route_evidence"].setdefault("source", source)
        output["route_evidence"].setdefault("move_points", path)
        output["route_evidence"].setdefault(
            "hold_points",
            [point for point in path if "hold_ms" in point],
        )
        output["route_evidence"].setdefault("final_pointer", drop or (path[-1] if path else None))
        output["route_evidence"].setdefault("modifiers", modifiers)
    if selected_payload is not None:
        output["selected_payload"] = selected_payload
    if no_op is not None:
        output["no_op"] = no_op
    if cleanup is not None:
        output["cleanup"] = cleanup
    if _is_passing(output) and expect.get("selected_payload_preserved") is True:
        if selected_payload is None:
            output["status"] = "BLOCKED"
            output["reason"] = "selected payload evidence unavailable"
            output["requested"] = {"expect": {"selected_payload_preserved": True}}
            output["accepted"] = {
                "selected_payload": "before and after selected row identities"
            }
            output["next_step"] = (
                "Use a UI backend or probe adapter that returns selected payload evidence."
            )
        elif selected_payload.get("preserved") is not True:
            output["status"] = "FAIL"
            output["reason"] = "selected payload expectation failed"
    if _is_passing(output) and expect.get("no_op") is True:
        expected_reason = expect.get("no_op_reason")
        if no_op is None:
            output["status"] = "BLOCKED"
            output["reason"] = "no-op evidence unavailable"
            output["requested"] = {"expect": {"no_op": True}}
            output["accepted"] = {
                "no_op": "adapter evidence with expected=true and reason"
            }
            output["next_step"] = (
                "Use a UI backend that reports no-op drag evidence for negative gestures."
            )
        elif cleanup is None:
            output["status"] = "BLOCKED"
            output["reason"] = "cleanup evidence unavailable"
            output["requested"] = {"expect": {"no_op": True}}
            output["accepted"] = {
                "cleanup": "modifier and pointer release evidence where observable"
            }
            output["next_step"] = (
                "Use a UI backend that reports cleanup evidence for drag gestures."
            )
        elif (
            no_op.get("expected") is not True
            or (
                expected_reason is not None
                and no_op.get("reason") != expected_reason
            )
        ):
            output["status"] = "FAIL"
            output["reason"] = "no-op expectation failed"
    if _is_passing(output) and not output["route_evidence"]:
        output["status"] = "BLOCKED"
        output["reason"] = "real pointer route evidence unavailable"
        output["requested"] = {
            "adapter_status": status,
            "route_evidence": None,
        }
        output["accepted"] = {
            "route_evidence": (
                "backend-produced pointer route evidence with move_points "
                "and final_pointer"
            )
        }
        output["next_step"] = (
            "Use a UI backend adapter that reports real pointer route evidence."
        )
    if status != "PASS":
        attach_blocked_details(output, result)
    evidence_ref = result.get("evidence_ref")
    if evidence_ref:
        output["evidence_ref"] = str(evidence_ref)
    return output


def _route_evidence_from_result(result: dict[str, Any]) -> dict[str, Any]:
    route_evidence = result.get("route_evidence")
    if not isinstance(route_evidence, Mapping):
        return {}
    compact_route = compact_evidence(dict(route_evidence))
    return compact_route if isinstance(compact_route, dict) else {}


def _is_passing(result: dict[str, Any]) -> bool:
    return str(result.get("status", "")).upper() == "PASS"


def _selected_payload_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    selected_payload = result.get("selected_payload")
    if not isinstance(selected_payload, Mapping):
        route_evidence = result.get("route_evidence")
        if isinstance(route_evidence, Mapping):
            selected_payload = route_evidence.get("selected_payload")
    if not isinstance(selected_payload, Mapping):
        return None

    output = compact_evidence(dict(selected_payload))
    if not isinstance(output, dict):
        return None
    before = _string_list(output.get("before"))
    after = _string_list(output.get("after"))
    if before is not None:
        output["before"] = before
    if after is not None:
        output["after"] = after
    if before is not None and after is not None:
        duplicate_identities = _duplicate_identities(before, after)
        lost_identities = _difference(before, after)
        unexpected_identities = _difference(after, before)
        if duplicate_identities:
            output["duplicate_identities"] = duplicate_identities
        if lost_identities:
            output["lost_identities"] = lost_identities
        if unexpected_identities:
            output["unexpected_identities"] = unexpected_identities
        output["preserved"] = (
            bool(before)
            and bool(after)
            and not duplicate_identities
            and not lost_identities
            and not unexpected_identities
        )
    return output


def _no_op_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    no_op = result.get("no_op")
    if not isinstance(no_op, Mapping):
        route_evidence = result.get("route_evidence")
        if isinstance(route_evidence, Mapping):
            no_op = route_evidence.get("no_op")
    if not isinstance(no_op, Mapping):
        return None
    output = compact_evidence(dict(no_op))
    return output if isinstance(output, dict) else None


def _cleanup_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    cleanup = result.get("cleanup")
    if isinstance(cleanup, Mapping):
        compact_cleanup = compact_evidence(dict(cleanup))
        return compact_cleanup if isinstance(compact_cleanup, dict) else None

    output: dict[str, Any] = {}
    for source in (result, result.get("result"), result.get("route_evidence")):
        if not isinstance(source, Mapping):
            continue
        for key in ("modifier_cleanup", "pointer_cleanup"):
            value = source.get(key)
            if isinstance(value, Mapping):
                output[key] = compact_evidence(dict(value))
    return output or None


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def _duplicate_identities(*groups: list[str]) -> list[str]:
    duplicates: list[str] = []
    for group in groups:
        seen: set[str] = set()
        for identity in group:
            if identity in seen and identity not in duplicates:
                duplicates.append(identity)
            seen.add(identity)
    return duplicates


def _difference(left: list[str], right: list[str]) -> list[str]:
    remaining = list(right)
    missing: list[str] = []
    for identity in left:
        if identity in remaining:
            remaining.remove(identity)
        else:
            missing.append(identity)
    return missing


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
