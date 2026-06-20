from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._substituter import render_template_value


def render_case_id(pattern: str, record: dict[str, Any]) -> str:
    return str(render_template_value(pattern, record))


def selector_from_record(
    record: dict[str, Any],
    *,
    selector_key: str = "selector",
    automation_key: str = "control",
) -> dict[str, Any]:
    selector = record.get(selector_key)
    if isinstance(selector, dict):
        return deepcopy(selector)
    automation_id = record.get(automation_key)
    if automation_id is None:
        automation_id = record.get("automation_id")
    return {"automation_id": str(automation_id or "")}


def keyboard_action(record: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": "ui.key_sequence",
        "selector": deepcopy(selector),
        "keys": str(record.get("keys") or "{SPACE}"),
    }
    if "realize" in record:
        action["realize"] = bool(record["realize"])
    return action


def file_json_probes_from_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw_oracles = record.get("oracles")
    oracles = raw_oracles if isinstance(raw_oracles, list) else [_single_oracle(record)]
    probes: list[dict[str, Any]] = []
    for index, raw_oracle in enumerate(oracles):
        if not isinstance(raw_oracle, dict):
            continue
        oracle = dict(raw_oracle)
        kind = str(oracle.get("kind") or "file.json")
        if kind != "file.json":
            probe = deepcopy(render_template_value(oracle, record))
            probe["kind"] = kind
            probe.setdefault("phase", str(oracle.get("phase", "after")))
            probe.setdefault("name", str(oracle.get("name", f"oracle_{index}")))
            probes.append(probe)
            continue
        path = oracle.get("path", record.get("path", record.get("evidence_path", "")))
        jsonpath = oracle.get("jsonpath", oracle.get("path_expr", record.get("jsonpath", "")))
        probe = {
            "kind": kind,
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


def action_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    action = record.get("action")
    if isinstance(action, dict):
        return deepcopy(render_template_value(action, record))
    if "wait_ms" in record:
        return {"kind": "wait", "idle_ms": render_template_value(record["wait_ms"], record)}
    return None


def settle_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    settle = record.get("settle")
    if isinstance(settle, dict):
        return deepcopy(render_template_value(settle, record))
    if "settle_ms" in record:
        return {"idle_ms": render_template_value(record["settle_ms"], record)}
    return None


def _single_oracle(record: dict[str, Any]) -> dict[str, Any]:
    oracle: dict[str, Any] = {
        "name": record.get("name", "value"),
        "jsonpath": record.get("jsonpath", ""),
    }
    if "expected" in record:
        oracle["expected"] = record["expected"]
    elif "expect" in record:
        oracle["expect"] = record["expect"]
    return oracle
