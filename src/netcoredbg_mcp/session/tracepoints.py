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

MAX_TRACE_ENTRIES = 1000
EVALUATE_TIMEOUT_SECONDS = 0.5
RATE_LIMIT_INTERVAL_SECONDS = 0.1  # Max 10 hits/sec per tracepoint



class TracepointManager:
    """Manages client-side tracepoints with rate limiting and trace buffer."""

    def __init__(self) -> None:
        self._tracepoints: dict[str, Tracepoint] = {}
        self._trace_buffer: deque[TraceEntry] = deque(maxlen=MAX_TRACE_ENTRIES)
        self._counter = 0
        self._last_hit_times: dict[str, float] = {}  # tp_id → last hit monotonic time
        self._lock = asyncio.Lock()

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

    def clear_log(self) -> int:
        """Clear trace buffer. Returns count of cleared entries."""
        count = len(self._trace_buffer)
        self._trace_buffer.clear()
        return count

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path for comparison: forward slashes + platform-aware case folding."""
        return os.path.normcase(path.replace("\\", "/"))

    def find_tracepoint_for_location(self, file: str, line: int) -> Tracepoint | None:
        """Find an active tracepoint matching a file:line location."""
        normalized = self._normalize_path(file)
        for tp in self._tracepoints.values():
            if tp.active and self._normalize_path(tp.file) == normalized and tp.line == line:
                return tp
        return None

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
                self._trace_buffer.append(TraceEntry(
                    timestamp=time.monotonic(),
                    file=tp.file,
                    line=tp.line,
                    expression=tp.expression,
                    value="<rate limited>",
                    thread_id=thread_id,
                    tracepoint_id=tp.id,
                ))
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
            self._trace_buffer.append(TraceEntry(
                timestamp=time.monotonic(),
                file=tp.file,
                line=tp.line,
                expression=tp.expression,
                value=value,
                thread_id=thread_id,
                tracepoint_id=tp.id,
            ))

            # Resume only if no user breakpoint at this location
            if not has_user_breakpoint:
                try:
                    session.prepare_for_execution()
                    await session._client.continue_execution(thread_id)
                except Exception as e:
                    logger.warning("Failed to resume after tracepoint evaluation: %s", e)
