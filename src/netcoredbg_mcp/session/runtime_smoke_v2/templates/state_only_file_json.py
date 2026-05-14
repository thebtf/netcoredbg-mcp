from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._helpers import render_case_id
from ._substituter import render_template_value


def render_state_only_file_json(
    record: dict[str, Any],
    id_pattern: str,
) -> dict[str, Any]:
    transition: dict[str, Any] = {
        "probes": _file_json_probes(record),
    }
    action = _action_from_record(record)
    if action is not None:
        transition["action"] = action
    settle = _settle_from_record(record)
    if settle is not None:
        transition["settle"] = settle

    return {
        "id": render_case_id(id_pattern, record),
        "transitions": [transition],
    }


def _file_json_probes(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw_oracles = record.get("oracles")
    oracles = raw_oracles if isinstance(raw_oracles, list) else [_single_oracle(record)]
    probes: list[dict[str, Any]] = []
    for index, raw_oracle in enumerate(oracles):
        if not isinstance(raw_oracle, dict):
            continue
        oracle = dict(raw_oracle)
        path = oracle.get("path", record.get("path", record.get("evidence_path", "")))
        jsonpath = oracle.get("jsonpath", oracle.get("path_expr", record.get("jsonpath", "")))
        probe = {
            "kind": "file.json",
            "phase": str(oracle.get("phase", "after")),
            "name": str(oracle.get("name", f"oracle_{index}")),
            "path": render_template_value(path, record),
            "jsonpath": render_template_value(jsonpath, record),
        }
        if "expected" in oracle:
            probe["expected"] = render_template_value(oracle["expected"], record)
        elif "expect" in oracle:
            probe["expect"] = render_template_value(oracle["expect"], record)
        probes.append(probe)
    return probes


def _single_oracle(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("name", "value"),
        "jsonpath": record.get("jsonpath", ""),
        "expected": record.get("expected"),
    }


def _action_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    action = record.get("action")
    if isinstance(action, dict):
        return deepcopy(render_template_value(action, record))
    if "wait_ms" in record:
        return {"kind": "wait", "idle_ms": render_template_value(record["wait_ms"], record)}
    return None


def _settle_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    settle = record.get("settle")
    if isinstance(settle, dict):
        return deepcopy(render_template_value(settle, record))
    if "settle_ms" in record:
        return {"idle_ms": render_template_value(record["settle_ms"], record)}
    return None
