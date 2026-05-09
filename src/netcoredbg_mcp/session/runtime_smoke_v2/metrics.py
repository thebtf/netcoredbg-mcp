from __future__ import annotations

import importlib
import types
from dataclasses import dataclass
from typing import Any

BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class MetricSnapshot:
    timestamp: float
    working_set_mb: float | None
    private_bytes_mb: float | None
    field_status: dict[str, dict[str, Any]]


def capture_metric_snapshot(context: Any) -> MetricSnapshot:
    timestamp = context.clock()
    psutil = _load_psutil()
    if psutil is None:
        return MetricSnapshot(
            timestamp=timestamp,
            working_set_mb=None,
            private_bytes_mb=None,
            field_status={
                "working_set_delta_mb": _blocked_field("psutil unavailable"),
                "private_bytes_delta_mb": _blocked_field("psutil unavailable"),
            },
        )

    pid = _process_id(context)
    if pid is None:
        return MetricSnapshot(
            timestamp=timestamp,
            working_set_mb=None,
            private_bytes_mb=None,
            field_status={
                "working_set_delta_mb": _blocked_field("target process id unavailable"),
                "private_bytes_delta_mb": _blocked_field("target process id unavailable"),
            },
        )

    try:
        process = psutil.Process(pid)
        memory_info = process.memory_info()
    except Exception as exc:  # pragma: no cover - platform/process dependent
        reason = f"psutil memory read failed: {exc}"
        return MetricSnapshot(
            timestamp=timestamp,
            working_set_mb=None,
            private_bytes_mb=None,
            field_status={
                "working_set_delta_mb": _blocked_field(reason),
                "private_bytes_delta_mb": _blocked_field(reason),
            },
        )

    private_bytes = getattr(memory_info, "private", None)
    private_bytes_status = (
        _blocked_field("private bytes unavailable") if private_bytes is None else {"status": "PASS"}
    )
    return MetricSnapshot(
        timestamp=timestamp,
        working_set_mb=_bytes_to_mb(getattr(memory_info, "rss", None)),
        private_bytes_mb=_bytes_to_mb(private_bytes),
        field_status={
            "working_set_delta_mb": {"status": "PASS"},
            "private_bytes_delta_mb": private_bytes_status,
        },
    )


def finish_transition_metrics(
    started: MetricSnapshot,
    context: Any,
) -> dict[str, Any]:
    ended = capture_metric_snapshot(context)
    metrics: dict[str, Any] = {
        "action_latency_ms": int(round(max(0.0, ended.timestamp - started.timestamp) * 1000)),
        "working_set_delta_mb": _delta_mb(
            started.working_set_mb,
            ended.working_set_mb,
        ),
        "private_bytes_delta_mb": _delta_mb(
            started.private_bytes_mb,
            ended.private_bytes_mb,
        ),
        "partial": False,
        "field_status": {
            "action_latency_ms": {"status": "PASS"},
            "working_set_delta_mb": _combined_field_status(
                started.field_status.get("working_set_delta_mb"),
                ended.field_status.get("working_set_delta_mb"),
            ),
            "private_bytes_delta_mb": _combined_field_status(
                started.field_status.get("private_bytes_delta_mb"),
                ended.field_status.get("private_bytes_delta_mb"),
            ),
        },
    }
    metrics["partial"] = any(
        field.get("status") == "BLOCKED" for field in metrics["field_status"].values()
    )
    return metrics


def merge_case_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "action_latency_ms": 0,
            "working_set_delta_mb": 0.0,
            "private_bytes_delta_mb": None,
            "partial": False,
            "field_status": {
                "action_latency_ms": {"status": "PASS"},
                "working_set_delta_mb": {"status": "PASS"},
                "private_bytes_delta_mb": {"status": "PASS"},
            },
        }

    metrics: dict[str, Any] = {
        "action_latency_ms": sum(int(record.get("action_latency_ms", 0)) for record in records),
        "working_set_delta_mb": _sum_delta(records, "working_set_delta_mb"),
        "private_bytes_delta_mb": _sum_delta(records, "private_bytes_delta_mb"),
        "partial": any(bool(record.get("partial")) for record in records),
        "field_status": {
            "action_latency_ms": {"status": "PASS"},
            "working_set_delta_mb": _merged_status(records, "working_set_delta_mb"),
            "private_bytes_delta_mb": _merged_status(records, "private_bytes_delta_mb"),
        },
    }
    metrics["partial"] = metrics["partial"] or any(
        field.get("status") == "BLOCKED" for field in metrics["field_status"].values()
    )
    return metrics


def evaluate_metric_thresholds(
    metrics: dict[str, Any],
    thresholds: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(thresholds, dict):
        return []
    failures: list[dict[str, Any]] = []
    for metric_name, rule in thresholds.items():
        if not isinstance(rule, dict):
            continue
        value = metrics.get(metric_name)
        if not isinstance(value, (int, float)):
            continue
        max_threshold = rule.get("max")
        min_threshold = rule.get("min")
        if isinstance(max_threshold, (int, float)) and value > max_threshold:
            failures.append(
                {
                    "kind": "metric_threshold",
                    "metric": metric_name,
                    "value": value,
                    "threshold": {"max": max_threshold},
                }
            )
        if isinstance(min_threshold, (int, float)) and value < min_threshold:
            failures.append(
                {
                    "kind": "metric_threshold",
                    "metric": metric_name,
                    "value": value,
                    "threshold": {"min": min_threshold},
                }
            )
    return failures


def _load_psutil() -> Any | None:
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return None
    return psutil if isinstance(psutil, types.ModuleType) else None


def _process_id(context: Any) -> int | None:
    session = getattr(context, "session", None)
    for attr_name in ("process_id", "debuggee_pid", "pid"):
        value = getattr(session, attr_name, None)
        if isinstance(value, int):
            return value
    return None


def _bytes_to_mb(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) / BYTES_PER_MB, 3)


def _delta_mb(started: float | None, ended: float | None) -> float | None:
    if started is None or ended is None:
        return None
    return round(ended - started, 3)


def _blocked_field(reason: str) -> dict[str, Any]:
    return {"status": "BLOCKED", "reason": reason}


def _combined_field_status(
    started: dict[str, Any] | None,
    ended: dict[str, Any] | None,
) -> dict[str, Any]:
    for field in (started, ended):
        if isinstance(field, dict) and field.get("status") == "BLOCKED":
            return dict(field)
    return {"status": "PASS"}


def _sum_delta(records: list[dict[str, Any]], field_name: str) -> float | None:
    if _merged_status(records, field_name).get("status") == "BLOCKED":
        return None
    values = [record.get(field_name) for record in records]
    numeric_values = [value for value in values if isinstance(value, (int, float))]
    if not numeric_values:
        return None if field_name == "private_bytes_delta_mb" else 0.0
    return round(sum(float(value) for value in numeric_values), 3)


def _merged_status(records: list[dict[str, Any]], field_name: str) -> dict[str, Any]:
    for record in records:
        field_status = record.get("field_status")
        if not isinstance(field_status, dict):
            continue
        field = field_status.get(field_name)
        if isinstance(field, dict) and field.get("status") == "BLOCKED":
            return dict(field)
    return {"status": "PASS"}
