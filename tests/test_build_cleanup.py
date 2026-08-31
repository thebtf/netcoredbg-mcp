"""Tests for the explicit pre-build owner gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from netcoredbg_mcp.build.cleanup import (
    NoOwnedAdapter,
    OwnedAdapterCleanup,
    consume_prebuild_owner,
    require_prebuild_owner,
)
from netcoredbg_mcp.build.session import BuildSession
from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt


class TestPreBuildOwnerGate:
    """Behavior of the explicit owner variants consumed by BuildManager."""

    @pytest.mark.asyncio
    async def test_o11_no_owned_adapter_runs_no_drain_operation(self) -> None:
        """O11: absence of a current capability starts no cleanup work."""
        outcome = await consume_prebuild_owner(NoOwnedAdapter())

        assert outcome.receipt is None
        assert outcome.allows_build is True
        require_prebuild_owner(outcome)

    @pytest.mark.asyncio
    async def test_owned_adapter_cleanup_returns_its_captured_receipt(self) -> None:
        """The handoff can consume only the callback bound to its owner ref."""
        owner = OwnedProcessRef("owner-a", "generation-a", 44001)
        receipt = OwnerDrainReceipt(
            owner=owner,
            status=DrainStatus.DRAINED,
            forced=False,
            root_returncode=0,
            active_processes=0,
        )
        drain = AsyncMock(return_value=receipt)

        outcome = await consume_prebuild_owner(OwnedAdapterCleanup(owner, drain))

        drain.assert_awaited_once_with(owner)
        assert outcome.receipt is receipt
        assert outcome.allows_build is True


class TestBuildSessionFileLockDetection:
    """Tests for ordinary build-lock recognition."""

    @pytest.mark.parametrize(
        "stdout,stderr",
        [
            ("error MSB3021: Unable to copy file", ""),
            ("error MSB3026: Could not copy", ""),
            ("error MSB3027: Cannot delete", ""),
            ("", "file is being used by another process"),
            ("The process cannot access the file because it is locked", ""),
        ],
    )
    def test_detects_lock_error(self, stdout: str, stderr: str) -> None:
        assert BuildSession(workspace_root=".")._is_file_lock_error(stdout, stderr)

    def test_returns_false_for_normal_error(self) -> None:
        assert not BuildSession(workspace_root=".")._is_file_lock_error(
            "error CS0246: The type or namespace name 'Foo' could not be found", ""
        )
