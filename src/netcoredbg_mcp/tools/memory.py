"""Memory inspection tools."""

from collections.abc import Callable
from typing import Any, cast

from mcp.server.fastmcp import Context, FastMCP

from ..backends import NetcoredbgBackend
from ..response import build_error_response, build_response
from ..session import SessionManager

ToolResponse = dict[str, Any]


def register_memory_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register raw memory tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(  # type: ignore[untyped-decorator]
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def read_memory(
        memory_reference: str,
        offset: int = 0,
        count: int = 0,
    ) -> ToolResponse:
        """Read raw memory bytes from a debugger memoryReference.

        Capability-gated: current netcoredbg builds usually return an unsupported
        error unless they advertise supportsReadMemoryRequest.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.

        Args:
            memory_reference: DAP memoryReference from a variable or stack frame
            offset: Byte offset from the memory reference
            count: Number of bytes to read; count=0 returns empty data locally
        """
        try:
            backend = NetcoredbgBackend(session.client)
            if not backend.supports_read_memory():
                return cast(
                    ToolResponse,
                    build_error_response(
                        "Adapter does not support readMemory. This feature requires "
                        "the debug adapter to advertise supportsReadMemoryRequest: true "
                        "at initialize. Current build does not.",
                        state=session.state.state,
                    ),
                )
            result = await session.read_memory(memory_reference, offset=offset, count=count)
            return cast(ToolResponse, build_response(data=result, state=session.state.state))
        except Exception as e:
            return cast(ToolResponse, build_error_response(str(e), state=session.state.state))

    @mcp.tool(  # type: ignore[untyped-decorator]
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False)
    )
    async def write_memory(
        ctx: Context,
        memory_reference: str,
        data: str,
        offset: int = 0,
        allow_partial: bool = False,
    ) -> ToolResponse:
        """Write base64-encoded bytes to a debugger memoryReference.

        DESTRUCTIVE: this mutates debuggee memory and can corrupt process state.
        Capability-gated: current netcoredbg builds usually return an unsupported
        error unless they advertise supportsWriteMemoryRequest.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.

        Args:
            memory_reference: DAP memoryReference from a variable or stack frame
            data: Base64-encoded bytes to write
            offset: Byte offset from the memory reference
            allow_partial: Let the adapter perform a partial write if needed
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return cast(
                    ToolResponse,
                    build_error_response(access_error, state=session.state.state),
                )

            backend = NetcoredbgBackend(session.client)
            if not backend.supports_write_memory():
                return cast(
                    ToolResponse,
                    build_error_response(
                        "Adapter does not support writeMemory. This feature requires "
                        "the debug adapter to advertise supportsWriteMemoryRequest: true "
                        "at initialize. Current build does not.",
                        state=session.state.state,
                    ),
                )
            result = await session.write_memory(
                memory_reference,
                data,
                offset=offset,
                allow_partial=allow_partial,
            )
            return cast(
                ToolResponse,
                build_response(
                    data=result,
                    state=session.state.state,
                    message="Memory write completed. Debuggee state may have changed.",
                ),
            )
        except Exception as e:
            return cast(ToolResponse, build_error_response(str(e), state=session.state.state))
