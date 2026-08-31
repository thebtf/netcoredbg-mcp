"""Explicit owner gate for pre-build work.

This module carries capability values and their validation only. It never
reconstructs authority from a process observation. The former enumeration-based
cleanup helpers were deleted in this cutover and must not be reintroduced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt
from .state import BuildError


@dataclass(frozen=True, slots=True)
class NoOwnedAdapter:
    """No current admitted adapter capability exists for this build.

    This variant is intentional. It allows ordinary restore and build work, but
    it carries no process observation from which an unrelated process can be
    selected or terminated.
    """


@dataclass(frozen=True, slots=True)
class OwnedAdapterCleanup:
    """One immutable handoff to drain exactly the captured adapter capability."""

    owner: OwnedProcessRef
    _drain: Callable[[OwnedProcessRef], Awaitable[OwnerDrainReceipt]]
    _receipt: OwnerDrainReceipt | None = None

    async def drain(self) -> OwnerDrainReceipt:
        """Ask the captured manager/client pair to validate and drain this owner."""
        if self._receipt is not None:
            return self._receipt
        return await self._drain(self.owner)

    def with_receipt(self, receipt: OwnerDrainReceipt) -> OwnedAdapterCleanup:
        """Bind an already-observed owner result through the later build gate."""
        if receipt.owner != self.owner:
            raise ValueError("pre-build receipt does not match the captured owner")
        return OwnedAdapterCleanup(owner=self.owner, _drain=self._drain, _receipt=receipt)


PreBuildOwner = NoOwnedAdapter | OwnedAdapterCleanup


@dataclass(frozen=True, slots=True)
class PreBuildOwnerOutcome:
    """The only owner-gate outcome that BuildManager may consume."""

    owner: PreBuildOwner
    receipt: OwnerDrainReceipt | None

    @property
    def allows_build(self) -> bool:
        """Whether this outcome proves the pre-build owner precondition."""
        if isinstance(self.owner, NoOwnedAdapter):
            return True
        return (
            self.receipt is not None
            and self.receipt.status is DrainStatus.DRAINED
            and self.receipt.active_processes == 0
        )


class PreBuildOwnerError(BuildError):
    """A captured adapter owner was stale or did not prove its tree drained."""

    def __init__(self, outcome: PreBuildOwnerOutcome) -> None:
        self.outcome = outcome
        receipt = outcome.receipt
        if receipt is None:
            detail = "no owner drain receipt"
        else:
            detail = f"status={receipt.status.value}, active={receipt.active_processes}"
        super().__init__(f"Pre-build adapter owner did not drain ({detail})")


async def consume_prebuild_owner(owner: PreBuildOwner) -> PreBuildOwnerOutcome:
    """Consume one explicit pre-build variant before any build command starts."""
    if isinstance(owner, NoOwnedAdapter):
        return PreBuildOwnerOutcome(owner=owner, receipt=None)
    return PreBuildOwnerOutcome(owner=owner, receipt=await owner.drain())


def require_prebuild_owner(outcome: PreBuildOwnerOutcome) -> None:
    """Fail closed unless the owner gate authorizes restore and build."""
    if outcome.allows_build:
        return
    # A generation or owner ID only fences stale callbacks. The retained owner
    # proves authority, and its zero-accounting receipt is the sole condition
    # that lets this boundary begin restore or build.
    raise PreBuildOwnerError(outcome)
