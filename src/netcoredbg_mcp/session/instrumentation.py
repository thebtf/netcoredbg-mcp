"""Runtime smoke instrumentation group service."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .runtime_smoke import RuntimeSmokeSession, compact_group_evidence
from .state import TerminalStatus, TraceEntry
from .tracepoints import TracepointManager

MAX_TRACE_LOG_LINES = 20


class _BreakpointRegistry(Protocol):
    def get_for_file(self, file: str) -> Sequence[Any]: ...
    def _normalize_path(self, path: str) -> str: ...


class _InstrumentationSession(Protocol):
    @property
    def breakpoints(self) -> _BreakpointRegistry: ...

    @property
    def runtime_smoke(self) -> RuntimeSmokeSession: ...

    @property
    def state(self) -> Any: ...

    async def add_breakpoint(
        self,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
        *,
        tracepoint_id: str | None = None,
        group_id: str | None = None,
    ) -> Any: ...

    async def remove_breakpoint(
        self, file: str, line: int, *, tracepoint_id: str | None = None,
        group_id: str | None = None,
    ) -> bool: ...


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
        self._mutation_lock = asyncio.Lock()

    async def create_group(
        self,
        name: str,
        *,
        breakpoints: list[dict[str, Any]] | None = None,
        tracepoints: list[dict[str, Any]] | None = None,
    ) -> InstrumentationResult:
        async with self._mutation_lock:
            return await self._create_group_locked(
                name, breakpoints=breakpoints, tracepoints=tracepoints
            )

    async def _create_group_locked(
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

        breakpoint_refs: list[dict[str, Any]] = []
        tracepoint_refs: list[dict[str, Any]] = []
        owner_ids: list[str] = []
        record = {
            "name": name,
            "breakpoints": breakpoint_refs,
            "breakpoint_owner_ids": owner_ids,
            "tracepoints": tracepoint_refs,
            "creation_incomplete": True,
        }
        self._groups[name] = record
        try:
            for spec in breakpoint_specs:
                owner_id = uuid.uuid4().hex
                owner_ids.append(owner_id)
                ref = {
                    "kind": "breakpoint", "file": os.path.normpath(str(spec["file"])),
                    "line": int(spec["line"]), "condition": spec.get("condition"),
                    "hit_condition": spec.get("hit_condition"), "verified": False,
                    "id": None, "dap_line": None,
                }
                breakpoint_refs.append(ref)
                bp = await self._session.add_breakpoint(
                    str(spec["file"]), int(spec["line"]), spec.get("condition"),
                    spec.get("hit_condition"), group_id=owner_id,
                )
                ref.update(verified=bool(getattr(bp, "verified", False)),
                           id=getattr(bp, "id", None), dap_line=getattr(bp, "dap_line", None))
            for spec in tracepoint_specs:
                await self._add_tracepoint_ref(spec, tracepoint_refs)
        except BaseException:
            try:
                await self._clear_group_locked(name)
            except BaseException:
                pass
            raise
        record["creation_incomplete"] = False
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
        if record.get("creation_incomplete"):
            return self._fail("instrumentation group creation incomplete", group=name)
        if record.get("cleanup_incomplete"):
            return self._fail("instrumentation group cleanup incomplete", group=name)
        unknown = getattr(self._session, "_breakpoint_sync_unknown", set())
        pending = getattr(self._session, "_breakpoint_sync_pending", {})
        if any(self._session.breakpoints._normalize_path(str(ref["file"])) in unknown
               or self._session.breakpoints._normalize_path(str(ref["file"])) in pending
               for ref in record["breakpoints"] + record["tracepoints"]):
            return self._fail("instrumentation group breakpoint sync unconfirmed", group=name)

        owner_ids = record["breakpoint_owner_ids"]
        if len(owner_ids) != len(record["breakpoints"]):
            return self._fail("instrumentation group owner missing", group=name)
        breakpoint_refs = []
        for ref, owner_id in zip(record["breakpoints"], owner_ids):
            bp = next((bp for bp in self._session.breakpoints.get_for_file(str(ref["file"]))
                       if bp.line == int(ref["line"]) and owner_id in bp.group_owners), None)
            if bp is None:
                return self._fail("instrumentation group owner missing", group=name)
            breakpoint_refs.append({
                **ref, "verified": bp.verified, "id": bp.id, "dap_line": bp.dap_line,
                "hit_count": self._breakpoint_hit_count(ref),
            })
        manager = self._tracepoint_manager(create=False)
        tracepoints = manager.tracepoints if manager is not None else {}
        tracepoint_refs = []
        for ref in record["tracepoints"]:
            tracepoint_id = str(ref["id"])
            tracepoint = tracepoints.get(tracepoint_id)
            bp = next((bp for bp in self._session.breakpoints.get_for_file(str(ref["file"]))
                       if bp.line == int(ref["line"])
                       and tracepoint_id in bp.tracepoint_owners), None)
            if (tracepoint is None or bp is None or tracepoint.line != int(ref["line"])
                    or self._session.breakpoints._normalize_path(tracepoint.file)
                    != self._session.breakpoints._normalize_path(str(ref["file"]))):
                return self._fail("instrumentation group tracepoint owner missing", group=name)
            tracepoint_refs.append(self._tracepoint_evidence({
                **ref, "breakpoint_id": bp.id, "dap_line": bp.dap_line,
            }))
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
        async with self._mutation_lock:
            return await self._clear_group_locked(name)

    async def _clear_group_locked(self, name: str) -> InstrumentationResult:
        record = self._groups.get(name)
        if record is None:
            return self._fail("instrumentation group not found", group=name)
        record["cleanup_incomplete"] = True

        for ref, owner_id in zip(record["breakpoints"], record["breakpoint_owner_ids"]):
            if any(owner_id in bp.group_owners and bp.line == int(ref["line"])
                   for bp in self._session.breakpoints.get_for_file(str(ref["file"]))):
                await self._session.remove_breakpoint(
                    ref["file"], int(ref["line"]), group_id=owner_id
                )
        manager = self._tracepoint_manager(create=False)
        for ref in record["tracepoints"]:
            tracepoint_id = str(ref["id"])
            owned = any(
                tracepoint_id in bp.tracepoint_owners and bp.line == int(ref["line"])
                for bp in self._session.breakpoints.get_for_file(str(ref["file"]))
            )
            if not owned and (manager is None or tracepoint_id not in manager.tracepoints):
                continue
            removed = await self._session.remove_breakpoint(
                ref["file"], int(ref["line"]), tracepoint_id=tracepoint_id
            )
            if manager is not None and (removed or not any(
                tracepoint_id in bp.tracepoint_owners
                for bp in self._session.breakpoints.get_for_file(str(ref["file"]))
            )):
                manager.remove(tracepoint_id)

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

    async def _add_tracepoint_ref(
        self, spec: dict[str, Any], refs: list[dict[str, Any]]
    ) -> None:
        file = str(spec["file"])
        line = int(spec["line"])
        expression = str(spec["expression"])
        manager = self._tracepoint_manager(create=True)
        assert manager is not None
        tracepoint = manager.add(file, line, expression)
        ref = {
            "kind": "tracepoint", "id": tracepoint.id, "file": os.path.normpath(file),
            "line": line, "expression": expression, "breakpoint_id": None, "dap_line": None,
        }
        refs.append(ref)
        try:
            bp = await self._session.add_breakpoint(file, line, tracepoint_id=tracepoint.id)
        except Exception as exc:
            retained = any(
                tracepoint.id in bp.tracepoint_owners
                for bp in self._session.breakpoints.get_for_file(file)
            )
            if not retained:
                manager.remove(tracepoint.id)
            raise RuntimeError(f"Tracepoint {tracepoint.id} setup failed: {exc}") from exc
        tracepoint.breakpoint_id = getattr(bp, "id", None)
        tracepoint.dap_line = getattr(bp, "dap_line", None)
        ref.update(breakpoint_id=tracepoint.breakpoint_id, dap_line=tracepoint.dap_line)


    def _tracepoint_manager(self, *, create: bool) -> TracepointManager | None:
        manager = getattr(self._session, "_tracepoint_manager", None)
        if manager is None and create:
            manager = TracepointManager()
            setattr(self._session, "_tracepoint_manager", manager)
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
        for ref, owner_id in zip(record["breakpoints"], record["breakpoint_owner_ids"]):
            for bp in self._session.breakpoints.get_for_file(str(ref["file"])):
                if bp.line == int(ref["line"]) and owner_id in bp.group_owners:
                    leaks.append(
                        {
                            "kind": "breakpoint",
                            "file": os.path.normpath(str(ref["file"])),
                            "line": int(ref["line"]),
                        }
                    )
                    break
        tracepoints = manager.tracepoints if manager is not None else {}
        for ref in record["tracepoints"]:
            tracepoint_id = str(ref["id"])
            if tracepoint_id in tracepoints:
                leaks.append({"kind": "tracepoint", "id": tracepoint_id})
            for bp in self._session.breakpoints.get_for_file(str(ref["file"])):
                if bp.line == int(ref["line"]) and tracepoint_id in bp.tracepoint_owners:
                    leaks.append({
                        "kind": "breakpoint",
                        "file": os.path.normpath(str(ref["file"])),
                        "line": int(ref["line"]),
                    })
        return leaks

    def _pass(self, reason: str, **payload: Any) -> InstrumentationResult:
        group = str(payload.get("group", ""))
        evidence_refs = (
            [
                {
                    "kind": "instrumentation_group",
                    "ref": f"group:{group}",
                    "summary": reason,
                }
            ]
            if group
            else []
        )
        return InstrumentationResult(
            {
                "status": TerminalStatus.PASS.value,
                "reason": reason,
                **payload,
                "evidence_refs": evidence_refs,
            }
        )

    def _fail(self, reason: str, **payload: Any) -> InstrumentationResult:
        return InstrumentationResult(
            {
                "status": TerminalStatus.FAIL.value,
                "reason": reason,
                **payload,
            }
        )
