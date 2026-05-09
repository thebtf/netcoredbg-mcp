from __future__ import annotations

import importlib
import os
from typing import Any

from ._common import blocked_probe, probe_name

_MB = 1024 * 1024


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
            requested={"pid": probe.get("pid") or probe.get("process_id")},
            accepted={"dependency": "psutil>=7.2.2,<8.0.0"},
            next_step="install psutil>=7.2.2 before running process.metric probes",
        )

    pid = _resolve_pid(probe, context.session)
    sample = _memory_sample(psutil.Process(pid))
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
        if isinstance(baseline, dict):
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
            value = {
                "action_latency_ms": 0,
                "working_set_delta_mb": 0.0,
                "private_bytes_delta_mb": None,
            }
    return {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "PASS",
        "value": value,
        "pid": pid,
    }


def _resolve_pid(probe: dict[str, Any], session: Any) -> int:
    raw_pid = probe.get("pid") or probe.get("process_id")
    if raw_pid is None:
        raw_pid = getattr(session, "process_id", None)
    if raw_pid is None:
        state = getattr(session, "state", None)
        raw_pid = getattr(state, "process_id", None)
    return int(raw_pid or os.getpid())


def _memory_sample(process: Any) -> dict[str, float | None]:
    info = process.memory_info()
    private_bytes = getattr(info, "private", None) if os.name == "nt" else None
    return {
        "rss_mb": round(float(getattr(info, "rss", 0)) / _MB, 3),
        "private_mb": (
            None if private_bytes is None else round(float(private_bytes) / _MB, 3)
        ),
    }
