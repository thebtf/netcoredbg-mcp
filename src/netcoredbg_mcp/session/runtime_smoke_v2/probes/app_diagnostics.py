from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ...runtime_smoke_schema import DIAGNOSTIC_SCHEMA_VERSION
from ..blocked import build_blocked
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

    probe, acquisition, acquisition_field = await _probe_with_diagnostic_json(probe, context)
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
    elif status == "FAIL":
        output["reason"] = "app diagnostics reported FAIL"
    return output


async def _probe_with_diagnostic_json(
    probe: dict[str, Any],
    context: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    field, source = _diagnostic_json_source(probe, context)
    if source is None:
        return probe, None, None

    acquired, metadata = await _read_wait_json(source, context)
    if acquired is None:
        return probe, metadata, field
    merged = dict(probe)
    merged.update(acquired)
    merged[field] = source
    return merged, metadata, field


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
        return "wait_json", launch_source
    return "wait_json", None


def _launch_diagnostic_json_source(context: Any) -> dict[str, Any] | None:
    action_context = getattr(context, "action_context", context)
    diagnostic_launch = getattr(action_context, "diagnostic_launch", None)
    if not isinstance(diagnostic_launch, dict):
        return None
    evidence = diagnostic_launch.get("evidence")
    if not isinstance(evidence, dict):
        return None
    path = evidence.get("path")
    if not isinstance(path, str) or not path:
        return None
    return {
        "path": path,
        "timeout_ms": LAUNCH_DIAGNOSTIC_WAIT_TIMEOUT_MS,
        "poll_interval_ms": LAUNCH_DIAGNOSTIC_WAIT_POLL_INTERVAL_MS,
    }


async def _read_wait_json(
    wait_json: dict[str, Any],
    context: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    raw_path = str(wait_json.get("path") or "")
    timeout_ms = _bounded_int(wait_json.get("timeout_ms"), default=0)
    poll_interval_ms = _bounded_int(wait_json.get("poll_interval_ms"), default=50)
    metadata: dict[str, Any] = {
        "path": raw_path,
        "observed": False,
        "polls": 0,
        "timeout_ms": timeout_ms,
    }
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
    while True:
        metadata["polls"] += 1
        try:
            file_text = await asyncio.to_thread(_read_file_if_present, path)
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
                    metadata["observed"] = True
                    metadata.pop("reason", None)
                    metadata.pop("error", None)
                    return payload, metadata
                metadata["reason"] = "diagnostic JSON must be an object"

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

    metadata.setdefault("reason", "diagnostic JSON not observed")
    return None, metadata


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
    app = dict(probe.get("app") or {})
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
