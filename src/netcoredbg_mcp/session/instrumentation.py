"""Runtime smoke instrumentation group service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from .runtime_smoke import RuntimeSmokeSession, compact_group_evidence
from .state import TerminalStatus, TraceEntry
from .tracepoints import TracepointManager

MAX_TRACE_LOG_LINES = 20


class _BreakpointRegistry(Protocol):
    def get_for_file(self, file: str) -> list[Any]: ...
    def _normalize_path(self, path: str) -> str: ...


class _InstrumentationSession(Protocol):
    breakpoints: _BreakpointRegistry
    runtime_smoke: RuntimeSmokeSession
    state: Any

    async def add_breakpoint(
        self,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> Any: ...

    async def remove_breakpoint(self, file: str, line: int) -> bool: ...


@dataclass(frozen=True)
class InstrumentationResult:
    """Serializable instrumentation operation result."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class InstrumentationGroupService:
    """Manage named groups of breakpoints and tracepoints for smoke evidence."""

    def __init__(self, session: _InstrumentationSession) -> None:
        self._session = session

    async def create_group(
        self,
        name: str,
        *,
        breakpoints: list[dict[str, Any]] | None = None,
        tracepoints: list[dict[str, Any]] | None = None,
    ) -> InstrumentationResult:
        if name in self._groups:
            return self._fail("instrumentation group already exists", group=name)

        breakpoint_specs = [dict(item) for item in (breakpoints or [])]
        tracepoint_specs = [dict(item) for item in (tracepoints or [])]
        if not breakpoint_specs and not tracepoint_specs:
            return self._fail("instrumentation group requires at least one item", group=name)

        breakpoint_refs = []
        tracepoint_refs = []
        for spec in breakpoint_specs:
            breakpoint_refs.append(await self._add_breakpoint_ref(spec))
        for spec in tracepoint_specs:
            tracepoint_refs.append(await self._add_tracepoint_ref(spec))

        record = {
            "name": name,
            "breakpoints": breakpoint_refs,
            "tracepoints": tracepoint_refs,
        }
        self._groups[name] = record
        return self._pass(
            "instrumentation group created",
            group=name,
            breakpoints=breakpoint_refs,
            tracepoints=tracepoint_refs,
            summary=compact_group_evidence(
                group=name,
                breakpoint_count=len(breakpoint_refs),
                tracepoint_count=len(tracepoint_refs),
            ),
        )

    async def inspect_group(self, name: str) -> InstrumentationResult:
        record = self._groups.get(name)
        if record is None:
            return self._fail("instrumentation group not found", group=name)

        breakpoint_refs = [
            {**ref, "hit_count": self._breakpoint_hit_count(ref)}
            for ref in record["breakpoints"]
        ]
        tracepoint_refs = [self._tracepoint_evidence(ref) for ref in record["tracepoints"]]
        hit_count = sum(int(ref["hit_count"]) for ref in breakpoint_refs)
        trace_log_count = sum(int(ref["log_count"]) for ref in tracepoint_refs)
        return self._pass(
            "instrumentation group inspected",
            group=name,
            breakpoints=breakpoint_refs,
            tracepoints=tracepoint_refs,
            summary=compact_group_evidence(
                group=name,
                breakpoint_count=len(breakpoint_refs),
                tracepoint_count=len(tracepoint_refs),
                hit_count=hit_count,
                trace_log_count=trace_log_count,
            ),
        )

    async def clear_group(self, name: str) -> InstrumentationResult:
        record = self._groups.get(name)
        if record is None:
            return self._fail("instrumentation group not found", group=name)

        for ref in record["breakpoints"]:
            await self._session.remove_breakpoint(ref["file"], int(ref["line"]))
        manager = self._tracepoint_manager(create=False)
        for ref in record["tracepoints"]:
            if manager is not None:
                manager.remove(str(ref["id"]))
            await self._session.remove_breakpoint(ref["file"], int(ref["line"]))

        leaks = self._leaks(record)
        if leaks:
            return self._fail(
                "instrumentation group cleanup leaked state",
                group=name,
                leaks=leaks,
            )

        del self._groups[name]
        return self._pass(
            "instrumentation group cleared",
            group=name,
            removed={
                "breakpoints": len(record["breakpoints"]),
                "tracepoints": len(record["tracepoints"]),
            },
            summary=compact_group_evidence(
                group=name,
                breakpoint_count=0,
                tracepoint_count=0,
            ),
        )

    @property
    def _groups(self) -> dict[str, Any]:
        return self._session.runtime_smoke.instrumentation_groups

    async def _add_breakpoint_ref(self, spec: dict[str, Any]) -> dict[str, Any]:
        file = str(spec["file"])
        line = int(spec["line"])
        bp = await self._session.add_breakpoint(
            file,
            line,
            spec.get("condition"),
            spec.get("hit_condition"),
        )
        return {
            "kind": "breakpoint",
            "file": os.path.normpath(file),
            "line": line,
            "condition": spec.get("condition"),
            "hit_condition": spec.get("hit_condition"),
            "verified": bool(getattr(bp, "verified", False)),
            "id": getattr(bp, "id", None),
            "dap_line": getattr(bp, "dap_line", None),
        }

    async def _add_tracepoint_ref(self, spec: dict[str, Any]) -> dict[str, Any]:
        file = str(spec["file"])
        line = int(spec["line"])
        expression = str(spec["expression"])
        manager = self._tracepoint_manager(create=True)
        tracepoint = manager.add(file, line, expression)
        bp = await self._session.add_breakpoint(file, line)
        tracepoint.breakpoint_id = getattr(bp, "id", None)
        tracepoint.dap_line = getattr(bp, "dap_line", None)
        return {
            "kind": "tracepoint",
            "id": tracepoint.id,
            "file": os.path.normpath(file),
            "line": line,
            "expression": expression,
            "breakpoint_id": tracepoint.breakpoint_id,
            "dap_line": tracepoint.dap_line,
        }

    def _tracepoint_manager(self, *, create: bool) -> TracepointManager | None:
        manager = getattr(self._session, "_tracepoint_manager", None)
        if manager is None and create:
            manager = TracepointManager()
            self._session._tracepoint_manager = manager
        return manager

    def _breakpoint_hit_count(self, ref: dict[str, Any]) -> int:
        hit_counts = getattr(self._session.state, "hit_counts", {})
        normalize = getattr(self._session.breakpoints, "_normalize_path", os.path.normpath)
        return int(hit_counts.get((normalize(str(ref["file"])), int(ref["line"])), 0))

    def _tracepoint_evidence(self, ref: dict[str, Any]) -> dict[str, Any]:
        manager = self._tracepoint_manager(create=False)
        tracepoint_id = str(ref["id"])
        entries: list[TraceEntry] = []
        tracepoint_hit_count = 0
        if manager is not None:
            tracepoint = manager.tracepoints.get(tracepoint_id)
            tracepoint_hit_count = int(getattr(tracepoint, "hit_count", 0) or 0)
            entries = manager.get_log(tracepoint_id=tracepoint_id)

        return {
            **ref,
            "hit_count": tracepoint_hit_count,
            "log_count": len(entries),
            "logs": [
                {
                    "line": entry.line,
                    "expression": entry.expression,
                    "value": entry.value,
                    "tracepoint_id": entry.tracepoint_id,
                }
                for entry in entries[:MAX_TRACE_LOG_LINES]
            ],
        }

    def _leaks(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        leaks = []
        manager = self._tracepoint_manager(create=False)
        for ref in [*record["breakpoints"], *record["tracepoints"]]:
            for bp in self._session.breakpoints.get_for_file(str(ref["file"])):
                if int(getattr(bp, "line", -1)) == int(ref["line"]):
                    leaks.append({
                        "kind": "breakpoint",
                        "file": os.path.normpath(str(ref["file"])),
                        "line": int(ref["line"]),
                    })
                    break
        if manager is not None:
            tracepoints = manager.tracepoints
            for ref in record["tracepoints"]:
                if ref["id"] in tracepoints:
                    leaks.append({"kind": "tracepoint", "id": ref["id"]})
        return leaks

    def _pass(self, reason: str, **payload: Any) -> InstrumentationResult:
        group = str(payload.get("group", ""))
        evidence_refs = [
            {
                "kind": "instrumentation_group",
                "ref": f"group:{group}",
                "summary": reason,
            }
        ] if group else []
        return InstrumentationResult({
            "status": TerminalStatus.PASS.value,
            "reason": reason,
            **payload,
            "evidence_refs": evidence_refs,
        })

    def _fail(self, reason: str, **payload: Any) -> InstrumentationResult:
        return InstrumentationResult({
            "status": TerminalStatus.FAIL.value,
            "reason": reason,
            **payload,
        })
