from __future__ import annotations

PROBE_KINDS = (
    "app_diagnostics",
    "debug.evaluate",
    "debug.tracepoint",
    "file.json",
    "oracle_pack",
    "output.field",
    "output.since",
    "process.metric",
    "ui.grid",
    "ui.grid.viewport",
    "ui.property",
    "ui.text",
)


def accepted_probe_kinds() -> list[str]:
    return list(PROBE_KINDS)
