from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..runtime_smoke_correlation import correlation_source
from .actions import ActionContext
from .blocked import build_blocked
from .probes import accepted_probe_kinds
from .probes.app_diagnostics import handle_app_diagnostics
from .probes.debug_evaluate import handle_debug_evaluate
from .probes.debug_tracepoint import handle_debug_tracepoint
from .probes.file_json import handle_file_json
from .probes.oracle_pack import handle_oracle_pack
from .probes.output_field import handle_output_field
from .probes.output_since import handle_output_since
from .probes.process_metric import handle_process_metric
from .probes.ui_grid import handle_ui_grid
from .probes.ui_grid_viewport import handle_ui_grid_viewport
from .probes.ui_property import handle_ui_property
from .probes.ui_text import handle_ui_text

ACCEPTED_PROBE_PHASES = frozenset({"before", "after", "both"})


@dataclass(frozen=True)
class ProbeContext:
    action_context: ActionContext
    scratch: dict[str, Any] = field(default_factory=dict)

    async def call_adapter(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return await self.action_context.call_adapter(name, **kwargs)

    @property
    def session(self) -> Any:
        return self.action_context.session


def probe_path(probe: dict[str, Any]) -> str:
    kind = str(probe.get("kind") or "")
    name = probe.get("name")
    if name is None:
        return kind
    name = str(name)
    if not name:
        return kind
    return f"{kind}.{name}"


def accepted_probe_phases() -> list[str]:
    return sorted(ACCEPTED_PROBE_PHASES)


def probe_runs_in_phase(probe: dict[str, Any], phase: str) -> bool:
    phases = _probe_phases(probe)
    return "both" in phases or phase in phases


def _probe_phases(probe: dict[str, Any]) -> set[str]:
    raw_phases = probe.get("phases")
    if raw_phases is not None:
        if isinstance(raw_phases, str):
            return {raw_phases}
        if isinstance(raw_phases, (list, tuple, set)):
            return {str(item) for item in raw_phases}
        return {str(raw_phases)}

    raw_phase = probe.get("phase")
    if raw_phase is None:
        return {"both"}
    return {str(raw_phase)}


async def dispatch_probe(
    probe: dict[str, Any],
    context: ProbeContext,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = str(probe.get("kind") or "")
    if kind == "debug.evaluate":
        result = await handle_debug_evaluate(probe, context, phase=phase)
    elif kind == "debug.tracepoint":
        result = await handle_debug_tracepoint(probe, context, phase=phase)
    elif kind == "app_diagnostics":
        result = await handle_app_diagnostics(probe, context, phase=phase)
    elif kind == "file.json":
        result = await handle_file_json(probe, context, phase=phase)
    elif kind == "oracle_pack":
        result = await handle_oracle_pack(probe, context, phase=phase)
    elif kind == "output.field":
        result = await handle_output_field(probe, context, phase=phase)
    elif kind == "output.since":
        result = await handle_output_since(probe, context, phase=phase)
    elif kind == "process.metric":
        result = await handle_process_metric(probe, context, phase=phase)
    elif kind == "ui.grid":
        result = await handle_ui_grid(probe, context, phase=phase)
    elif kind == "ui.grid.viewport":
        result = await handle_ui_grid_viewport(probe, context, phase=phase)
    elif kind == "ui.property":
        result = await handle_ui_property(probe, context, phase=phase)
    elif kind == "ui.text":
        result = await handle_ui_text(probe, context, phase=phase)
    else:
        blocked = build_blocked(
            reason="probe execution not implemented",
            requested={"kind": kind},
            accepted={"probe_kinds": accepted_probe_kinds()},
            next_step="Use a probe kind implemented by this runtime-smoke phase.",
        )
        result = {
            "name": str(probe.get("name") or kind),
            "kind": kind,
            "status": "BLOCKED",
            "value": None,
            **blocked,
        }
    result["correlation_source"] = correlation_source(
        probe,
        fallback=probe_path(probe),
    )
    return result
