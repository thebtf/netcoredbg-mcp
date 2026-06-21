from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...freshness import DebugFreshnessVerifier
from ...runtime_smoke_schema import DIAGNOSTIC_SCHEMA_VERSION
from ..blocked import build_blocked
from ..evidence_manifest import (
    APP_DIAGNOSTICS_BLOCKED,
    APP_DIAGNOSTICS_MISSING,
    APP_DIAGNOSTICS_OBSERVED,
    APP_DIAGNOSTICS_REPORTED,
    APP_DIAGNOSTICS_STALE,
    APP_DIAGNOSTICS_UNREADABLE,
)
from ..result_envelope import compact_value
from ..timing import sleep_ms
from ._common import probe_name
from ._diagnostic_common import (
    blocked_details_from_first_observation,
    bounded_diagnostic_value,
    diagnostic_limits,
    diagnostic_validation_errors,
    invalid_diagnostic_probe,
)

LAUNCH_DIAGNOSTIC_WAIT_TIMEOUT_MS = 0
LAUNCH_DIAGNOSTIC_WAIT_POLL_INTERVAL_MS = 50


async def handle_app_diagnostics(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "app_diagnostics"
    base_errors = diagnostic_validation_errors(probe, kind=kind)
    if base_errors:
        return invalid_diagnostic_probe(probe, kind=kind, errors=base_errors)

    probe, acquisition, acquisition_field = await _probe_with_diagnostic_json(
        probe,
        context,
        phase=phase,
    )
    if acquisition is not None and acquisition.get("observed") is not True:
        return _blocked_diagnostic_json_probe(
            probe,
            acquisition,
            field=acquisition_field or "wait_json",
        )

    merged_errors = diagnostic_validation_errors(probe, kind=kind)
    if merged_errors:
        return invalid_diagnostic_probe(probe, kind=kind, errors=merged_errors)

    observations = [
        dict(observation)
        for observation in probe.get("observations", [])
        if isinstance(observation, dict)
    ]
    app = dict(probe.get("app") or {})
    status = str(probe.get("status") or "PASS")
    value = {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "app": app,
        "status": status,
        "observation_count": len(observations),
        "observations": observations,
        "limits": dict(probe.get("limits") or {}),
    }
    if acquisition is not None and acquisition_field is not None:
        value[acquisition_field] = acquisition
    freshness = _verify_declared_freshness(probe, context)
    if freshness is not None:
        value["freshness"] = freshness
        if status == "PASS" and freshness.get("status") == "FAIL":
            status = "FAIL"
            value["status"] = status
    value["manifest"] = {
        "sources": [
            _manifest_source_entry(
                probe,
                app=app,
                status=status,
                acquisition=acquisition,
                field=acquisition_field,
                freshness=freshness,
            )
        ]
    }
    limits = diagnostic_limits(probe)
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": bounded_diagnostic_value(value, limits=limits),
        "evidence_ref": f"diagnostic:app_diagnostics:{app.get('name') or 'app'}",
    }
    if status == "BLOCKED":
        output["reason"] = "app diagnostics reported BLOCKED"
        output.update(blocked_details_from_first_observation(observations))
    elif status == "FAIL" and freshness is not None and freshness.get("status") == "FAIL":
        output["reason"] = "app diagnostics freshness mismatch"
    elif status == "FAIL":
        output["reason"] = "app diagnostics reported FAIL"
    return output


def _verify_declared_freshness(probe: dict[str, Any], context: Any) -> dict[str, Any] | None:
    expectations = _freshness_expectations(probe)
    if expectations is None:
        return None
    session = getattr(getattr(context, "action_context", None), "session", None)
    if session is None:
        return {
            "status": "WARN",
            "reason": "debug freshness evidence incomplete",
            "warnings": [
                {
                    "kind": "session_unavailable",
                    "reason": (
                        "app diagnostics freshness expectations declared but no session "
                        "is available"
                    ),
                }
            ],
            "mismatches": [],
        }
    return DebugFreshnessVerifier(session).verify(**expectations).to_dict()


def _freshness_expectations(probe: dict[str, Any]) -> dict[str, Any] | None:
    app = _object_or_empty(probe.get("app"))
    process = _object_or_empty(probe.get("process"))
    expectations = {
        "expected_process_id": _int_or_none(
            app.get("process_id")
            or app.get("expected_process_id")
            or process.get("process_id")
            or process.get("id")
            or process.get("expected_process_id")
            or process.get("expected_id")
        ),
        "expected_process_name": _str_or_none(
            app.get("process_name")
            or app.get("expected_process_name")
            or process.get("process_name")
            or process.get("name")
            or process.get("expected_process_name")
            or process.get("expected_name")
        ),
        "expected_workspace": _path_or_none(probe.get("workspace")),
        "expected_sources": _string_list(
            app.get("expected_sources")
            or probe.get("loaded_sources")
            or probe.get("sources")
            or _object_or_empty(probe.get("workspace")).get("sources")
        ),
        "expected_modules": _string_list(
            app.get("expected_modules") or _expected_collection(probe.get("modules"))
        ),
        "expected_artifacts": _string_list(_expected_collection(probe.get("artifacts"))),
        "require_active_process": bool(
            app.get("require_active_process")
            or process.get("require_active_process")
            or process.get("require_active")
        ),
    }
    has_expectations = any(
        expectations[key]
        for key in (
            "expected_process_id",
            "expected_process_name",
            "expected_workspace",
            "expected_sources",
            "expected_modules",
            "expected_artifacts",
            "require_active_process",
        )
    )
    return expectations if has_expectations else None


def _object_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _path_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return _str_or_none(
            value.get("path")
            or value.get("expected")
            or value.get("expected_path")
            or value.get("root")
        )
    return None


def _expected_collection(value: Any) -> Any:
    if isinstance(value, dict):
        return (
            value.get("expected")
            or value.get("paths")
            or value.get("names")
            or value.get("modules")
            or value.get("artifacts")
        )
    return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        path = value.get("path") or value.get("name")
        return [str(path)] if path else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item_value = item.get("path") or item.get("name")
                if item_value:
                    items.append(str(item_value))
            elif item:
                items.append(str(item))
        return items
    return []


async def _probe_with_diagnostic_json(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    field, source = _diagnostic_json_source(probe, context)
    if source is None:
        return probe, None, None

    async def progress_reporter(metadata: dict[str, Any]) -> None:
        await _publish_app_diagnostics_progress(
            probe,
            context,
            phase=phase,
            field=field,
            metadata=metadata,
        )

    acquired, metadata = await _read_wait_json(
        source,
        context,
        progress_reporter=progress_reporter,
    )
    if acquired is None:
        return probe, metadata, field
    merged = _merge_diagnostic_payload(probe, acquired)
    merged[field] = source
    return merged, metadata, field


def _merge_diagnostic_payload(
    probe: dict[str, Any],
    acquired: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(probe)
    merged.update(acquired)
    for key in ("app", "process", "workspace"):
        declared = probe.get(key)
        observed = acquired.get(key)
        if isinstance(declared, dict) and isinstance(observed, dict):
            nested = dict(declared)
            nested.update(observed)
            merged[key] = nested
    return merged


def _diagnostic_json_source(
    probe: dict[str, Any],
    context: Any,
) -> tuple[str, dict[str, Any] | None]:
    wait_json = probe.get("wait_json")
    if isinstance(wait_json, dict):
        return "wait_json", wait_json
    poll = probe.get("poll")
    if isinstance(poll, dict):
        return "poll", poll
    launch_source = _launch_diagnostic_json_source(context)
    if launch_source is not None:
        return launch_source
    return "wait_json", None


def _launch_diagnostic_json_source(context: Any) -> tuple[str, dict[str, Any]] | None:
    action_context = getattr(context, "action_context", context)
    diagnostic_launch = getattr(action_context, "diagnostic_launch", None)
    if not isinstance(diagnostic_launch, dict):
        return None
    evidence = diagnostic_launch.get("evidence")
    if not isinstance(evidence, dict):
        return None
    launch_boundary_since = _launch_diagnostic_boundary_since(diagnostic_launch)
    path = evidence.get("path")
    if not isinstance(path, str) or not path:
        directory = evidence.get("directory")
        if isinstance(directory, str) and directory:
            return "poll", _launch_diagnostic_source(
                directory,
                since=launch_boundary_since,
            )
        return None
    directory = evidence.get("directory")
    if isinstance(directory, str) and directory:
        fallback = _launch_diagnostic_directory_fallback_source(
            path,
            directory,
            context,
            launch_boundary_since=launch_boundary_since,
        )
        if fallback is not None:
            return "poll", fallback
    return "wait_json", _launch_diagnostic_source(path)


def _launch_diagnostic_source(
    path: str,
    *,
    since: tuple[int, str] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "path": path,
        "timeout_ms": LAUNCH_DIAGNOSTIC_WAIT_TIMEOUT_MS,
        "poll_interval_ms": LAUNCH_DIAGNOSTIC_WAIT_POLL_INTERVAL_MS,
    }
    if since is not None:
        source["since"] = _diagnostic_cursor_payload(since)
    return source


def _launch_diagnostic_directory_fallback_source(
    path: str,
    directory: str,
    context: Any,
    *,
    launch_boundary_since: tuple[int, str] | None = None,
) -> dict[str, Any] | None:
    try:
        resolved_path = _resolve_wait_json_path(path, context)
    except ValueError:
        return None
    if not resolved_path.is_file():
        return _launch_diagnostic_source(directory, since=launch_boundary_since)
    try:
        since = _diagnostic_candidate_sort_key(resolved_path)
        if launch_boundary_since is not None and launch_boundary_since > since:
            since = launch_boundary_since
        resolved_directory = _resolve_wait_json_path(directory, context)
        newer_candidate = _first_diagnostic_candidate(
            resolved_directory,
            None,
            since,
        )
    except (OSError, ValueError):
        return None
    if newer_candidate is None:
        return None
    return _launch_diagnostic_source(directory, since=since)


def _launch_diagnostic_boundary_since(
    diagnostic_launch: dict[str, Any],
) -> tuple[int, str] | None:
    boundary_since = diagnostic_launch.get("_launch_boundary_since")
    if not isinstance(boundary_since, dict):
        return None
    return _diagnostic_since_cursor({"since": boundary_since})


async def _read_wait_json(
    wait_json: dict[str, Any],
    context: Any,
    *,
    progress_reporter: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    raw_path = str(wait_json.get("path") or "")
    timeout_ms = _bounded_int(wait_json.get("timeout_ms"), default=0)
    poll_interval_ms = _bounded_int(wait_json.get("poll_interval_ms"), default=50)
    condition = _diagnostic_condition(wait_json)
    metadata: dict[str, Any] = {
        "path": raw_path,
        "observed": False,
        "polls": 0,
        "timeout_ms": timeout_ms,
    }
    pattern = _diagnostic_pattern(wait_json)
    if pattern is not None:
        metadata["pattern"] = pattern
    since_cursor = _diagnostic_since_cursor(wait_json)
    if since_cursor is not None:
        metadata["since"] = _diagnostic_cursor_payload(since_cursor)
    if not raw_path:
        metadata["reason"] = "diagnostic JSON path is required"
        return None, metadata

    try:
        path = _resolve_wait_json_path(raw_path, context)
    except ValueError as exc:
        metadata["reason"] = "diagnostic JSON path is outside allowed scope"
        metadata["validation_error"] = str(exc)
        return None, metadata

    clock = context.action_context.clock
    deadline = clock() + (timeout_ms / 1000)
    last_progress_fingerprint: str | None = None
    while True:
        metadata["polls"] += 1
        try:
            candidate_entry = await asyncio.to_thread(
                _first_diagnostic_candidate,
                path,
                pattern,
                since_cursor,
            )
            file_text = None
            if candidate_entry is not None:
                candidate, cursor = candidate_entry
                metadata["matched_path"] = str(candidate)
                candidate = _resolve_matched_candidate_path(candidate, context)
                metadata["matched_path"] = str(candidate)
                metadata["cursor"] = _diagnostic_cursor_payload(cursor)
                file_text = await asyncio.to_thread(_read_file_if_present, candidate)
        except ValueError as exc:
            metadata["reason"] = "matched diagnostic JSON is outside allowed scope"
            metadata["validation_error"] = str(exc)
            file_text = None
        except OSError as exc:
            metadata["reason"] = "diagnostic JSON is not readable"
            metadata["error"] = str(exc)
            file_text = None
        if file_text is not None:
            try:
                payload = json.loads(file_text)
            except json.JSONDecodeError as exc:
                metadata["reason"] = "diagnostic JSON is not readable"
                metadata["error"] = str(exc)
            else:
                if isinstance(payload, dict):
                    condition_result = _evaluate_diagnostic_condition(payload, condition)
                    if condition_result is not None:
                        metadata["candidate_observed"] = True
                        metadata["condition"] = condition_result
                        if not condition_result["matched"]:
                            metadata["reason"] = (
                                "diagnostic JSON condition not satisfied"
                            )
                            metadata.pop("error", None)
                            if condition_result.get("error"):
                                metadata["error"] = condition_result["error"]
                            if condition_result.get("terminal"):
                                last_progress_fingerprint = await _maybe_report_diagnostic_progress(
                                    progress_reporter,
                                    metadata,
                                    previous_fingerprint=last_progress_fingerprint,
                                )
                                break
                            if clock() < deadline:
                                last_progress_fingerprint = await _maybe_report_diagnostic_progress(
                                    progress_reporter,
                                    metadata,
                                    previous_fingerprint=last_progress_fingerprint,
                                )
                                await sleep_ms(
                                    clock,
                                    _poll_sleep_ms(
                                        timeout_ms=timeout_ms,
                                        poll_interval_ms=poll_interval_ms,
                                        remaining_ms=max(
                                            1,
                                            int((deadline - clock()) * 1000),
                                        ),
                                    ),
                                )
                                continue
                            break
                    metadata["observed"] = True
                    metadata.pop("reason", None)
                    metadata.pop("error", None)
                    return payload, metadata
                metadata["reason"] = "diagnostic JSON must be an object"
        last_progress_fingerprint = await _maybe_report_diagnostic_progress(
            progress_reporter,
            metadata,
            previous_fingerprint=last_progress_fingerprint,
        )

        if clock() >= deadline:
            break
        await sleep_ms(
            clock,
            _poll_sleep_ms(
                timeout_ms=timeout_ms,
                poll_interval_ms=poll_interval_ms,
                remaining_ms=max(1, int((deadline - clock()) * 1000)),
            ),
        )

    if since_cursor is not None:
        metadata.setdefault("reason", "diagnostic JSON not observed after since cursor")
    else:
        metadata.setdefault("reason", "diagnostic JSON not observed")
    return None, metadata


async def _maybe_report_diagnostic_progress(
    progress_reporter: Callable[[dict[str, Any]], Any] | None,
    metadata: dict[str, Any],
    *,
    previous_fingerprint: str | None,
) -> str | None:
    if progress_reporter is None:
        return previous_fingerprint
    if not _metadata_has_progress_signal(metadata):
        return previous_fingerprint
    try:
        progress_payload = compact_value(metadata)
        fingerprint = json.dumps(
            _diagnostic_progress_fingerprint_payload(progress_payload),
            sort_keys=True,
            ensure_ascii=False,
        )
        if fingerprint == previous_fingerprint:
            return previous_fingerprint
        result = progress_reporter(dict(progress_payload))
        if inspect.isawaitable(result):
            await result
        return fingerprint
    except Exception:
        return previous_fingerprint


def _metadata_has_progress_signal(metadata: dict[str, Any]) -> bool:
    return True


def _diagnostic_progress_fingerprint_payload(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    fingerprint_payload = dict(metadata)
    fingerprint_payload.pop("polls", None)
    return fingerprint_payload


async def _publish_app_diagnostics_progress(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
    field: str,
    metadata: dict[str, Any],
) -> None:
    action_context = getattr(context, "action_context", context)
    if not hasattr(action_context, "publish_app_diagnostics_progress"):
        return
    app = dict(probe.get("app") or {})
    entry = compact_value(
        {
            "case_id": getattr(action_context, "case_id", None),
            "transition_index": getattr(action_context, "transition_index", None),
            "phase": phase,
            "probe": probe_name(probe, "app_diagnostics"),
            "status": "RUNNING",
            "reason": str(metadata.get("reason") or f"waiting for app_diagnostics.{field}"),
            "progress": {
                "field": field,
                "metadata": compact_value(metadata),
            },
            "evidence_ref": f"diagnostic:app_diagnostics:{app.get('name') or 'app'}",
        }
    )
    await action_context.publish_app_diagnostics_progress(entry)


def _resolve_wait_json_path(raw_path: str, context: Any) -> Path:
    session = getattr(context, "session", None)
    validate_path = getattr(session, "validate_path", None)
    if callable(validate_path):
        return Path(validate_path(raw_path, must_exist=False))
    return Path(raw_path).resolve()


def _read_file_if_present(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _resolve_matched_candidate_path(path: Path, context: Any) -> Path:
    session = getattr(context, "session", None)
    validate_path = getattr(session, "validate_path", None)
    if callable(validate_path):
        return Path(validate_path(str(path), must_exist=True))
    return path


def _diagnostic_pattern(source: dict[str, Any]) -> str | None:
    pattern = source.get("pattern")
    if isinstance(pattern, str) and pattern:
        return pattern
    return None


def _diagnostic_condition(source: dict[str, Any]) -> dict[str, Any] | None:
    condition = source.get("condition")
    return condition if isinstance(condition, dict) else None


def _diagnostic_since_cursor(source: dict[str, Any]) -> tuple[int, str] | None:
    since = source.get("since")
    if not isinstance(since, dict):
        return None
    mtime_ns = since.get("mtime_ns")
    name = since.get("name")
    if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
        return None
    if not isinstance(name, str) or not name:
        return None
    return (max(0, mtime_ns), name)


def _evaluate_diagnostic_condition(
    payload: dict[str, Any],
    condition: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if condition is None:
        return None
    jsonpath = str(condition.get("jsonpath") or "")
    expected = condition.get("expected")
    result: dict[str, Any] = {
        "jsonpath": jsonpath,
        "expected": expected,
    }
    try:
        value = _diagnostic_jsonpath_value(payload, jsonpath)
    except (ImportError, ValueError) as exc:
        result.update(
            {
                "value": None,
                "matched": False,
                "error": str(exc),
                "terminal": True,
            }
        )
        return result
    result["value"] = value
    result["matched"] = _diagnostic_condition_values_equal(value, expected)
    return result


def _diagnostic_condition_values_equal(value: Any, expected: Any) -> bool:
    if isinstance(value, bool) or isinstance(expected, bool):
        return type(value) is type(expected) and value == expected
    if isinstance(value, (int, float)) and isinstance(expected, (int, float)):
        return value == expected
    if isinstance(value, list) and isinstance(expected, list):
        return len(value) == len(expected) and all(
            _diagnostic_condition_values_equal(item, expected_item)
            for item, expected_item in zip(value, expected, strict=True)
        )
    if isinstance(value, dict) and isinstance(expected, dict):
        return value.keys() == expected.keys() and all(
            _diagnostic_condition_values_equal(value[key], expected[key])
            for key in value
        )
    if type(value) is not type(expected):
        return False
    return value == expected


def _diagnostic_jsonpath_value(payload: dict[str, Any], jsonpath: str) -> Any:
    if not jsonpath:
        raise ValueError("condition jsonpath is required")
    try:
        from jsonpath_ng import parse  # type: ignore[import-untyped]
        from jsonpath_ng.exceptions import (  # type: ignore[import-untyped]
            JsonPathLexerError,
            JsonPathParserError,
        )
    except ImportError as exc:
        raise ImportError("jsonpath-ng is not installed") from exc
    try:
        matches = [match.value for match in parse(jsonpath).find(payload)]
    except (JsonPathLexerError, JsonPathParserError) as exc:
        raise ValueError(f"condition jsonpath evaluation failed: {exc}") from exc
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches


def _first_diagnostic_candidate(
    path: Path,
    pattern: str | None,
    since: tuple[int, str] | None = None,
) -> tuple[Path, tuple[int, str]] | None:
    if path.is_file():
        key = _diagnostic_candidate_sort_key(path)
        return (path, key) if _diagnostic_candidate_is_after(key, since) else None
    if not path.is_dir():
        return None
    matches: list[tuple[tuple[int, str], Path]] = []
    for candidate in path.glob(pattern or "*.json"):
        if not candidate.is_file():
            continue
        key = _diagnostic_candidate_sort_key(candidate)
        if _diagnostic_candidate_is_after(key, since):
            matches.append((key, candidate))
    if not matches:
        return None
    key, candidate = max(matches, key=lambda item: item[0])
    return candidate, key


def _diagnostic_candidate_sort_key(path: Path) -> tuple[int, str]:
    stat = path.stat()
    return (stat.st_mtime_ns, path.name)


def _diagnostic_candidate_is_after(
    key: tuple[int, str],
    since: tuple[int, str] | None,
) -> bool:
    if since is None:
        return True
    return key > since


def _diagnostic_cursor_payload(cursor: tuple[int, str]) -> dict[str, Any]:
    return {"mtime_ns": cursor[0], "name": cursor[1]}


def _bounded_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, value)


def _poll_sleep_ms(
    *,
    timeout_ms: int,
    poll_interval_ms: int,
    remaining_ms: int,
) -> int:
    if timeout_ms <= 0:
        return 0
    interval_ms = poll_interval_ms if poll_interval_ms > 0 else 1
    return min(interval_ms, remaining_ms)


def _blocked_diagnostic_json_probe(
    probe: dict[str, Any],
    acquisition: dict[str, Any],
    *,
    field: str,
) -> dict[str, Any]:
    app = _object_or_empty(probe.get("app"))
    limits = diagnostic_limits(probe)
    value = {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "app": app,
        "status": "BLOCKED",
        "observation_count": 0,
        "observations": [],
        field: acquisition,
        "limits": dict(probe.get("limits") or {}),
    }
    value["manifest"] = {
        "sources": [
            _manifest_source_entry(
                probe,
                app=app,
                status="BLOCKED",
                acquisition=acquisition,
                field=field,
                freshness=None,
            )
        ]
    }
    return {
        "name": probe_name(probe, "app_diagnostics"),
        "kind": "app_diagnostics",
        "status": "BLOCKED",
        "value": bounded_diagnostic_value(value, limits=limits),
        "evidence_ref": f"diagnostic:app_diagnostics:{app.get('name') or 'app'}",
        **build_blocked(
            reason=str(acquisition.get("reason") or "diagnostic JSON not observed"),
            requested={field: acquisition},
            accepted={
                "source": f"app_diagnostics.{field}",
                "poll": "app_diagnostics.poll",
                "wait_json": "app_diagnostics.wait_json",
            },
            next_step=(
                "Retry app_diagnostics.poll or app_diagnostics.wait_json after the app "
                "writes the diagnostic artifact."
            ),
        ),
    }


def _manifest_source_entry(
    probe: dict[str, Any],
    *,
    app: dict[str, Any],
    status: str,
    acquisition: dict[str, Any] | None,
    field: str | None,
    freshness: dict[str, Any] | None,
) -> dict[str, Any]:
    source_id = f"app_diagnostics.{field}" if field else "app_diagnostics.observations"
    entry: dict[str, Any] = {
        "id": source_id,
        "kind": "app_diagnostics",
        "status": status,
        "classification": _manifest_classification(acquisition),
        "evidence_ref": f"diagnostic:app_diagnostics:{app.get('name') or 'app'}",
        "limits": dict(probe.get("limits") or {}),
    }
    artifact_path = _manifest_artifact_path(acquisition)
    if artifact_path:
        entry["artifact_path"] = artifact_path
    reason = _manifest_reason(acquisition)
    if reason:
        entry["reason"] = reason
    redaction = _manifest_redaction(probe)
    if redaction:
        entry["redaction"] = redaction
    if freshness is not None:
        entry["freshness"] = freshness
    return entry


def _manifest_classification(acquisition: dict[str, Any] | None) -> str:
    if acquisition is None:
        return APP_DIAGNOSTICS_REPORTED
    if acquisition.get("observed") is True:
        return APP_DIAGNOSTICS_OBSERVED
    reason = str(acquisition.get("reason") or "")
    if reason == "diagnostic JSON is not readable":
        return APP_DIAGNOSTICS_UNREADABLE
    if reason == "diagnostic JSON not observed after since cursor":
        return APP_DIAGNOSTICS_STALE
    if reason == "diagnostic JSON not observed":
        return APP_DIAGNOSTICS_MISSING
    return APP_DIAGNOSTICS_BLOCKED


def _manifest_artifact_path(acquisition: dict[str, Any] | None) -> str | None:
    if not isinstance(acquisition, dict):
        return None
    path = acquisition.get("matched_path") or acquisition.get("path")
    return str(path) if path else None


def _manifest_reason(acquisition: dict[str, Any] | None) -> str | None:
    if not isinstance(acquisition, dict):
        return None
    reason = acquisition.get("reason")
    return str(reason) if reason else None


def _manifest_redaction(probe: dict[str, Any]) -> dict[str, Any] | None:
    redaction = probe.get("redaction")
    if not isinstance(redaction, dict):
        return None
    omitted = redaction.get("omit_fields")
    if not isinstance(omitted, list):
        return None
    return {"status": "PASS", "omitted_count": len(omitted)}
