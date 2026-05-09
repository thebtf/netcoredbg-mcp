from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .actions import ActionContext
from .blocked import build_blocked
from .probes import accepted_probe_kinds
from .probes.debug_evaluate import handle_debug_evaluate
from .probes.debug_tracepoint import handle_debug_tracepoint
from .probes.file_json import handle_file_json
from .probes.output_field import handle_output_field
from .probes.output_since import handle_output_since
from .probes.process_metric import handle_process_metric
from .probes.ui_grid import handle_ui_grid
from .probes.ui_property import handle_ui_property
from .probes.ui_text import handle_ui_text


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
    name = str(probe.get("name") or kind)
    return f"{kind}.{name}"


async def dispatch_probe(
    probe: dict[str, Any],
    context: ProbeContext,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = str(probe.get("kind") or "")
    if kind == "debug.evaluate":
        return await handle_debug_evaluate(probe, context, phase=phase)
    if kind == "debug.tracepoint":
        return await handle_debug_tracepoint(probe, context, phase=phase)
    if kind == "file.json":
        return await handle_file_json(probe, context, phase=phase)
    if kind == "output.field":
        return await handle_output_field(probe, context, phase=phase)
    if kind == "output.since":
        return await handle_output_since(probe, context, phase=phase)
    if kind == "process.metric":
        return await handle_process_metric(probe, context, phase=phase)
    if kind == "ui.grid":
        return await handle_ui_grid(probe, context, phase=phase)
    if kind == "ui.property":
        return await handle_ui_property(probe, context, phase=phase)
    if kind == "ui.text":
        return await handle_ui_text(probe, context, phase=phase)
    blocked = build_blocked(
        reason="probe execution not implemented",
        requested={"kind": kind},
        accepted={"probe_kinds": accepted_probe_kinds()},
        next_step="Use a probe kind implemented by this runtime-smoke phase.",
    )
    return {
        "name": str(probe.get("name") or kind),
        "kind": kind,
        "status": "BLOCKED",
        "value": None,
        **blocked,
    }
