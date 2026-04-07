"""State snapshot manager for variable diff between stops.

Captures all local variables at the current stack frame as named snapshots.
Supports diff between two snapshots to see added/removed/changed variables.
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from .state import Snapshot, SnapshotVar

if TYPE_CHECKING:
    from .manager import SessionManager

logger = logging.getLogger(__name__)

MAX_SNAPSHOTS = int(os.environ.get("NETCOREDBG_MAX_SNAPSHOTS", "20"))
MAX_VARIABLES_PER_SNAPSHOT = int(os.environ.get("NETCOREDBG_MAX_VARS_PER_SNAPSHOT", "200"))


class SnapshotManager:
    """Manages named variable snapshots with FIFO eviction."""

    def __init__(self) -> None:
        self._snapshots: OrderedDict[str, Snapshot] = OrderedDict()

    @property
    def snapshots(self) -> dict[str, Snapshot]:
        """All snapshots (read-only copy)."""
        return dict(self._snapshots)

    async def create(self, name: str, session: SessionManager) -> Snapshot:
        """Capture all locals at the current frame as a named snapshot.

        Args:
            name: Unique snapshot name.
            session: Active SessionManager (must be in STOPPED state).

        Returns:
            The created Snapshot.

        Raises:
            ValueError: If name already exists.
            RuntimeError: If not in stopped state.
        """
        from .state import DebugState

        if name in self._snapshots:
            raise ValueError(f"Snapshot '{name}' already exists. Use a different name.")

        if session.state.state != DebugState.STOPPED:
            raise RuntimeError(
                f"Cannot create snapshot: program is {session.state.state.value}, not stopped."
            )

        # Get top frame
        tid = session.state.current_thread_id or 1
        frames = await session.get_stack_trace(thread_id=tid, levels=1)
        if not frames:
            raise RuntimeError("No stack frames available.")

        frame = frames[0]
        frame_name = frame.name

        # Get scopes → variables
        scopes = await session.get_scopes(frame_id=frame.id)
        variables: dict[str, SnapshotVar] = {}

        for scope in scopes:
            if len(variables) >= MAX_VARIABLES_PER_SNAPSHOT:
                break
            var_ref = scope.get("variablesReference", 0)
            if var_ref == 0:
                continue
            scope_vars = await session.get_variables(var_ref)
            for var in scope_vars:
                if len(variables) >= MAX_VARIABLES_PER_SNAPSHOT:
                    break
                variables[var.name] = SnapshotVar(value=var.value, type=var.type or "")

        snapshot = Snapshot(
            name=name,
            timestamp=time.monotonic(),
            frame_name=frame_name,
            variables=variables,
        )

        # FIFO eviction
        if len(self._snapshots) >= MAX_SNAPSHOTS:
            oldest_key = next(iter(self._snapshots))
            del self._snapshots[oldest_key]
            logger.info("Evicted oldest snapshot: %s", oldest_key)

        self._snapshots[name] = snapshot
        return snapshot

    def diff(self, name1: str, name2: str) -> dict[str, Any]:
        """Compare two snapshots and return the differences.

        Returns:
            Dict with added, removed, changed, unchanged_count.

        Raises:
            KeyError: If either snapshot name not found.
        """
        if name1 not in self._snapshots:
            raise KeyError(f"Snapshot '{name1}' not found.")
        if name2 not in self._snapshots:
            raise KeyError(f"Snapshot '{name2}' not found.")

        s1 = self._snapshots[name1]
        s2 = self._snapshots[name2]

        keys1 = set(s1.variables.keys())
        keys2 = set(s2.variables.keys())

        added = [
            {"name": k, "value": s2.variables[k].value, "type": s2.variables[k].type}
            for k in sorted(keys2 - keys1)
        ]

        removed = [
            {"name": k, "value": s1.variables[k].value, "type": s1.variables[k].type}
            for k in sorted(keys1 - keys2)
        ]

        changed = []
        unchanged_count = 0
        for k in sorted(keys1 & keys2):
            v1 = s1.variables[k]
            v2 = s2.variables[k]
            if v1.value != v2.value or v1.type != v2.type:
                changed.append({
                    "name": k,
                    "old_value": v1.value,
                    "new_value": v2.value,
                    "type": v2.type,
                })
            else:
                unchanged_count += 1

        return {
            "snapshot1": name1,
            "snapshot2": name2,
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged_count": unchanged_count,
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List all snapshots with metadata."""
        return [
            {
                "name": s.name,
                "timestamp": s.timestamp,
                "frame": s.frame_name,
                "variable_count": len(s.variables),
            }
            for s in self._snapshots.values()
        ]
