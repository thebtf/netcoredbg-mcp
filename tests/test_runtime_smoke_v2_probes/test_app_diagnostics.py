from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
from netcoredbg_mcp.session.runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
)
from netcoredbg_mcp.session.runtime_smoke_v2.runner import validate_v2_plan_contract

from .helpers import ProbeSmokeSession, after_probe, before_probe, one_probe_plan, runner


def _limits() -> dict[str, int]:
    return dict(DIAGNOSTIC_EVIDENCE_LIMITS)


def _app_diagnostics(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "app_diagnostics",
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "app": {
            "name": "WpfSmokeApp",
            "process_name": "dotnet",
            "expected_modules": ["WpfSmokeApp.dll"],
        },
        "status": "BLOCKED",
        "observations": [
            {
                "kind": "ui.backend",
                "status": "BLOCKED",
                "reason": "GridPattern unavailable",
                "requested": {"control_type": "DataGrid"},
                "accepted": {"fallback": "bounded descendant text"},
                "next_step": "Run the WPF fixture replay on a GUI worker.",
            }
        ],
        "redaction": {"omit_fields": ["raw_tree", "screenshot_base64", "secret"]},
        "limits": _limits(),
    }
    payload.update(overrides)
    return payload


class _RewriteJsonOnSleepClock:
    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self._path = path
        self._payload = payload
        self._now = 0.0
        self._rewritten = False

    def __call__(self) -> float:
        return self._now

    def sleep_ms(self, idle_ms: int) -> None:
        if not self._rewritten:
            self._path.write_text(json.dumps(self._payload), encoding="utf-8")
            self._rewritten = True
        self._now += max(1, idle_ms) / 1000


class _RecordingAdvancingClock:
    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[int] = []

    def __call__(self) -> float:
        return self._now

    def sleep_ms(self, idle_ms: int) -> None:
        self.sleeps.append(idle_ms)
        self._now += max(1, idle_ms) / 1000


def test_validate_v2_plan_contract_accepts_app_diagnostics_probe_kind() -> None:
    validation = validate_v2_plan_contract(one_probe_plan(_app_diagnostics()))

    assert validation["validation_errors"] == []
    assert "app_diagnostics" in validation["accepted_probe_kinds"]


@pytest.mark.asyncio
async def test_app_diagnostics_probe_returns_bounded_blocked_observations() -> None:
    result = await runner(ProbeSmokeSession()).run(one_probe_plan(_app_diagnostics()))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["kind"] == "app_diagnostics"
    assert probe["reason"] == "GridPattern unavailable"
    assert probe["value"]["schema"] == DIAGNOSTIC_SCHEMA_VERSION
    assert probe["value"]["app"]["name"] == "WpfSmokeApp"
    assert probe["value"]["observation_count"] == 1
    observation = probe["value"]["observations"][0]
    assert observation["reason"] == "GridPattern unavailable"
    assert observation["requested"] == {"control_type": "DataGrid"}
    assert observation["accepted"] == {"fallback": "bounded descendant text"}
    assert observation["next_step"].startswith("Run the WPF fixture replay")
    assert "raw_tree" not in str(probe["value"])
    assert "screenshot_base64" not in str(probe["value"])
    assert "secret" not in str(probe["value"])


@pytest.mark.asyncio
async def test_app_diagnostics_probe_applies_probe_specific_evidence_limits() -> None:
    long_reason = "backend diagnostic text exceeds strict limit"
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                observations=[
                    {
                        "kind": "ui.backend",
                        "status": "BLOCKED",
                        "reason": long_reason,
                        "requested": {"control_type": "DataGrid"},
                        "accepted": {"fallback": "bounded descendant text"},
                        "next_step": "Inspect the bounded evidence.",
                    },
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "second observation",
                        "next_step": "No action required.",
                    },
                ],
                limits={
                    **_limits(),
                    "max_text_length": 12,
                    "max_list_items": 1,
                },
            )
        )
    )

    observations = after_probe(result)["value"]["observations"]
    assert "reason" not in observations[0]
    assert observations[0]["reason_length"] == len(long_reason)
    assert observations[0]["next_step_length"] == len("Inspect the bounded evidence.")
    assert observations[0]["omitted_fields"] == ["reason", "next_step"]
    assert observations[1] == {"omitted_count": 1}


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_reads_live_diagnostic_artifact(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "diagnostic artifact observed",
                        "next_step": "No action required.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(diagnostic_path),
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "LiveWpfSmokeApp"
    assert probe["value"]["observation_count"] == 1
    assert probe["value"]["observations"][0]["reason"] == "diagnostic artifact observed"
    assert probe["value"]["wait_json"]["observed"] is True
    assert probe["value"]["wait_json"]["polls"] >= 1
    assert probe["evidence_ref"] == "diagnostic:app_diagnostics:LiveWpfSmokeApp"


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_times_out_with_poll_next_step(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-app-diagnostics.json"

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "WpfSmokeApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(missing_path),
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed"
    assert probe["next_step"] == (
        "Retry app_diagnostics.poll or app_diagnostics.wait_json after the app "
        "writes the diagnostic artifact."
    )
    assert probe["value"]["wait_json"]["path"] == str(missing_path)
    assert probe["value"]["wait_json"]["observed"] is False
    assert probe["value"]["wait_json"]["polls"] == 1
    assert probe["value"]["wait_json"]["timeout_ms"] == 0
    assert probe["value"]["wait_json"]["reason"] == "diagnostic JSON not observed"


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_clamps_zero_poll_interval_for_positive_timeout(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-app-diagnostics-clamped.json"
    clock = _RecordingAdvancingClock()
    session = ProbeSmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={"ui.invoke": session.invoke},
        clock=clock,
    ).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "WpfSmokeApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(missing_path),
                    "timeout_ms": 3,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["value"]["wait_json"]["polls"] > 1
    assert clock.sleeps
    assert all(idle_ms > 0 for idle_ms in clock.sleeps)


@pytest.mark.asyncio
async def test_app_diagnostics_poll_times_out_with_poll_metadata(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-app-diagnostics-poll.json"

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "WpfSmokeApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(missing_path),
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed"
    assert probe["value"]["poll"]["path"] == str(missing_path)
    assert probe["value"]["poll"]["observed"] is False
    assert probe["value"]["poll"]["polls"] == 1
    assert probe["value"]["poll"]["timeout_ms"] == 0
    assert probe["value"]["poll"]["reason"] == "diagnostic JSON not observed"


@pytest.mark.asyncio
async def test_app_diagnostics_poll_reads_live_diagnostic_artifact(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-poll.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "PolledWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "diagnostic artifact polled",
                        "next_step": "No action required.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(diagnostic_path),
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "PolledWpfSmokeApp"
    assert probe["value"]["observation_count"] == 1
    assert probe["value"]["observations"][0]["reason"] == "diagnostic artifact polled"
    assert probe["value"]["poll"]["observed"] is True
    assert probe["value"]["poll"]["polls"] >= 1


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_invalid_source_fails_schema_before_poll() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                wait_json={
                    "path": "",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert "app_diagnostics.wait_json.path is required" in result["validation_errors"]
    assert result["cases"] == []


@pytest.mark.asyncio
async def test_app_diagnostics_poll_invalid_source_fails_schema_before_poll() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                poll={
                    "path": "",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert "app_diagnostics.poll.path is required" in result["validation_errors"]
    assert result["cases"] == []


@pytest.mark.asyncio
async def test_app_diagnostics_rejects_wait_json_and_poll_together() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                wait_json={
                    "path": "app-diagnostics.json",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
                poll={
                    "path": "app-diagnostics.json",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert (
        "app_diagnostics.wait_json and app_diagnostics.poll are mutually exclusive"
        in result["validation_errors"]
    )
    assert result["cases"] == []


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_clears_stale_error_after_success(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "eventual-app-diagnostics.json"
    diagnostic_path.write_text("{not-json", encoding="utf-8")
    payload = _app_diagnostics(
        app={"name": "RecoveredWpfSmokeApp", "process_name": "dotnet"},
        status="PASS",
        observations=[
            {
                "kind": "artifact",
                "status": "PASS",
                "reason": "diagnostic artifact recovered",
                "next_step": "No action required.",
            }
        ],
    )
    clock = _RewriteJsonOnSleepClock(diagnostic_path, payload)
    session = ProbeSmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={"ui.invoke": session.invoke},
        clock=clock,
    ).run(
        one_probe_plan(
            _app_diagnostics(
                phase="before",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(diagnostic_path),
                    "timeout_ms": 100,
                    "poll_interval_ms": 10,
                },
            )
        )
    )

    wait_json = before_probe(result)["value"]["wait_json"]
    assert result["status"] == "PASS"
    assert wait_json["observed"] is True
    assert wait_json["polls"] == 2
    assert "reason" not in wait_json
    assert "error" not in wait_json


@pytest.mark.asyncio
async def test_app_diagnostics_probe_blocks_invalid_unsafe_evidence() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                observations=[
                    {
                        "kind": "ui.backend",
                        "status": "BLOCKED",
                        "reason": "Tree dump leaked",
                        "requested": {"raw_tree": {"name": "Window"}},
                        "accepted": {"fallback": "bounded descendant text"},
                        "next_step": "Use summarized UI evidence.",
                    }
                ]
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert "app_diagnostics.observations[0].requested.raw_tree must be omitted" in (
        "\n".join(result["validation_errors"])
    )
    assert result["cases"] == []
