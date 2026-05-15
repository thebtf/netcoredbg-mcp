from __future__ import annotations

PROBE_KINDS = (
    "debug.evaluate",
    "debug.tracepoint",
    "file.json",
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
