"""Tests for debugger backend capability probes."""

from __future__ import annotations

from typing import Any

from netcoredbg_mcp.backends import DebuggerBackend, NetcoredbgBackend


class FakeClient:
    def __init__(self, capabilities: dict[str, Any]):
        self._capabilities = capabilities

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities)


def test_netcoredbg_backend_delegates_capability_flags():
    backend = NetcoredbgBackend(FakeClient({
        "supportsReadMemoryRequest": True,
        "supportsWriteMemoryRequest": False,
        "supportsDisassembleRequest": True,
        "supportsLoadedSourcesRequest": False,
        "supportsLocationsRequest": True,
        "supportsStepInTargetsRequest": True,
        "supportsProgressReporting": False,
    }))

    assert backend.supports_read_memory() is True
    assert backend.supports_write_memory() is False
    assert backend.supports_disassemble() is True
    assert backend.supports_loaded_sources() is False
    assert backend.supports_locations() is True
    assert backend.supports_step_in_targets() is True
    assert backend.supports_progress_reporting() is False


def test_netcoredbg_backend_defaults_missing_capabilities_to_false():
    backend = NetcoredbgBackend(FakeClient({}))

    assert backend.supports_read_memory() is False
    assert backend.supports_write_memory() is False
    assert backend.supports_disassemble() is False
    assert backend.supports_loaded_sources() is False
    assert backend.supports_locations() is False
    assert backend.supports_step_in_targets() is False
    assert backend.supports_progress_reporting() is False


def test_netcoredbg_backend_satisfies_protocol_shape():
    backend: DebuggerBackend = NetcoredbgBackend(FakeClient({
        "supportsReadMemoryRequest": True,
    }))

    assert backend.supports_read_memory() is True
