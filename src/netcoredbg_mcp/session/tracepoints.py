"""Client-side tracepoint manager.

Tracepoints are non-stopping breakpoints that log expression values.
Uses the quick_evaluate pattern (pause → evaluate → resume) with a
shorter timeout (500ms) and rate limiting (max 10 hits/sec).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from .state import TraceEntry, Tracepoint

if TYPE_CHECKING:
    from .manager import SessionManager

logger = logging.getLogger(__name__)

MAX_TRACE_ENTRIES = int(os.environ.get("NETCOREDBG_MAX_TRACE_ENTRIES", "1000"))
EVALUATE_TIMEOUT_SECONDS = float(os.environ.get("NETCOREDBG_EVALUATE_TIMEOUT", "0.5"))
RATE_LIMIT_INTERVAL_SECONDS = float(
    os.environ.get("NETCOREDBG_RATE_LIMIT_INTERVAL", "0.1")
)  # Max 10 hits/sec


class _TraceBuffer(deque[TraceEntry]):
    def __init__(
        self,
        owner: TracepointManager,
        entries: list[TraceEntry] | deque[TraceEntry] | None = None,
        *,
        maxlen: int | None,
    ) -> None:
        super().__init__(entries or (), maxlen=maxlen)
        self._owner = owner

    def append(self, entry: TraceEntry) -> None:  # type: ignore[override]
        if self.maxlen is not None and len(self) >= self.maxlen:
            self._owner._trace_drop_generation += 1
        self._owner._trace_append_generation += 1
        super().append(entry)

    def clear(self) -> None:  # type: ignore[override]
        if self:
            self._owner._trace_drop_generation += len(self)
        super().clear()


class TracepointManager:
    """Manages client-side tracepoints with rate limiting and trace buffer."""

    def __init__(self) -> None:
        self._tracepoints: dict[str, Tracepoint] = {}
        self._trace_append_generation = 0
        self._trace_drop_generation = 0
        self._trace_buffer_storage: deque[TraceEntry] = _TraceBuffer(
            self,
            maxlen=MAX_TRACE_ENTRIES,
        )
        self._counter = 0
        self._last_hit_times: dict[str, float] = {}  # tp_id → last hit monotonic time
        self._lock = asyncio.Lock()

    @property
    def _trace_buffer(self) -> deque[TraceEntry]:
        return self._trace_buffer_storage

    @_trace_buffer.setter
    def _trace_buffer(self, value: deque[TraceEntry]) -> None:
        self._trace_buffer_storage = _TraceBuffer(
            self,
            list(value),
            maxlen=value.maxlen,
        )

    @property
    def tracepoints(self) -> dict[str, Tracepoint]:
        """All registered tracepoints."""
        return dict(self._tracepoints)

    @property
    def is_log_full(self) -> bool:
        """True when the trace buffer has reached its maximum capacity."""
        return len(self._trace_buffer) >= self._trace_buffer.maxlen  # type: ignore[operator]

    def add(self, file: str, line: int, expression: str) -> Tracepoint:
        """Register a new tracepoint. Returns the Tracepoint (DAP breakpoint set separately)."""
        self._counter += 1
        tp_id = f"tp-{self._counter}"
        tp = Tracepoint(
            id=tp_id,
            file=file,
            line=line,
            expression=expression,
        )
        self._tracepoints[tp_id] = tp
        return tp

    def remove(self, tp_id: str) -> Tracepoint | None:
        """Remove a tracepoint by ID. Returns the removed tracepoint or None."""
        tp = self._tracepoints.pop(tp_id, None)
        self._last_hit_times.pop(tp_id, None)
        return tp

    def get_log(
        self,
        since: float | None = None,
        tracepoint_id: str | None = None,
    ) -> list[TraceEntry]:
        """Get trace log entries, optionally filtered."""
        entries = list(self._trace_buffer)
        if since is not None:
            entries = [e for e in entries if e.timestamp >= since]
        if tracepoint_id is not None:
            entries = [e for e in entries if e.tracepoint_id == tracepoint_id]
        return entries

    def mark_trace_cursor(self, tracepoint_id: str | None = None) -> dict[str, Any]:
        """Return a cursor for the current trace log boundary."""
        all_entries = self.get_log()
        entries = self.get_log(tracepoint_id=tracepoint_id)
        return self._build_trace_cursor(
            entries,
            tracepoint_id=tracepoint_id,
            global_entries=all_entries,
        )

    def get_trace_delta(
        self,
        cursor: dict[str, Any] | float | None,
        *,
        limit: int | None = None,
        tracepoint_id: str | None = None,
    ) -> dict[str, Any]:
        """Return trace entries after a cursor plus continuation metadata."""
        after_timestamp, after_ordinal, cursor_tracepoint_id = self._parse_trace_cursor(
            cursor,
            tracepoint_id=tracepoint_id,
        )
        effective_tracepoint_id = (
            tracepoint_id if tracepoint_id is not None else cursor_tracepoint_id
        )
        if self._uses_global_cursor_boundary(cursor, tracepoint_id=tracepoint_id):
            after_ordinal = self._filter_ordinal_at_global_boundary(
                after_timestamp=after_timestamp,
                global_after_ordinal=after_ordinal,
                tracepoint_id=tracepoint_id,
            )

        retained_entries = self.get_log(tracepoint_id=effective_tracepoint_id)
        oldest_retained = retained_entries[0].timestamp if retained_entries else None
        stale = self._is_stale_cursor(
            cursor,
            after_timestamp=after_timestamp,
            after_ordinal=after_ordinal,
            oldest_retained=oldest_retained,
            retained_entries=retained_entries,
        )

        entries = retained_entries
        if after_timestamp is not None:
            entries = self._entries_after_cursor(entries, after_timestamp, after_ordinal)

        available = len(entries)
        bounded_entries = entries
        if limit is not None:
            bounded_entries = entries[: max(limit, 0)]

        next_cursor: dict[str, Any]
        if limit is not None and max(limit, 0) == 0 and available > 0:
            next_cursor = self._build_unadvanced_trace_cursor(
                cursor,
                after_timestamp=after_timestamp,
                after_ordinal=after_ordinal,
                tracepoint_id=effective_tracepoint_id,
                oldest_retained=oldest_retained,
                retained_count=len(retained_entries),
            )
        elif bounded_entries:
            next_cursor = self._build_trace_cursor_for_boundary(
                retained_entries,
                bounded_entries[-1],
                tracepoint_id=effective_tracepoint_id,
            )
        elif after_timestamp is not None:
            next_cursor = {
                "after_timestamp": after_timestamp,
                "after_ordinal": after_ordinal,
                "tracepoint_id": effective_tracepoint_id,
                "buffer_start_timestamp": oldest_retained,
                "buffer_size": len(retained_entries),
                "append_generation": self._trace_append_generation,
                "drop_generation": self._trace_drop_generation,
            }
        else:
            next_cursor = self._build_trace_cursor(
                retained_entries,
                tracepoint_id=effective_tracepoint_id,
            )

        return {
            "entries": bounded_entries,
            "available": available,
            "total": len(bounded_entries),
            "limit": limit,
            "limited": limit is not None and available > max(limit, 0),
            "truncated": self.is_log_full,
            "stale": stale,
            "dropped_count": None if stale else 0,
            "cursor": cursor,
            "next_cursor": next_cursor,
        }

    def clear_log(self) -> int:
        """Clear trace buffer. Returns count of cleared entries."""
        count = len(self._trace_buffer)
        self._trace_buffer.clear()
        return count

    def _parse_trace_cursor(
        self,
        cursor: dict[str, Any] | float | None,
        *,
        tracepoint_id: str | None,
    ) -> tuple[float | None, int, str | None]:
        if isinstance(cursor, dict):
            use_global_boundary = self._uses_global_cursor_boundary(
                cursor,
                tracepoint_id=tracepoint_id,
            )
            timestamp_key = (
                "global_after_timestamp"
                if use_global_boundary and "global_after_timestamp" in cursor
                else "after_timestamp"
            )
            ordinal_key = (
                "global_after_ordinal"
                if use_global_boundary and "global_after_ordinal" in cursor
                else "after_ordinal"
            )
            after_timestamp = cursor.get(timestamp_key)
            if after_timestamp is not None:
                after_timestamp = float(after_timestamp)
            after_ordinal = int(cursor.get(ordinal_key) or 0)
            parsed_tracepoint_id = cursor.get("tracepoint_id")
            if parsed_tracepoint_id is not None:
                parsed_tracepoint_id = str(parsed_tracepoint_id)
            return after_timestamp, after_ordinal, parsed_tracepoint_id
        if cursor is None:
            return None, 0, None
        return float(cursor), 0, None

    @staticmethod
    def _uses_global_cursor_boundary(
        cursor: dict[str, Any] | float | None,
        *,
        tracepoint_id: str | None,
    ) -> bool:
        if not isinstance(cursor, dict) or tracepoint_id is None:
            return False
        cursor_tracepoint_id = cursor.get("tracepoint_id")
        return cursor_tracepoint_id is None or str(cursor_tracepoint_id) != tracepoint_id

    def _filter_ordinal_at_global_boundary(
        self,
        *,
        after_timestamp: float | None,
        global_after_ordinal: int,
        tracepoint_id: str | None,
    ) -> int:
        if after_timestamp is None or tracepoint_id is None or global_after_ordinal <= 0:
            return 0

        global_seen = 0
        filtered_seen = 0
        for entry in self.get_log():
            if entry.timestamp != after_timestamp:
                continue
            global_seen += 1
            if entry.tracepoint_id == tracepoint_id:
                filtered_seen += 1
            if global_seen >= global_after_ordinal:
                return filtered_seen
        return filtered_seen

    def _build_trace_cursor(
        self,
        entries: list[TraceEntry],
        *,
        tracepoint_id: str | None,
        global_entries: list[TraceEntry] | None = None,
    ) -> dict[str, Any]:
        global_entries = entries if global_entries is None else global_entries
        return {
            "after_timestamp": entries[-1].timestamp if entries else None,
            "after_ordinal": (
                sum(1 for entry in entries if entry.timestamp == entries[-1].timestamp)
                if entries
                else 0
            ),
            "global_after_timestamp": global_entries[-1].timestamp if global_entries else None,
            "global_after_ordinal": (
                sum(
                    1
                    for entry in global_entries
                    if entry.timestamp == global_entries[-1].timestamp
                )
                if global_entries
                else 0
            ),
            "tracepoint_id": tracepoint_id,
            "buffer_start_timestamp": entries[0].timestamp if entries else None,
            "buffer_size": len(entries),
            "append_generation": self._trace_append_generation,
            "drop_generation": self._trace_drop_generation,
        }

    def _build_unadvanced_trace_cursor(
        self,
        cursor: dict[str, Any] | float | None,
        *,
        after_timestamp: float | None,
        after_ordinal: int,
        tracepoint_id: str | None,
        oldest_retained: float | None,
        retained_count: int,
    ) -> dict[str, Any]:
        if after_timestamp is not None:
            return {
                "after_timestamp": after_timestamp,
                "after_ordinal": after_ordinal,
                "tracepoint_id": tracepoint_id,
                "buffer_start_timestamp": oldest_retained,
                "buffer_size": retained_count,
                "append_generation": self._trace_append_generation,
                "drop_generation": self._trace_drop_generation,
            }
        if isinstance(cursor, dict):
            next_cursor = dict(cursor)
            next_cursor["after_ordinal"] = int(next_cursor.get("after_ordinal") or 0)
            next_cursor["tracepoint_id"] = tracepoint_id
            return next_cursor
        return self._build_trace_cursor([], tracepoint_id=tracepoint_id)

    @staticmethod
    def _entries_after_cursor(
        entries: list[TraceEntry],
        after_timestamp: float,
        after_ordinal: int,
    ) -> list[TraceEntry]:
        same_timestamp_seen = 0
        result: list[TraceEntry] = []
        for entry in entries:
            if entry.timestamp > after_timestamp:
                result.append(entry)
                continue
            if entry.timestamp == after_timestamp:
                same_timestamp_seen += 1
                if same_timestamp_seen > after_ordinal:
                    result.append(entry)
        return result

    def _build_trace_cursor_for_boundary(
        self,
        entries: list[TraceEntry],
        boundary: TraceEntry,
        *,
        tracepoint_id: str | None,
    ) -> dict[str, Any]:
        after_ordinal = 0
        for entry in entries:
            if entry.timestamp == boundary.timestamp:
                after_ordinal += 1
            if entry is boundary:
                break
        return {
            "after_timestamp": boundary.timestamp,
            "after_ordinal": after_ordinal,
            "tracepoint_id": tracepoint_id,
            "buffer_start_timestamp": entries[0].timestamp if entries else None,
            "buffer_size": len(entries),
            "append_generation": self._trace_append_generation,
            "drop_generation": self._trace_drop_generation,
        }

    def _is_stale_cursor(
        self,
        cursor: dict[str, Any] | float | None,
        *,
        after_timestamp: float | None,
        after_ordinal: int,
        oldest_retained: float | None,
        retained_entries: list[TraceEntry],
    ) -> bool:
        if after_timestamp is not None:
            if oldest_retained is None or oldest_retained > after_timestamp:
                if isinstance(cursor, dict):
                    cursor_drop_generation = int(cursor.get("drop_generation") or 0)
                    return self._trace_drop_generation > cursor_drop_generation
                return True
            retained_boundary_count = sum(
                1 for entry in retained_entries if entry.timestamp == after_timestamp
            )
            return oldest_retained == after_timestamp and retained_boundary_count < after_ordinal
        if not isinstance(cursor, dict):
            return False
        marked_empty_log = (
            cursor.get("after_timestamp") is None
            and cursor.get("buffer_start_timestamp") is None
            and cursor.get("buffer_size") == 0
        )
        cursor_drop_generation = int(cursor.get("drop_generation") or 0)
        return marked_empty_log and self._trace_drop_generation > cursor_drop_generation

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path for comparison: forward slashes + platform-aware case folding."""
        return os.path.normcase(path.replace("\\", "/"))

    def find_tracepoint_for_location(self, file: str, line: int) -> Tracepoint | None:
        """Find an active tracepoint matching a file:line location.

        Uses two-stage matching:
        1. Full path comparison (normalized)
        2. Filename-only fallback (handles PDB path mismatches)
        """
        if not file:
            return None
        normalized = self._normalize_path(file)
        filename = os.path.normcase(os.path.basename(file))

        # Stage 1: full path match
        for tp in self._tracepoints.values():
            if not tp.active:
                continue
            if tp.line != line and tp.dap_line != line:
                continue
            tp_norm = self._normalize_path(tp.file)
            if tp_norm == normalized:
                logger.debug("Tracepoint %s matched by full path: %s:%d", tp.id, file, line)
                return tp

        # Stage 2: filename-only fallback (PDB may store different directory)
        # Uses os.path.normcase for platform-correct comparison (case-insensitive
        # on Windows, case-sensitive on Linux/macOS)
        for tp in self._tracepoints.values():
            if not tp.active:
                continue
            if tp.line != line and tp.dap_line != line:
                continue
            tp_filename = os.path.normcase(os.path.basename(tp.file))
            if tp_filename == filename:
                logger.info(
                    "Tracepoint %s matched by filename fallback: frame=%s tp=%s "
                    "line=%d (requested=%d, dap=%s)",
                    tp.id,
                    file,
                    tp.file,
                    line,
                    tp.line,
                    tp.dap_line,
                )
                return tp

        logger.debug(
            "No tracepoint match for %s:%d (checked %d tracepoints, normalized=%s)",
            file,
            line,
            len(self._tracepoints),
            normalized,
        )
        return None

    def set_dap_line(self, tp_id: str, dap_line: int | None) -> bool:
        """Record the DAP-adjusted line for a tracepoint. Returns True if updated."""
        tp = self._tracepoints.get(tp_id)
        if tp is None:
            return False
        tp.dap_line = dap_line
        if dap_line is not None and dap_line != tp.line:
            logger.info(
                "Tracepoint %s line adjusted by DAP: requested=%d, actual=%d "
                "(typical for async state machines)",
                tp_id,
                tp.line,
                dap_line,
            )
        return True

    def set_dap_line_for_breakpoint(self, breakpoint_id: int, dap_line: int | None) -> None:
        """Propagate DAP line change for a tracepoint-owned breakpoint."""
        for tp in self._tracepoints.values():
            if tp.breakpoint_id == breakpoint_id:
                tp.dap_line = dap_line
                if dap_line is not None and dap_line != tp.line:
                    logger.info(
                        "Tracepoint %s line updated via adapter event: requested=%d, actual=%d",
                        tp.id,
                        tp.line,
                        dap_line,
                    )
                return

    def _is_rate_limited(self, tp_id: str) -> bool:
        """Check if tracepoint is hitting too frequently."""
        now = time.monotonic()
        last = self._last_hit_times.get(tp_id, 0.0)
        if (now - last) < RATE_LIMIT_INTERVAL_SECONDS:
            return True
        self._last_hit_times[tp_id] = now
        return False

    async def on_tracepoint_hit(
        self,
        tp: Tracepoint,
        session: SessionManager,
        thread_id: int,
        *,
        has_user_breakpoint: bool = False,
        top_frame: Any | None = None,
    ) -> None:
        """Handle a tracepoint hit: evaluate expression, optionally resume, log result.

        Called from SessionManager._check_tracepoint when a tracepoint is detected.
        The program is already paused at this point.

        Args:
            tp: The matched tracepoint.
            session: Active SessionManager.
            thread_id: Thread that hit the tracepoint.
            has_user_breakpoint: If True, a user-defined breakpoint also exists at
                this location — do NOT auto-resume after evaluation.
            top_frame: Pre-fetched top stack frame (avoids redundant DAP call).
        """
        async with self._lock:
            tp.hit_count += 1

            # Rate limiting
            if self._is_rate_limited(tp.id):
                self._trace_buffer.append(
                    TraceEntry(
                        timestamp=time.monotonic(),
                        file=tp.file,
                        line=tp.line,
                        expression=tp.expression,
                        value="<rate limited>",
                        thread_id=thread_id,
                        tracepoint_id=tp.id,
                    )
                )
                if not has_user_breakpoint:
                    try:
                        session.prepare_for_execution()
                        await session._client.continue_execution(thread_id)
                    except Exception as e:
                        logger.warning("Failed to resume after rate-limited tracepoint: %s", e)
                return

            # Evaluate with short timeout
            value: str
            try:
                frame_id = top_frame.id if top_frame else None
                if frame_id is None:
                    frames = await session.get_stack_trace(thread_id=thread_id, levels=1)
                    frame_id = frames[0].id if frames else None

                response = await asyncio.wait_for(
                    session._client.evaluate(tp.expression, frame_id),
                    timeout=EVALUATE_TIMEOUT_SECONDS,
                )
                if response.success:
                    value = response.body.get("result", "")
                else:
                    value = f"<error: {response.message or 'evaluation failed'}>"
            except asyncio.TimeoutError:
                value = "<timeout>"
            except Exception as e:
                value = f"<error: {type(e).__name__}>"

            # Log entry
            self._trace_buffer.append(
                TraceEntry(
                    timestamp=time.monotonic(),
                    file=tp.file,
                    line=tp.line,
                    expression=tp.expression,
                    value=value,
                    thread_id=thread_id,
                    tracepoint_id=tp.id,
                )
            )

            # Resume only if no user breakpoint at this location
            if not has_user_breakpoint:
                try:
                    session.prepare_for_execution()
                    await session._client.continue_execution(thread_id)
                except Exception as e:
                    logger.warning("Failed to resume after tracepoint evaluation: %s", e)
