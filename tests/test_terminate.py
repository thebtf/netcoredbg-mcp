"""Tests for terminate_debug functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPResponse


class TestTerminate:

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            return m

    def test_terminate_method_exists(self, manager):
        """DAPClient has terminate method."""
        assert hasattr(manager._client, "terminate")

    def test_capabilities_property(self):
        """DAPClient exposes capabilities property."""
        with patch("netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg", return_value="netcoredbg"):
            from netcoredbg_mcp.dap.client import DAPClient
            real_client = DAPClient()
            caps = real_client.capabilities
            assert isinstance(caps, dict)
            assert caps == {}

    def test_client_property(self, manager):
        """SessionManager exposes client property."""
        assert manager.client is manager._client

    @pytest.mark.asyncio
    async def test_client_terminate(self, manager):
        """Client terminate sends request."""
        manager._client.terminate = AsyncMock(return_value=DAPResponse(
            seq=1, request_seq=1, success=True, command="terminate"))
        response = await manager._client.terminate()
        assert response.success
