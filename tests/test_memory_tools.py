"""Tests for CR-002 memory request wrappers and MCP tools."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from netcoredbg_mcp.dap.protocol import DAPResponse
from netcoredbg_mcp.session import SessionManager


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


def make_manager() -> SessionManager:
    manager = SessionManager(netcoredbg_path="/path")
    manager.client._capabilities = {}
    return manager


def make_response(success: bool = True, body: dict | None = None, message: str | None = None):
    return DAPResponse(
        seq=1,
        request_seq=1,
        success=success,
        command="memory",
        body=body or {},
        message=message,
    )


def register_tools(manager: SessionManager):
    from netcoredbg_mcp.tools.memory import register_memory_tools

    registry = ToolRegistry()
    register_memory_tools(registry, manager, lambda ctx: None)
    return registry.tools


@pytest.mark.asyncio
async def test_read_memory_count_zero_returns_empty_without_dap_call():
    manager = make_manager()
    manager.client.read_memory = AsyncMock()

    result = await manager.read_memory("0x1234", count=0)

    assert result == {"address": "", "unreadable_bytes": 0, "data": ""}
    manager.client.read_memory.assert_not_called()


@pytest.mark.asyncio
async def test_read_memory_negative_count_errors():
    manager = make_manager()

    with pytest.raises(ValueError, match="count must be greater than or equal to 0"):
        await manager.read_memory("0x1234", count=-1)


@pytest.mark.asyncio
async def test_read_memory_success_maps_dap_fields():
    manager = make_manager()
    manager.client.read_memory = AsyncMock(return_value=make_response(body={
        "address": "0x1234",
        "unreadableBytes": 2,
        "data": "AQID",
    }))

    result = await manager.read_memory("0x1234", offset=4, count=16)

    manager.client.read_memory.assert_called_once_with("0x1234", offset=4, count=16)
    assert result == {"address": "0x1234", "unreadable_bytes": 2, "data": "AQID"}


@pytest.mark.asyncio
async def test_read_memory_failure_raises_message():
    manager = make_manager()
    manager.client.read_memory = AsyncMock(return_value=make_response(
        success=False,
        message="not supported",
    ))

    with pytest.raises(RuntimeError, match="not supported"):
        await manager.read_memory("0x1234", count=16)


@pytest.mark.asyncio
async def test_write_memory_rejects_invalid_base64():
    manager = make_manager()

    with pytest.raises(ValueError, match="valid base64"):
        await manager.write_memory("0x1234", "not base64!!")


@pytest.mark.asyncio
async def test_write_memory_success_maps_dap_fields():
    manager = make_manager()
    manager.client.write_memory = AsyncMock(return_value=make_response(body={
        "bytesWritten": 3,
        "offset": 4,
    }))

    result = await manager.write_memory("0x1234", "AQID", offset=4, allow_partial=True)

    manager.client.write_memory.assert_called_once_with(
        "0x1234",
        "AQID",
        offset=4,
        allow_partial=True,
    )
    assert result == {"bytes_written": 3, "offset": 4}


@pytest.mark.asyncio
async def test_read_memory_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsReadMemoryRequest": False}
    manager.read_memory = AsyncMock()
    tools = register_tools(manager)

    response = await tools["read_memory"]("0x1234", count=16)

    assert "error" in response
    assert "supportsReadMemoryRequest" in response["error"]
    manager.read_memory.assert_not_called()


@pytest.mark.asyncio
async def test_read_memory_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsReadMemoryRequest": True}
    manager.read_memory = AsyncMock(return_value={
        "address": "0x1234",
        "unreadable_bytes": 0,
        "data": "AQID",
    })
    tools = register_tools(manager)

    response = await tools["read_memory"]("0x1234", count=3)

    assert response["data"]["data"] == "AQID"
    manager.read_memory.assert_called_once_with("0x1234", offset=0, count=3)


@pytest.mark.asyncio
async def test_write_memory_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsWriteMemoryRequest": False}
    manager.write_memory = AsyncMock()
    tools = register_tools(manager)

    response = await tools["write_memory"](object(), "0x1234", "AQID")

    assert "error" in response
    assert "supportsWriteMemoryRequest" in response["error"]
    manager.write_memory.assert_not_called()


@pytest.mark.asyncio
async def test_write_memory_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsWriteMemoryRequest": True}
    manager.write_memory = AsyncMock(return_value={"bytes_written": 3, "offset": 0})
    tools = register_tools(manager)

    response = await tools["write_memory"](object(), "0x1234", "AQID")

    assert response["data"] == {"bytes_written": 3, "offset": 0}
    assert "Memory write completed" in response["message"]
    manager.write_memory.assert_called_once_with(
        "0x1234",
        "AQID",
        offset=0,
        allow_partial=False,
    )
