"""Focused UI query, snapshot, and diff evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .serialization import bound_elements, selector_ref

ALLOWED_UI_FIELDS = (
    "focus",
    "selection",
    "value",
    "text",
    "enabled",
    "visible",
    "window",
)


@dataclass
class UISnapshotStore:
    """Session-scoped UI snapshot storage."""

    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    def save(self, snapshot: dict[str, Any]) -> None:
        self.snapshots[str(snapshot["snapshot"])] = dict(snapshot)

    def get(self, name: str) -> dict[str, Any]:
        if name not in self.snapshots:
            raise KeyError(f"UI snapshot not found: {name}")
        return self.snapshots[name]

    def has(self, name: str) -> bool:
        return name in self.snapshots

    def names(self) -> list[str]:
        return sorted(self.snapshots)


async def query_ui_fields(
    backend: Any,
    selector: dict[str, Any],
    *,
    fields: list[str],
    max_results: int = 20,
) -> dict[str, Any]:
    """Query focused UI fields without returning an unbounded tree."""
    invalid = invalid_ui_fields(fields)
    if invalid:
        return {
            "status": "FAIL",
            "reason": "unknown UI fields",
            "invalid_fields": invalid,
            "allowed_fields": list(ALLOWED_UI_FIELDS),
        }

    tried_grid_selection = _is_datagrid_selection_query(selector, fields)
    if tried_grid_selection:
        grid_selection = await _query_datagrid_selection(backend, selector)
        if grid_selection is not None:
            return grid_selection

    result = await backend.query_ui(dict(selector), list(fields), max_results=max_results)
    if result.get("unsupported") is True or result.get("status") in {"BLOCKED", "UNSUPPORTED"}:
        if (
            not tried_grid_selection
            and _can_try_datagrid_selection_fallback(selector, fields)
        ):
            grid_selection = await _query_datagrid_selection(backend, selector)
            if grid_selection is not None and grid_selection.get("status") != "BLOCKED":
                return grid_selection
        return {
            **result,
            "status": "BLOCKED",
            "elements": [],
        }

    raw_elements = result.get("elements") or []
    if not isinstance(raw_elements, list):
        raw_elements = []
    bounded = bound_elements(
        [dict(element) for element in raw_elements if isinstance(element, dict)],
        fields=fields,
        max_results=max_results,
    )
    bounded = _merge_reported_counts(bounded, result)
    evidence_ref = {
        "kind": "ui_query",
        "ref": f"ui_query:{selector_ref(selector)}",
        "summary": (
            f"returned={bounded['returned_count']} omitted={bounded['omitted_count']} "
            f"fields={','.join(fields)}"
        ),
    }
    return {
        "status": result.get("status", "PASS"),
        "fields": list(fields),
        **bounded,
        "evidence_refs": [evidence_ref],
    }


async def capture_ui_snapshot(
    backend: Any,
    store: UISnapshotStore,
    *,
    name: str,
    selector: dict[str, Any],
    fields: list[str],
    max_results: int = 20,
) -> dict[str, Any]:
    """Capture a named field-limited UI snapshot."""
    if store.has(name):
        return {
            "status": "FAIL",
            "reason": "snapshot name already exists",
            "snapshot": name,
            "available_snapshots": store.names(),
        }
    result = await query_ui_fields(
        backend,
        selector,
        fields=fields,
        max_results=max_results,
    )
    if result.get("status") != "PASS":
        return result
    snapshot = {
        **result,
        "snapshot": name,
        "selector": dict(selector),
    }
    store.save(snapshot)
    return snapshot


def diff_ui_snapshots(
    store: UISnapshotStore,
    before: str,
    after: str,
    *,
    fields: list[str],
) -> dict[str, Any]:
    """Diff two named snapshots and omit unchanged fields."""
    invalid = invalid_ui_fields(fields)
    if invalid:
        return {
            "status": "FAIL",
            "reason": "unknown UI fields",
            "invalid_fields": invalid,
            "allowed_fields": list(ALLOWED_UI_FIELDS),
        }
    if not store.has(before) or not store.has(after):
        return {
            "status": "FAIL",
            "reason": "snapshot not found",
            "before": before,
            "after": after,
            "available_snapshots": store.names(),
        }

    before_elements = _by_id(store.get(before).get("elements", []))
    after_elements = _by_id(store.get(after).get("elements", []))
    added = [
        _project(after_elements[key], fields)
        for key in sorted(set(after_elements) - set(before_elements))
    ]
    removed = [
        _project(before_elements[key], fields)
        for key in sorted(set(before_elements) - set(after_elements))
    ]
    changed = []
    for key in sorted(set(before_elements) & set(after_elements)):
        changes = {}
        for field_name in fields:
            before_value = before_elements[key].get(field_name)
            after_value = after_elements[key].get(field_name)
            if before_value != after_value:
                changes[field_name] = {"before": before_value, "after": after_value}
        if changes:
            changed.append({"element_id": key, "changes": changes})

    return {
        "status": "PASS",
        "before": before,
        "after": after,
        "fields": list(fields),
        "added": added,
        "removed": removed,
        "changed": changed,
        "evidence_refs": [
            {
                "kind": "ui_diff",
                "ref": f"ui_diff:{before}->{after}",
                "summary": f"added={len(added)} removed={len(removed)} changed={len(changed)}",
            }
        ],
    }


def invalid_ui_fields(fields: list[str]) -> list[str]:
    allowed = set(ALLOWED_UI_FIELDS)
    return sorted(field for field in fields if field not in allowed)


def _is_datagrid_selection_query(selector: dict[str, Any], fields: list[str]) -> bool:
    if list(fields) != ["selection"]:
        return False
    if selector.get("xpath"):
        return False
    control_type = str(
        selector.get("control_type")
        or selector.get("controlType")
        or ""
    ).lower()
    return control_type in {"datagrid", "data_grid", "grid"}


def _can_try_datagrid_selection_fallback(selector: dict[str, Any], fields: list[str]) -> bool:
    if list(fields) != ["selection"]:
        return False
    if selector.get("xpath"):
        return False
    return bool(
        selector.get("automation_id")
        or selector.get("automationId")
        or selector.get("name")
    )


async def _query_datagrid_selection(
    backend: Any,
    selector: dict[str, Any],
) -> dict[str, Any] | None:
    grid_selected_rows = getattr(backend, "grid_selected_rows", None)
    if not callable(grid_selected_rows):
        return None
    result = await grid_selected_rows(dict(selector), columns=[])
    if not isinstance(result, dict):
        return None
    if result.get("unsupported") is True or result.get("status") in {"BLOCKED", "UNSUPPORTED"}:
        return {
            **result,
            "status": "BLOCKED",
            "elements": [],
        }
    selected_rows = result.get("selected_rows")
    if not isinstance(selected_rows, list):
        selected_rows = []
    selected_rows = [
        _strip_unbounded_evidence(row)
        for row in selected_rows
        if isinstance(row, dict)
    ]
    element = {
        "element_id": selector_ref(selector),
        "selection": {
            "source": "grid_selected_rows",
            "selected_count": len(selected_rows),
            "selected_rows": selected_rows,
        },
    }
    evidence_ref = {
        "kind": "ui_query",
        "ref": f"ui_query:{selector_ref(selector)}",
        "summary": (
            f"returned=1 omitted=0 fields=selection "
            f"selected_rows={len(selected_rows)}"
        ),
    }
    return {
        "status": result.get("status", "PASS"),
        "fields": ["selection"],
        "elements": [element],
        "element_count": 1,
        "returned_count": 1,
        "omitted_count": 0,
        "evidence_refs": [evidence_ref],
    }


def _strip_unbounded_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_unbounded_evidence(item)
            for key, item in value.items()
            if key not in {"full_tree", "raw_tree", "ui_tree", "window_tree"}
        }
    if isinstance(value, list):
        return [_strip_unbounded_evidence(item) for item in value]
    return value


def _merge_reported_counts(
    bounded: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    element_count = _nonnegative_int(result.get("element_count"))
    if element_count is None or element_count < bounded["returned_count"]:
        return bounded
    return {
        **bounded,
        "element_count": element_count,
        "omitted_count": max(0, element_count - bounded["returned_count"]),
    }


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _by_id(elements: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        element_id = str(element.get("element_id") or "")
        if element_id:
            result[element_id] = dict(element)
    return result


def _project(element: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    element_id = str(element.get("element_id") or "unknown")
    return {
        "element_id": element_id,
        **{field: element[field] for field in fields if field in element},
    }
