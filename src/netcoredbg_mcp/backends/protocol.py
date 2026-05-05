"""Backend-agnostic debugger capability probes."""

from __future__ import annotations

from typing import Protocol

from ..dap import DAPClient


class DebuggerBackend(Protocol):
    """Capability surface consumed by MCP tools."""

    def supports_read_memory(self) -> bool:
        """Return whether readMemory can be sent."""

    def supports_write_memory(self) -> bool:
        """Return whether writeMemory can be sent."""

    def supports_disassemble(self) -> bool:
        """Return whether disassemble can be sent."""

    def supports_loaded_sources(self) -> bool:
        """Return whether loadedSources can be sent."""

    def supports_locations(self) -> bool:
        """Return whether locations can be sent."""

    def supports_step_in_targets(self) -> bool:
        """Return whether stepInTargets can be sent."""

    def supports_progress_reporting(self) -> bool:
        """Return whether progress events are expected."""


class NetcoredbgBackend:
    """DebuggerBackend adapter over the current DAPClient."""

    def __init__(self, client: DAPClient):
        self._client = client

    def supports_read_memory(self) -> bool:
        return self._supports("supportsReadMemoryRequest")

    def supports_write_memory(self) -> bool:
        return self._supports("supportsWriteMemoryRequest")

    def supports_disassemble(self) -> bool:
        return self._supports("supportsDisassembleRequest")

    def supports_loaded_sources(self) -> bool:
        return self._supports("supportsLoadedSourcesRequest")

    def supports_locations(self) -> bool:
        return self._supports("supportsLocationsRequest")

    def supports_step_in_targets(self) -> bool:
        return self._supports("supportsStepInTargetsRequest")

    def supports_progress_reporting(self) -> bool:
        # DAP supportsProgressReporting is a client initialize capability, not
        # an adapter response capability. This client always advertises it.
        return True

    def _supports(self, capability: str) -> bool:
        return bool(self._client.capabilities.get(capability, False))
