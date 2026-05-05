"""Tests for CR-002 loaded source, disassembly, and location tools."""

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


def make_response(success: bool = True, body: dict | None = None, message: str | None = None):
    return DAPResponse(
        seq=1,
        request_seq=1,
        success=success,
        command="inspection",
        body=body or {},
        message=message,
    )


def make_manager() -> SessionManager:
    manager = SessionManager(netcoredbg_path="/path")
    manager.client._capabilities = {}
    return manager


def register_tools(manager: SessionManager):
    from netcoredbg_mcp.tools.inspection import register_inspection_tools

    registry = ToolRegistry()
    register_inspection_tools(registry, manager, lambda ctx: None)
    return registry.tools


@pytest.mark.asyncio
async def test_get_loaded_sources_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsLoadedSourcesRequest": False}
    manager.get_loaded_sources = AsyncMock()
    tools = register_tools(manager)

    response = await tools["get_loaded_sources"]()

    assert "error" in response
    assert "supportsLoadedSourcesRequest" in response["error"]
    manager.get_loaded_sources.assert_not_called()


@pytest.mark.asyncio
async def test_get_loaded_sources_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsLoadedSourcesRequest": True}
    manager.get_loaded_sources = AsyncMock(return_value=[{
        "name": "Program.cs",
        "path": "C:/src/Program.cs",
        "sourceReference": 7,
        "origin": None,
        "presentationHint": "normal",
        "adapterData": {"kind": "source"},
    }])
    tools = register_tools(manager)

    response = await tools["get_loaded_sources"]()

    assert response["data"]["count"] == 1
    assert response["data"]["sources"][0]["sourceReference"] == 7
    manager.get_loaded_sources.assert_called_once_with()


@pytest.mark.asyncio
async def test_get_loaded_sources_manager_maps_and_tracks_state():
    manager = make_manager()
    manager.client.loaded_sources = AsyncMock(return_value=make_response(body={
        "sources": [{
            "name": "Program.cs",
            "path": "C:/src/Program.cs",
            "sourceReference": 7,
            "origin": "generated",
            "presentationHint": "normal",
            "adapterData": {"kind": "source"},
        }],
    }))

    sources = await manager.get_loaded_sources()

    assert sources[0]["sourceReference"] == 7
    assert len(manager.state.loaded_sources) == 1


@pytest.mark.asyncio
async def test_disassemble_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsDisassembleRequest": False}
    manager.disassemble = AsyncMock()
    tools = register_tools(manager)

    response = await tools["disassemble"]("0x1234")

    assert "error" in response
    assert "supportsDisassembleRequest" in response["error"]
    assert "--enable-disassembly" in response["error"]
    manager.disassemble.assert_not_called()


@pytest.mark.asyncio
async def test_disassemble_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsDisassembleRequest": True}
    manager.disassemble = AsyncMock(return_value=[{
        "address": "0x1234",
        "instructionBytes": "55",
        "instruction": "push rbp",
        "symbol": "main",
        "presentationHint": "normal",
    }])
    tools = register_tools(manager)

    response = await tools["disassemble"](
        "0x1234",
        offset=4,
        instruction_offset=-1,
        instruction_count=8,
        resolve_symbols=False,
    )

    assert response["data"]["instructions"][0]["instruction"] == "push rbp"
    manager.disassemble.assert_called_once_with(
        "0x1234",
        offset=4,
        instruction_offset=-1,
        instruction_count=8,
        resolve_symbols=False,
    )


@pytest.mark.asyncio
async def test_disassemble_rejects_invalid_args():
    manager = make_manager()

    with pytest.raises(ValueError, match="memory_reference is required"):
        await manager.disassemble("", instruction_count=1)

    with pytest.raises(ValueError, match="instruction_count must be greater than 0"):
        await manager.disassemble("0x1234", instruction_count=0)


@pytest.mark.asyncio
async def test_get_locations_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsLocationsRequest": False}
    manager.get_locations = AsyncMock()
    tools = register_tools(manager)

    response = await tools["get_locations"](42)

    assert "error" in response
    assert "supportsLocationsRequest" in response["error"]
    manager.get_locations.assert_not_called()


@pytest.mark.asyncio
async def test_get_locations_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsLocationsRequest": True}
    manager.get_locations = AsyncMock(return_value={
        "source": {"path": "C:/src/Program.cs"},
        "line": 12,
        "column": 3,
        "end_line": 12,
        "end_column": 8,
        "endLine": 12,
        "endColumn": 8,
    })
    tools = register_tools(manager)

    response = await tools["get_locations"](42)

    assert response["data"]["source"]["path"] == "C:/src/Program.cs"
    assert response["data"]["end_line"] == 12
    manager.get_locations.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_get_locations_rejects_invalid_args():
    manager = make_manager()

    with pytest.raises(ValueError, match="location_reference must be greater than 0"):
        await manager.get_locations(0)


@pytest.mark.asyncio
async def test_manager_disassemble_and_locations_map_dap_fields():
    manager = make_manager()
    manager.client.disassemble = AsyncMock(return_value=make_response(body={
        "instructions": [{
            "address": "0x1234",
            "instructionBytes": "55",
            "instruction": "push rbp",
            "presentationHint": "normal",
        }],
    }))
    manager.client.locations = AsyncMock(return_value=make_response(body={
        "source": {"path": "C:/src/Program.cs"},
        "line": 20,
        "column": 1,
        "endLine": 20,
        "endColumn": 5,
    }))

    instructions = await manager.disassemble("0x1234", instruction_count=1)
    location = await manager.get_locations(99)

    assert instructions[0]["address"] == "0x1234"
    assert location["line"] == 20
    assert location["end_line"] == 20
