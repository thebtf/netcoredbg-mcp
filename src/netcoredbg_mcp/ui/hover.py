from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HOVER_TIMEOUT_MIN_MS = 1
HOVER_TIMEOUT_MAX_MS = 30_000
HOVER_TIMEOUT_ERROR = "timeout_ms must be an integer from 1 to 30000"

HOVER_SUCCESS_FIELDS = (
    "resolvedSelector",
    "target",
    "matchCount",
    "targetRootHwnd",
    "targetProcessId",
    "foregroundHwndBefore",
    "foregroundHwndAfter",
    "foregroundVerified",
    "focusBefore",
    "focusAfter",
    "focusUnchanged",
    "targetRect",
    "requestedPoint",
    "actualPointer",
    "hitElement",
    "hitRelation",
    "underPointer",
    "hovered",
    "click",
    "button",
    "timeoutMs",
    "elapsedMs",
    "pointerMutationState",
)

_BOUNDED_KEYS = frozenset(
    {
        *HOVER_SUCCESS_FIELDS,
        "status",
        "reason",
        "phase",
        "timeoutMs",
        "elapsedMs",
        "pointerMutationState",
        "requested",
        "accepted",
        "next_step",
        "backend",
        "capability",
        "evidence",
        "missing",
        "malformed",
        "contradictions",
        "virtualScreen",
        "isOffscreen",
        "search",
        "rootMatchCount",
    }
)


def validate_hover_timeout(timeout_ms: Any) -> int:
    if (
        type(timeout_ms) is not int
        or not HOVER_TIMEOUT_MIN_MS <= timeout_ms <= HOVER_TIMEOUT_MAX_MS
    ):
        raise ValueError(HOVER_TIMEOUT_ERROR)
    return timeout_ms


def hover_selector(
    *,
    automation_id: str | None = None,
    name: str | None = None,
    control_type: str | None = None,
    root_id: str | None = None,
    xpath: str | None = None,
) -> dict[str, str]:
    selector: dict[str, str] = {}
    if automation_id is not None:
        selector["automation_id"] = automation_id
    if name is not None:
        selector["name"] = name
    if control_type is not None:
        selector["control_type"] = control_type
    if root_id is not None:
        selector["root_id"] = root_id
    if xpath is not None:
        selector["xpath"] = xpath
    return selector


def bounded_hover_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _bounded_value(value)
        for key, value in result.items()
        if key in _BOUNDED_KEYS
    }


def validate_hover_evidence(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return _malformed_hover_result(
            missing=[],
            malformed=["result"],
            evidence={"result_type": type(result).__name__},
        )

    bounded = bounded_hover_evidence(result)
    status = str(result.get("status") or "").upper()
    if status != "PASS":
        return bounded

    missing = [field for field in HOVER_SUCCESS_FIELDS if field not in result]
    malformed = _malformed_success_fields(result)
    if missing or malformed:
        return _malformed_hover_result(
            missing=missing,
            malformed=malformed,
            evidence=bounded,
        )

    contradictions = _hover_contradictions(result)
    if contradictions:
        return {
            "status": "FAIL",
            "reason": "hover evidence contradicted the required contract",
            "contradictions": contradictions,
            "requested": {"hover_evidence": "complete PASS contract"},
            "accepted": {
                "hovered": True,
                "underPointer": True,
                "foregroundVerified": True,
                "focusUnchanged": True,
                "click": False,
                "button": "none",
                "matchCount": 1,
                "hitRelation": ["self", "descendant"],
                "pointerMutationState": "moved",
            },
            "next_step": (
                "Inspect foreground, focus, hit-test, pointer, and selector evidence "
                "before retrying."
            ),
            "evidence": bounded,
        }

    return bounded


def _malformed_success_fields(result: Mapping[str, Any]) -> list[str]:
    malformed: list[str] = []
    for field in ("resolvedSelector", "target", "focusBefore", "focusAfter", "hitElement"):
        if field in result and not isinstance(result[field], Mapping):
            malformed.append(field)
    for field in (
        "matchCount",
        "targetRootHwnd",
        "targetProcessId",
        "foregroundHwndBefore",
        "foregroundHwndAfter",
        "timeoutMs",
        "elapsedMs",
    ):
        if field in result and type(result[field]) is not int:
            malformed.append(field)
    for field in (
        "foregroundVerified",
        "focusUnchanged",
        "underPointer",
        "hovered",
        "click",
    ):
        if field in result and type(result[field]) is not bool:
            malformed.append(field)
    for field in ("targetRect", "requestedPoint", "actualPointer"):
        if field in result and not _valid_geometry(result[field], rectangle=field == "targetRect"):
            malformed.append(field)
    if "hitRelation" in result and not isinstance(result["hitRelation"], str):
        malformed.append("hitRelation")
    if "button" in result and not isinstance(result["button"], str):
        malformed.append("button")
    if "pointerMutationState" in result and not isinstance(result["pointerMutationState"], str):
        malformed.append("pointerMutationState")
    return sorted(set(malformed))


def _valid_geometry(value: Any, *, rectangle: bool) -> bool:
    if not isinstance(value, Mapping):
        return False
    keys = ("x", "y", "width", "height") if rectangle else ("x", "y")
    if any(type(value.get(key)) not in {int, float} for key in keys):
        return False
    if rectangle and (value["width"] <= 0 or value["height"] <= 0):
        return False
    return True


def _hover_contradictions(result: Mapping[str, Any]) -> list[str]:
    contradictions: list[str] = []
    target_hwnd = result["targetRootHwnd"]
    if result["matchCount"] != 1:
        contradictions.append("matchCount must equal 1")
    if result["targetRootHwnd"] <= 0:
        contradictions.append("targetRootHwnd must be positive")
    if result["targetProcessId"] <= 0:
        contradictions.append("targetProcessId must be positive")
    if result["foregroundHwndBefore"] != target_hwnd:
        contradictions.append("foregroundHwndBefore must equal targetRootHwnd")
    if result["foregroundHwndAfter"] != target_hwnd:
        contradictions.append("foregroundHwndAfter must equal targetRootHwnd")
    if result["foregroundVerified"] is not True:
        contradictions.append("foregroundVerified must be true")
    if result["focusUnchanged"] is not True:
        contradictions.append("focusUnchanged must be true")
    focus_identity_keys = ("automationId", "name", "controlType", "className")
    if any(
        result["focusBefore"].get(key) != result["focusAfter"].get(key)
        for key in focus_identity_keys
    ):
        contradictions.append("focusBefore and focusAfter identities must match")
    if result["underPointer"] is not True:
        contradictions.append("underPointer must be true")
    if result["hovered"] is not True:
        contradictions.append("hovered must be true")
    if result["click"] is not False:
        contradictions.append("click must be false")
    if result["button"] != "none":
        contradictions.append("button must equal none")
    if result["hitRelation"] not in {"self", "descendant"}:
        contradictions.append("hitRelation must be self or descendant")
    if not _point_inside_rect(result["actualPointer"], result["targetRect"]):
        contradictions.append("actualPointer must be inside targetRect")
    if not HOVER_TIMEOUT_MIN_MS <= result["timeoutMs"] <= HOVER_TIMEOUT_MAX_MS:
        contradictions.append("timeoutMs must be an integer from 1 to 30000")
    if result["elapsedMs"] < 0 or result["elapsedMs"] > result["timeoutMs"]:
        contradictions.append("elapsedMs must be non-negative and not greater than timeoutMs")
    if result["pointerMutationState"] != "moved":
        contradictions.append("pointerMutationState must equal moved")
    return contradictions


def _point_inside_rect(point: Mapping[str, Any], rect: Mapping[str, Any]) -> bool:
    return (
        rect["x"] <= point["x"] < rect["x"] + rect["width"]
        and rect["y"] <= point["y"] < rect["y"] + rect["height"]
    )


def _malformed_hover_result(
    *,
    missing: list[str],
    malformed: list[str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "hover backend returned malformed success evidence",
        "missing": sorted(missing),
        "malformed": sorted(malformed),
        "requested": {"required_fields": list(HOVER_SUCCESS_FIELDS)},
        "accepted": {"hover_evidence": "complete typed PASS contract"},
        "next_step": "Use the FlaUI hover bridge and return every required evidence field.",
        "evidence": _bounded_value(dict(evidence)),
    }


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<truncated>"
    if isinstance(value, Mapping):
        items = list(value.items())[:40]
        return {str(key): _bounded_value(item, depth=depth + 1) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:1000]
    if value is None or type(value) in {bool, int, float}:
        return value
    return repr(value)[:1000]
