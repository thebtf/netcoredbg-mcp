"""FD-007 direct-Python baseline: netcoredbg_mcp.mux (SessionOwnership,
get_mux_session_id).

Establishes ground-truth behavior for mux session ownership with no .NET host
involved: pure unit tests of the SessionOwnership state machine and
get_mux_session_id's context extraction, plus a real (non-mocked)
FastMCP server exercised over an in-memory MCP client session
(mcp.shared.memory.create_connected_server_and_client_session) so
_meta.muxSessionId truly flows through the SDK's own request parsing into a
real Context, not a hand-rolled fake.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, RequestParams, TextContent

from netcoredbg_mcp.mux import SESSION_OWNERSHIP_TIMEOUT, SessionOwnership, get_mux_session_id
from netcoredbg_mcp.server import create_server


def _payload(result: CallToolResult) -> dict:
    assert not result.isError, f"tool call reported isError: {result}"
    first = result.content[0]
    assert isinstance(first, TextContent)
    return json.loads(first.text)


class TestSessionOwnershipUnit:
    """Pure state-machine behavior of SessionOwnership, no MCP layer involved."""

    def test_not_behind_mux_is_always_allowed_and_claims_nothing(self):
        ownership = SessionOwnership()
        assert ownership.check_access(None) is None
        assert ownership.owner is None

    def test_first_claim_auto_claims_and_same_session_stays_allowed(self):
        ownership = SessionOwnership()
        assert ownership.check_access("agent-A") is None
        assert ownership.owner == "agent-A"
        assert ownership.check_access("agent-A") is None

    def test_competing_session_is_denied_and_names_the_owner(self):
        ownership = SessionOwnership()
        ownership.claim("agent-A")

        error = ownership.check_access("agent-B")

        assert error is not None
        assert "owned by another agent" in error
        assert "agent-A" in error
        assert "cleanup_processes(force=True)" in error
        # A denied request never changes ownership.
        assert ownership.owner == "agent-A"

    def test_release_clears_ownership_and_a_new_session_can_claim(self):
        ownership = SessionOwnership()
        ownership.claim("agent-A")

        ownership.release()

        assert ownership.owner is None
        assert ownership.check_access("agent-B") is None
        assert ownership.owner == "agent-B"

    def test_idle_ownership_expires_after_timeout_and_a_new_session_can_claim(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("netcoredbg_mcp.mux.time.monotonic", lambda: clock[0])
        ownership = SessionOwnership()
        ownership.claim("agent-A")
        assert ownership.owner == "agent-A"

        clock[0] += SESSION_OWNERSHIP_TIMEOUT + 1

        assert ownership.owner is None
        assert ownership.check_access("agent-B") is None
        assert ownership.owner == "agent-B"

    def test_touch_on_the_owning_session_resets_the_idle_clock(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("netcoredbg_mcp.mux.time.monotonic", lambda: clock[0])
        ownership = SessionOwnership()
        ownership.claim("agent-A")

        clock[0] += SESSION_OWNERSHIP_TIMEOUT - 1
        assert ownership.check_access("agent-A") is None  # touches last_activity

        clock[0] += SESSION_OWNERSHIP_TIMEOUT - 1
        # Elapsed since the touch is still under the timeout, so ownership survives.
        assert ownership.owner == "agent-A"
        assert ownership.check_access("agent-B") is not None


class TestGetMuxSessionId:
    """get_mux_session_id's context extraction, using the real pydantic Meta model."""

    def test_missing_meta_returns_none(self):
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta=None))
        assert get_mux_session_id(ctx) is None

    def test_mux_session_id_extra_field_is_extracted(self):
        meta = RequestParams.Meta(muxSessionId="agent-Z")
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta))
        assert get_mux_session_id(ctx) == "agent-Z"

    def test_meta_present_without_mux_session_id_returns_none(self):
        meta = RequestParams.Meta()
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta))
        assert get_mux_session_id(ctx) is None

    def test_malformed_context_returns_none_instead_of_raising(self):
        assert get_mux_session_id(SimpleNamespace()) is None


class TestDirectPythonSessionOwnershipThroughARealSession:
    """Ground truth for T-FD007-02: SessionOwnership exercised through a real
    ClientSession/Context round trip, not a hand-rolled ctx.

    Each test opens its own create_connected_server_and_client_session inline
    (rather than via a fixture) so setup, calls, and teardown all run in the
    same asyncio task - an async-generator fixture crosses a task boundary
    that trips anyio's task-group cancel-scope check.
    """

    @pytest.mark.asyncio
    async def test_same_owner_can_mutate_repeatedly(self, tmp_path):
        server = create_server(str(tmp_path))
        async with create_connected_server_and_client_session(server) as session:
            first = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-A"}
                )
            )
            assert "error" not in first

            second = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-A"}
                )
            )
            assert "error" not in second

    @pytest.mark.asyncio
    async def test_competing_owner_is_denied(self, tmp_path):
        server = create_server(str(tmp_path))
        async with create_connected_server_and_client_session(server) as session:
            claimed = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-A"}
                )
            )
            assert "error" not in claimed

            denied = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-B"}
                )
            )
            assert "error" in denied
            assert "agent-A" in denied["error"]
            assert "owned by another agent" in denied["error"]

    @pytest.mark.asyncio
    async def test_read_only_observation_is_always_permitted(self, tmp_path):
        server = create_server(str(tmp_path))
        async with create_connected_server_and_client_session(server) as session:
            claimed = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-A"}
                )
            )
            assert "error" not in claimed

            observed = _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": False}, meta={"muxSessionId": "agent-B"}
                )
            )
            assert "error" not in observed
            assert observed["data"]["action"] == "status"

    @pytest.mark.asyncio
    async def test_no_meta_at_all_is_treated_as_single_client_mode(self, tmp_path):
        server = create_server(str(tmp_path))
        async with create_connected_server_and_client_session(server) as session:
            # Another agent claims ownership first...
            assert "error" not in _payload(
                await session.call_tool(
                    "cleanup_processes", {"force": True}, meta={"muxSessionId": "agent-A"}
                )
            )
            # ...but a call carrying no muxSessionId at all is not "behind mux" and
            # is always allowed, per SessionOwnership.check_access(None).
            unmuxed = _payload(await session.call_tool("cleanup_processes", {"force": True}))
            assert "error" not in unmuxed
