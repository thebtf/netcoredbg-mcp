"""Session-owned runtime smoke state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .state import EvidenceRef

CleanupCallback = Callable[[], None]
CleanupFailure = dict[str, str]


@dataclass
class RuntimeSmokeSession:
    """Mutable runtime smoke state owned by one debug session."""
    instrumentation_groups: dict[str, Any] = field(default_factory=dict)
    output_checkpoints: dict[str, Any] = field(default_factory=dict)
    freshness_evidence: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    last_reset_failures: tuple[CleanupFailure, ...] = ()
    _cleanup_callbacks: dict[str, CleanupCallback] = field(default_factory=dict)

    def register_cleanup(self, name: str, callback: CleanupCallback) -> None:
        """Register an idempotent cleanup callback for session reset."""
        if not name:
            raise ValueError("cleanup name is required")
        self._cleanup_callbacks[name] = callback

    def reset(self) -> tuple[CleanupFailure, ...]:
        """Run cleanup callbacks and clear all runtime smoke state."""
        failures: list[CleanupFailure] = []
        for name, callback in list(self._cleanup_callbacks.items()):
            try:
                callback()
            except Exception as exc:
                failures.append({"name": name, "error": str(exc)})

        self.instrumentation_groups.clear()
        self.output_checkpoints.clear()
        self.freshness_evidence.clear()
        self.evidence_refs.clear()
        self._cleanup_callbacks.clear()
        self.last_reset_failures = tuple(failures)
        return self.last_reset_failures
