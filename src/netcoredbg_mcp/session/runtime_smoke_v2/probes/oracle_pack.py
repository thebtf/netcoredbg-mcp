from __future__ import annotations

import asyncio
import json
from typing import Any

from ...runtime_smoke_schema import DIAGNOSTIC_SCHEMA_VERSION
from ..blocked import build_blocked
from ..evidence_manifest import (
    DISAGREEING_SOURCES,
    ORACLE_SOURCE_BLOCKED,
    ORACLE_SOURCE_FAILED,
    ORACLE_SOURCE_IMPASSE,
    ORACLE_SOURCE_PASS,
)
from ._common import probe_name
from ._diagnostic_common import (
    bounded_diagnostic_value,
    diagnostic_limits,
    diagnostic_validation_errors,
    invalid_diagnostic_probe,
)


async def handle_oracle_pack(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "oracle_pack"
    errors = diagnostic_validation_errors(probe, kind=kind)
    if errors:
        return invalid_diagnostic_probe(probe, kind=kind, errors=errors)

    checks = [dict(check) for check in probe.get("checks", []) if isinstance(check, dict)]
    sources = await _run_sources(probe, context, phase=phase)
    status = str(probe.get("status") or "PASS")
    value = {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "id": str(probe.get("id") or probe_name(probe, kind)),
        "status": status,
        "check_count": len(checks),
        "checks": checks,
        "source_count": len(sources),
        "sources": sources,
        "limits": dict(probe.get("limits") or {}),
    }
    if probe.get("description"):
        value["description"] = str(probe["description"])
    disagreement = _source_disagreement(sources)
    source_status = _source_terminal_status(sources)
    disagreement_blocks_pack = (
        disagreement is not None and source_status == "PASS" and status == "PASS"
    )
    source_status_drives_pack = (
        not disagreement_blocks_pack and source_status != "PASS" and status == "PASS"
    )
    if disagreement_blocks_pack:
        status = "BLOCKED"
        value["status"] = status
        value["source_values"] = disagreement
    elif source_status_drives_pack:
        status = source_status
        value["status"] = status
    value["manifest"] = {
        "sources": _source_manifest_entries(
            sources,
            classification_override=DISAGREEING_SOURCES
            if disagreement_blocks_pack
            else None,
        )
    }
    limits = diagnostic_limits(probe)
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": bounded_diagnostic_value(value, limits=limits),
        "evidence_ref": f"diagnostic:oracle_pack:{value['id']}",
    }
    if disagreement_blocks_pack:
        output["classification"] = "DISAGREEING_SOURCES"
        output.update(
            build_blocked(
                reason="DISAGREEING_SOURCES",
                requested={"source_values": disagreement},
                accepted={"source_values": "all comparable source values agree"},
                next_step="Inspect source evidence and fix the disagreeing oracle inputs.",
            )
        )
    elif source_status_drives_pack and source_status == "BLOCKED":
        output.update(
            build_blocked(
                reason="oracle source blocked",
                requested={"sources": sources},
                accepted={"source_status": "PASS"},
                next_step="Inspect source evidence and make every source runnable.",
            )
        )
    elif source_status_drives_pack:
        if source_status == "FAIL":
            output["reason"] = "oracle source failed"
        elif source_status == "IMPASSE":
            output["reason"] = "oracle source impasse"
        else:
            output["reason"] = "oracle source reported non-PASS"
    elif status == "BLOCKED":
        output["reason"] = "oracle pack reported BLOCKED"
    elif status == "FAIL":
        output["reason"] = "oracle pack reported FAIL"
    return output


async def _run_sources(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> list[dict[str, Any]]:
    raw_sources = probe.get("sources")
    if not isinstance(raw_sources, list):
        return []

    tasks = [
        _run_source(index, raw_source, context, phase=phase)
        for index, raw_source in enumerate(raw_sources)
    ]
    return list(await asyncio.gather(*tasks))


async def _run_source(
    index: int,
    raw_source: Any,
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    if not isinstance(raw_source, dict):
        return {
            "id": f"source_{index}",
            "status": "BLOCKED",
            "reason": "oracle source must be an object",
        }
    source_id = str(raw_source.get("id") or raw_source.get("name") or f"source_{index}")
    raw_probe = raw_source.get("probe")
    if not isinstance(raw_probe, dict):
        return {
            "id": source_id,
            "status": "BLOCKED",
            "reason": "oracle source probe must be an object",
        }
    source_probe = dict(raw_probe)
    source_kind = str(source_probe.get("kind") or "")
    if source_kind == "oracle_pack":
        return {
            "id": source_id,
            "kind": source_kind,
            "status": "BLOCKED",
            "reason": "nested oracle_pack sources are not supported",
        }

    from ..probe_dispatcher import dispatch_probe

    result = await dispatch_probe(source_probe, context, phase=phase)
    return _source_summary(source_id, result)


def _source_summary(source_id: str, result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "id": source_id,
        "kind": str(result.get("kind") or ""),
        "status": str(result.get("status") or "PASS"),
    }
    if "value" in result:
        summary["value"] = result["value"]
    if result.get("reason"):
        summary["reason"] = str(result["reason"])
    if result.get("evidence_ref"):
        summary["evidence_ref"] = str(result["evidence_ref"])
    return summary


def _source_manifest_entries(
    sources: list[dict[str, Any]],
    *,
    classification_override: str | None = None,
) -> list[dict[str, Any]]:
    return [
        _source_manifest_entry(source, classification_override=classification_override)
        for source in sources
    ]


def _source_manifest_entry(
    source: dict[str, Any],
    *,
    classification_override: str | None,
) -> dict[str, Any]:
    entry = {
        "id": str(source.get("id") or ""),
        "kind": str(source.get("kind") or "unknown"),
        "status": str(source.get("status") or "PASS"),
        "classification": classification_override or _source_manifest_classification(source),
    }
    if source.get("reason"):
        entry["reason"] = str(source["reason"])
    if source.get("evidence_ref"):
        entry["evidence_ref"] = str(source["evidence_ref"])
    return entry


def _source_manifest_classification(source: dict[str, Any]) -> str:
    status = str(source.get("status") or "PASS")
    if status == "PASS":
        return ORACLE_SOURCE_PASS
    if status == "FAIL":
        return ORACLE_SOURCE_FAILED
    if status == "IMPASSE":
        return ORACLE_SOURCE_IMPASSE
    if status == "BLOCKED":
        return ORACLE_SOURCE_BLOCKED
    return f"ORACLE_SOURCE_{status}"


def _source_disagreement(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    comparable = {
        str(source["id"]): source.get("value")
        for source in sources
        if source.get("status") == "PASS" and "value" in source
    }
    if len(comparable) < 2:
        return None
    serialized = {_stable_json(value) for value in comparable.values()}
    if len(serialized) <= 1:
        return None
    return comparable


def _source_terminal_status(sources: list[dict[str, Any]]) -> str:
    statuses = {str(source.get("status") or "PASS") for source in sources}
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "IMPASSE" in statuses:
        return "IMPASSE"
    non_pass_statuses = sorted(status for status in statuses if status != "PASS")
    if non_pass_statuses:
        return non_pass_statuses[0]
    return "PASS"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
