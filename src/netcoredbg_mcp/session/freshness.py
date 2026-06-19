"""Debug session freshness evidence for runtime smoke scenarios."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DebugFreshnessResult:
    """Serializable debug freshness verification result."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class DebugFreshnessVerifier:
    """Verify that a debug session still matches the intended runtime target."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def verify(
        self,
        *,
        expected_process_id: int | None = None,
        expected_process_name: str | None = None,
        expected_workspace: str | None = None,
        expected_sources: list[str] | None = None,
        expected_modules: list[str] | None = None,
        expected_artifacts: list[str] | None = None,
        require_active_process: bool = False,
    ) -> DebugFreshnessResult:
        state = getattr(self._session, "state", None)
        process_id = _state_value(self._session, state, "process_id")
        process_name = _state_value(self._session, state, "process_name")
        project_path = getattr(self._session, "project_path", None)
        loaded_sources = _source_records(_state_value(self._session, state, "loaded_sources") or {})
        modules = _module_records(_state_value(self._session, state, "modules") or [])
        workspace = _display_path(expected_workspace) if expected_workspace else None
        expected_source_paths = [_display_path(path) for path in expected_sources or []]
        expected_module_paths = [_display_path(path) for path in expected_modules or []]
        expected_artifact_paths = [_display_path(path) for path in expected_artifacts or []]
        warnings: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []

        if expected_process_id is not None or require_active_process:
            if process_id is None:
                warnings.append(
                    {
                        "kind": "process_id_unavailable",
                        "expected": expected_process_id,
                    }
                )
            elif expected_process_id is not None and process_id != expected_process_id:
                mismatches.append(
                    {
                        "kind": "process_id_mismatch",
                        "expected": expected_process_id,
                        "actual": process_id,
                    }
                )

        if expected_process_name:
            if not process_name:
                warnings.append(
                    {
                        "kind": "process_name_unavailable",
                        "expected": expected_process_name,
                    }
                )
            elif process_name.casefold() != expected_process_name.casefold():
                mismatches.append(
                    {
                        "kind": "process_name_mismatch",
                        "expected": expected_process_name,
                        "actual": process_name,
                    }
                )

        workspace_payload: dict[str, Any] = {
            "expected": workspace,
            "session_project_path": _display_path(project_path) if project_path else None,
            "matched": None,
        }
        if workspace:
            if project_path:
                workspace_payload["matched"] = _same_path(project_path, workspace)
                if not workspace_payload["matched"]:
                    mismatches.append(
                        {
                            "kind": "workspace_mismatch",
                            "expected": workspace,
                            "actual": _display_path(project_path),
                        }
                    )
            else:
                warnings.append({"kind": "workspace_unavailable", "expected": workspace})

        source_paths = [record["path"] for record in loaded_sources if record.get("path")]
        normalized_sources = {_normalize_path(path) for path in source_paths}
        if expected_source_paths and not source_paths:
            warnings.append(
                {
                    "kind": "loaded_sources_unavailable",
                    "expected": expected_source_paths,
                }
            )
        for path in expected_source_paths:
            if source_paths and _normalize_path(path) not in normalized_sources:
                mismatches.append({"kind": "expected_source_missing", "expected": path})
        if workspace and source_paths:
            for path in source_paths:
                if not _is_within(path, workspace):
                    mismatches.append(
                        {
                            "kind": "source_workspace_mismatch",
                            "expected_workspace": workspace,
                            "actual": path,
                        }
                    )

        module_paths = [record["path"] for record in modules if record.get("path")]
        module_names = {str(record.get("name") or "").casefold() for record in modules}
        normalized_modules = {_normalize_path(path) for path in module_paths}
        if expected_module_paths and not modules:
            warnings.append(
                {
                    "kind": "modules_unavailable",
                    "expected": expected_module_paths,
                }
            )
        for path in expected_module_paths:
            normalized = _normalize_path(path)
            expected_name = os.path.basename(path).casefold()
            if (
                modules
                and normalized not in normalized_modules
                and expected_name not in module_names
            ):
                mismatches.append({"kind": "expected_module_missing", "expected": path})

        existing_artifacts = []
        missing_artifacts = []
        for path in expected_artifact_paths:
            if os.path.exists(path):
                existing_artifacts.append(path)
            else:
                missing_artifacts.append(path)
                mismatches.append({"kind": "artifact_missing", "expected": path})

        status = "FAIL" if mismatches else "WARN" if warnings else "PASS"
        payload = {
            "status": status,
            "reason": _reason(status),
            "process": {
                "process_id": process_id,
                "process_name": process_name,
                "expected_process_id": expected_process_id,
                "expected_process_name": expected_process_name,
            },
            "workspace": workspace_payload,
            "loaded_sources": {
                "count": len(source_paths),
                "paths": source_paths,
                "expected": expected_source_paths,
            },
            "modules": {
                "count": len(modules),
                "paths": module_paths,
                "names": sorted(module_names),
                "expected": expected_module_paths,
                "records": modules,
            },
            "artifacts": {
                "existing": existing_artifacts,
                "missing": missing_artifacts,
            },
            "warnings": warnings,
            "mismatches": mismatches,
        }
        runtime_smoke = getattr(self._session, "runtime_smoke", None)
        if runtime_smoke is not None:
            runtime_smoke.freshness_evidence["latest"] = dict(payload)
        return DebugFreshnessResult(payload)


def _reason(status: str) -> str:
    if status == "PASS":
        return "debug freshness verified"
    if status == "WARN":
        return "debug freshness evidence incomplete"
    return "debug freshness mismatch"


def _state_value(session: Any, state: Any, name: str) -> Any:
    if state is not None and hasattr(state, name):
        return getattr(state, name)
    return getattr(session, name, None)


def _source_records(raw_sources: Any) -> list[dict[str, Any]]:
    values = raw_sources.values() if isinstance(raw_sources, dict) else raw_sources
    return [_record_from_any(item) for item in values]


def _module_records(raw_modules: Any) -> list[dict[str, Any]]:
    return [_record_from_any(item) for item in raw_modules]


def _record_from_any(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        data = item.to_dict()
    elif isinstance(item, dict):
        data = dict(item)
    else:
        data = {
            "name": getattr(item, "name", None),
            "path": getattr(item, "path", None),
        }
    path = data.get("path")
    record = {
        "name": data.get("name") or (os.path.basename(path) if path else None),
        "path": _display_path(path) if path else None,
    }
    for key in (
        "id",
        "version",
        "isOptimized",
        "symbolStatus",
        "sourceReference",
        "origin",
        "presentationHint",
    ):
        value = data.get(key) if key in data else getattr(item, key, None)
        if value is not None:
            record[key] = value
    return record


def _display_path(path: Any) -> str:
    return os.path.normpath(os.fspath(path))


def _normalize_path(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _same_path(left: Any, right: Any) -> bool:
    return _normalize_path(left) == _normalize_path(right)


def _is_within(path: Any, root: Any) -> bool:
    normalized_path = _normalize_path(path)
    normalized_root = _normalize_path(root)
    try:
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except ValueError:
        return False
