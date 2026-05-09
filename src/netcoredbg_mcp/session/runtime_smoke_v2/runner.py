from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..runtime_smoke_schema import (
    ACCEPTED_SCHEMA_VALUES,
    ACCEPTED_TOP_LEVEL_KEYS_V2,
)
from .result_envelope import finalize_result


def compact_v2_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "generated_case_count": result.get("generated_case_count", 0),
        "case_count": len(result.get("cases", [])),
        "cleanup": result.get("cleanup", {}),
    }


class RuntimeStateOracleRunner:
    def __init__(
        self,
        session: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._clock = clock

    async def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        started = self._clock()
        cleanup = {"status": "PASS", "attempted": [], "failures": []}
        return finalize_result(
            status="BLOCKED",
            reason="runtime smoke v2 state-oracle execution is not available yet",
            elapsed_ms=int(max(0.0, self._clock() - started) * 1000),
            action_count=0,
            completed_steps=[],
            failed_assertions=[],
            cleanup=cleanup,
            evidence_refs=[],
            compact_builder=compact_v2_result,
            extra={
                "generated_case_count": 0,
                "cases": [],
                "baseline": None,
                "metrics_thresholds": plan.get("metrics_thresholds"),
                "accepted_schema_values": list(ACCEPTED_SCHEMA_VALUES),
                "accepted_top_level_keys_v2": list(ACCEPTED_TOP_LEVEL_KEYS_V2),
                "blocked": {
                    "reason": "v2 runner baseline only",
                    "requested": "runtime_smoke_v2_cases",
                    "accepted": ["schema", "baseline", "generate", "cases"],
                    "next_step": "complete Phase 2 route-sensitive actions",
                },
            },
        )
