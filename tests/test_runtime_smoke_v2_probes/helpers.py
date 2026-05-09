from __future__ import annotations

from collections.abc import Callable
from typing import Any

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class ProbeSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("ui.invoke", dict(selector)))
        return {"status": "PASS", "invoked": True}


def runner(
    session: ProbeSmokeSession,
    adapters: dict[str, Callable[..., Any]] | None = None,
) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            **dict(adapters or {}),
        },
    )


def one_probe_plan(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "probe_case",
                "transitions": [
                    {
                        "id": "probe_transition",
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "ToggleSetting"},
                        },
                        "probes": [probe],
                    }
                ],
            }
        ],
    }


def after_probe(result: dict[str, Any]) -> dict[str, Any]:
    return result["cases"][0]["transitions"][0]["probes"]["after"][0]


def before_probe(result: dict[str, Any]) -> dict[str, Any]:
    return result["cases"][0]["transitions"][0]["probes"]["before"][0]
