from __future__ import annotations

from typing import Any

from ..blocked import build_blocked
from . import accepted_probe_kinds

_EXPECTED_MISSING = object()


def probe_name(probe: dict[str, Any], default: str) -> str:
    return str(probe.get("name") or default)


def service_available(context: Any, adapter_name: str) -> bool:
    return adapter_name in context.action_context.service_adapters


def blocked_probe(
    probe: dict[str, Any],
    *,
    kind: str,
    reason: str = "probe execution not available",
    requested: dict[str, Any] | None = None,
    accepted: dict[str, Any] | None = None,
    next_step: str = "Connect a service adapter that can execute this probe kind.",
) -> dict[str, Any]:
    return {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "BLOCKED",
        "value": None,
        **build_blocked(
            reason=reason,
            requested=dict(requested or {"kind": kind}),
            accepted=dict(accepted or {"probe_kinds": accepted_probe_kinds()}),
            next_step=next_step,
        ),
    }


def evidence_ref(result: dict[str, Any]) -> str | None:
    direct = result.get("evidence_ref")
    if direct:
        return str(direct)
    refs = result.get("evidence_refs")
    if isinstance(refs, list) and refs:
        first = refs[0]
        if isinstance(first, dict) and first.get("ref"):
            return str(first["ref"])
        return str(first)
    return None


def expected_for(probe: dict[str, Any]) -> Any:
    if "expected" in probe:
        return probe["expected"]
    if "expect" in probe:
        return probe["expect"]
    return _EXPECTED_MISSING


def attach_expected_and_status(
    output: dict[str, Any],
    *,
    probe: dict[str, Any],
    phase: str,
    value: Any,
    reason: str = "expected value did not match",
) -> dict[str, Any]:
    expected = expected_for(probe)
    if expected is _EXPECTED_MISSING:
        return output
    output["expected"] = expected
    if phase == "after" and output.get("status") == "PASS" and not _matches(value, expected):
        output["status"] = "FAIL"
        output["reason"] = reason
    return output


def _matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, str) and expected.startswith("contains:"):
        return expected.removeprefix("contains:") in str(value)
    return bool(value == expected)
