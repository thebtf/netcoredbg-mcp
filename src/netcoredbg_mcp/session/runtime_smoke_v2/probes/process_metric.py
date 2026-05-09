from __future__ import annotations

import importlib
import logging
import os
from typing import Any

from ._common import blocked_probe, probe_name

_MB = 1024 * 1024
_LOG = logging.getLogger(__name__)


async def handle_process_metric(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "process.metric"
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return blocked_probe(
            probe,
            kind=kind,
            reason="psutil is not installed",
            requested={"pid": _requested_pid(probe)},
            accepted={"dependency": "psutil>=7.2.2,<8.0.0"},
            next_step="install psutil>=7.2.2 before running process.metric probes",
        )

    try:
        pid = _resolve_pid(probe, context.session)
    except (TypeError, ValueError) as exc:
        return blocked_probe(
            probe,
            kind=kind,
            reason=f"invalid pid: {exc}",
            requested={"pid": _requested_pid(probe)},
            accepted={"pid": "positive integer"},
            next_step="Provide a valid pid/process_id or ensure the session exposes one.",
        )
    if pid is None:
        return blocked_probe(
            probe,
            kind=kind,
            reason="target process id unavailable",
            requested={"pid": _requested_pid(probe)},
            accepted={"pid": "positive integer"},
            next_step="Provide pid/process_id or launch a session that exposes process_id.",
        )
    try:
        sample = _memory_sample(psutil.Process(pid))
    except (psutil.Error, OSError) as exc:  # pragma: no cover - psutil/platform dependent
        _LOG.warning("process.metric memory read failed for pid %s: %s", pid, exc)
        return blocked_probe(
            probe,
            kind=kind,
            reason="target process is not accessible",
            requested={"pid": pid},
            accepted={"pid": "running and accessible process id"},
            next_step="Ensure the target process is still running and readable.",
        ) | {"error": str(exc)}
    cache_key = f"{kind}:{probe_name(probe, kind)}"
    if phase == "before":
        context.scratch[cache_key] = {
            "sample": sample,
            "clock": context.action_context.clock(),
        }
        value = {
            "working_set_mb": sample["rss_mb"],
            "private_bytes_mb": sample["private_mb"],
        }
    else:
        baseline = context.scratch.get(cache_key)
        if (
            isinstance(baseline, dict)
            and "sample" in baseline
            and "clock" in baseline
        ):
            before = baseline["sample"]
            started = float(baseline["clock"])
            private_delta = (
                None
                if sample["private_mb"] is None or before["private_mb"] is None
                else round(sample["private_mb"] - before["private_mb"], 3)
            )
            value = {
                "action_latency_ms": int(
                    max(0.0, context.action_context.clock() - started) * 1000
                ),
                "working_set_delta_mb": round(sample["rss_mb"] - before["rss_mb"], 3),
                "private_bytes_delta_mb": private_delta,
            }
        else:
            return blocked_probe(
                probe,
                kind=kind,
                reason="process.metric baseline is missing",
                requested={"phase": phase, "pid": pid},
                accepted={"baseline": "matching before-phase sample"},
                next_step="Run the probe in the before phase before requesting after deltas.",
            )
    return {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "PASS",
        "value": value,
        "pid": pid,
    }


def _resolve_pid(probe: dict[str, Any], session: Any) -> int | None:
    raw_pid = _requested_pid(probe)
    if raw_pid is None:
        raw_pid = getattr(session, "process_id", None)
    if raw_pid is None:
        state = getattr(session, "state", None)
        raw_pid = getattr(state, "process_id", None)
    if raw_pid is None:
        return None
    if isinstance(raw_pid, bool):
        raise ValueError("pid must be positive")
    pid = int(raw_pid)
    if pid <= 0:
        raise ValueError("pid must be positive")
    return pid


def _requested_pid(probe: dict[str, Any]) -> Any:
    if "pid" in probe and probe.get("pid") is not None:
        return probe.get("pid")
    return probe.get("process_id")


def _memory_sample(process: Any) -> dict[str, float | None]:
    info = process.memory_info()
    private_bytes = getattr(info, "private", None) if os.name == "nt" else None
    return {
        "rss_mb": round(float(getattr(info, "rss", 0)) / _MB, 3),
        "private_mb": (
            None if private_bytes is None else round(float(private_bytes) / _MB, 3)
        ),
    }
