"""FD-002 real-stdio fixture: a minimal standalone MCP server used only by the
FD-002 progress/logging relay tests (Python and .NET), independent of the
production ``netcoredbg_mcp`` package. It is never imported by or registered as
a ``netcoredbg_mcp`` tool - this keeps FD-002's test-only surface fully separate
from the production tool catalog FD-004 owns, and from ``PythonBackendProcess``'s
fixed ``-m netcoredbg_mcp`` launch (real callers always launch the production
package; only this dedicated fixture launches this probe).

Exposes one tool, ``emit_progress_and_logs``, that reports a caller-chosen number
of strictly increasing ``notifications/progress`` updates interleaved with
``notifications/message`` log lines for the same call, then returns a structured
result - proving per-token order and "no progress after the terminal result" are
observable with a real MCP server, not a mock. ``hold_seconds`` inserts a real
await point between steps so a caller can deterministically cancel mid-run.

``--with-logging-capability`` registers a ``logging/setLevel`` handler so this
probe advertises the ``logging`` server capability - the MCP Python SDK's
lowlevel ``Server.get_capabilities`` only sets ``LoggingCapability`` when such a
handler is registered, mirroring direct ``netcoredbg_mcp``'s current
absent-capability behaviour when the flag is omitted.
"""

from __future__ import annotations

import argparse
import asyncio

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import LoggingLevel


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
    ) -> dict:
        """Reports `steps` increasing progress notifications interleaved with
        `steps` log messages, then returns completion and active logging level."""
        for step in range(1, steps + 1):
            await ctx.report_progress(progress=step, total=steps, message=f"step-{step}")
            await ctx.log("info", f"log-{step}", logger_name=logger_name)
            if hold_seconds:
                await asyncio.sleep(hold_seconds)
        return {"steps_completed": steps, "active_logging_level": active_logging_level}

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
    args = parser.parse_args()

    build_server(args.with_logging_capability).run()


if __name__ == "__main__":
    main()
