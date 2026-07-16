"""FD-002 real-stdio fixture: a minimal standalone MCP server used only by the
FD-002 progress/logging relay tests (Python and .NET), independent of the
production ``netcoredbg_mcp`` package. It is never imported by or registered as
a ``netcoredbg_mcp`` tool - this keeps FD-002's test-only surface fully separate
from the production tool catalog FD-004 owns, and from ``PythonBackendProcess``'s
fixed ``-m netcoredbg_mcp`` launch (real callers always launch the production
package; only this dedicated fixture launches this probe).

Exposes one tool, ``emit_progress_and_logs``, that reports a caller-chosen number
of strictly increasing ``notifications/progress`` updates interleaved with
structured ``notifications/message`` log payloads carrying notification
``_meta``, then returns the request ``_meta`` and emission counts. Logging is
emitted even without ``--with-logging-capability`` on purpose, so the host proves
it suppresses a misbehaving non-advertising upstream. Optional turn coordination
makes two real stdio calls interleave deterministically.

``hold_seconds`` inserts a real await point between steps so a caller can
deterministically cancel mid-run. ``--with-logging-capability`` registers a
``logging/setLevel`` handler so this probe advertises the ``logging`` server
capability - the MCP Python SDK's lowlevel ``Server.get_capabilities`` only sets
``LoggingCapability`` when such a handler is registered, mirroring direct
``netcoredbg_mcp``'s current absent-capability behaviour when the flag is omitted.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from anyio import ClosedResourceError
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import LoggingLevel


_interleave_conditions: dict[str, asyncio.Condition] = {}
_interleave_turns: dict[str, int] = {}


async def _wait_for_turn(key: str, turn: int) -> asyncio.Condition:
    condition = _interleave_conditions.setdefault(key, asyncio.Condition())
    async with condition:
        await condition.wait_for(lambda: _interleave_turns.get(key, 0) == turn)
    return condition


async def _finish_turn(key: str, condition: asyncio.Condition) -> None:
    async with condition:
        _interleave_turns[key] = _interleave_turns.get(key, 0) + 1
        condition.notify_all()


def build_server(advertise_logging: bool) -> FastMCP:
    """Builds the probe server; see module docstring for ``advertise_logging``."""
    mcp = FastMCP("fd002-notification-probe")
    active_logging_level: LoggingLevel = "debug"

    @mcp.tool()
    async def emit_progress_and_logs(
        ctx: Context,
        steps: int = 3,
        hold_seconds: float = 0.0,
        logger_name: str = "fixture",
        interleave_key: str | None = None,
        interleave_index: int = 0,
        interleave_participants: int = 1,
    ) -> dict:
        """Emit progress and deliberately capability-independent structured logs."""
        request_meta = (
            ctx.request_context.meta.model_dump(by_alias=True, exclude_none=True)
            if ctx.request_context.meta
            else None
        )
        progress_token = ctx.request_context.meta.progressToken if ctx.request_context.meta else None

        for step in range(1, steps + 1):
            condition = None
            if interleave_key is not None:
                turn = (step - 1) * interleave_participants + interleave_index
                condition = await _wait_for_turn(interleave_key, turn)

            await ctx.report_progress(progress=step, total=steps, message=f"step-{step}")
            await ctx.request_context.session.send_notification(
                types.ServerNotification(
                    types.LoggingMessageNotification(
                        params=types.LoggingMessageNotificationParams(
                            level="info",
                            logger=logger_name,
                            data={"message": f"log-{step}", "step": step, "progressToken": progress_token},
                            _meta={
                                "fixture": "fd002-notification-probe",
                                "step": step,
                                "progressToken": progress_token,
                            },
                        )
                    )
                ),
                related_request_id=ctx.request_context.request_id,
            )

            if condition is not None:
                await _finish_turn(interleave_key, condition)
            if hold_seconds:
                await asyncio.sleep(hold_seconds)

        return {
            "steps_completed": steps,
            "logs_emitted": steps,
            "active_logging_level": active_logging_level,
            "request_meta": request_meta,
        }

    if advertise_logging:

        @mcp._mcp_server.set_logging_level()
        async def _set_logging_level(level: LoggingLevel) -> None:
            nonlocal active_logging_level
            active_logging_level = level

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-logging-capability",
        action="store_true",
        help="Register logging/setLevel so this probe advertises the logging capability.",
    )
    parser.add_argument(
        "--shutdown-marker",
        help="Write this marker only after a clean or expected closed-stdio shutdown.",
    )
    args = parser.parse_args()

    try:
        build_server(args.with_logging_capability).run()
    except* ClosedResourceError:
        # Closing stdio while a cancelled request is unwinding is a normal fixture shutdown.
        # Any sibling exception in the group remains unhandled and therefore fails the child.
        pass

    if args.shutdown_marker:
        Path(args.shutdown_marker).write_text("clean\n", encoding="utf-8")


if __name__ == "__main__":
    main()
