from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
from netcoredbg_mcp.session.runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
    app_diagnostics_launch_contract,
)
from netcoredbg_mcp.session.runtime_smoke_v2.probes.app_diagnostics import (
    handle_app_diagnostics,
)
from netcoredbg_mcp.session.runtime_smoke_v2.runner import validate_v2_plan_contract
from netcoredbg_mcp.session.state import DebugState

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


class LaunchDiagnosticSmokeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.launches: list[dict[str, Any]] = []

    async def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.launches.append(dict(kwargs))
        return {"status": "PASS", "profile": kwargs.get("profile", "isolated")}


def _session_with_debug_freshness(
    *,
    process_id: int | None = 1234,
    process_name: str | None = "LiveWpfSmokeApp",
    project_path: str | None = None,
    modules: list[dict[str, Any]] | None = None,
    sources: dict[str, dict[str, Any]] | None = None,
) -> ProbeSmokeSession:
    session = ProbeSmokeSession()
    session.project_path = project_path
    session.state = SimpleNamespace(
        state=DebugState.RUNNING,
        output_buffer=[],
        process_id=process_id,
        process_name=process_name,
        modules=list(modules or []),
        loaded_sources=dict(sources or {}),
    )
    return session


def _launch_diagnostics_plan(
    diagnostic_path: Path,
    probe: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "diagnostics": {
            "app_diagnostics": {
                "diagnostic_launch": app_diagnostics_launch_contract(
                    evidence_dir=str(diagnostic_path.parent),
                    file_name=diagnostic_path.name,
                )
            }
        },
        "baseline": {
            "steps": [
                {
                    "id": "launch-with-diagnostics",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": "SmokeTestApp.dll",
                        "profile": "isolated",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "diagnostic_case",
                "transitions": [
                    {
                        "id": "read_app_diagnostics",
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "RefreshDiagnostics"},
                        },
                        "probes": [probe],
                    }
                ],
            }
        ],
    }


def _launch_diagnostics_runner(
    session: LaunchDiagnosticSmokeSession,
) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "launch": session.launch,
            "ui.invoke": session.invoke,
        },
    )


def test_validate_v2_plan_contract_accepts_app_diagnostics_probe_kind() -> None:
    validation = validate_v2_plan_contract(one_probe_plan(_app_diagnostics()))

    assert validation["validation_errors"] == []
    assert "app_diagnostics" in validation["accepted_probe_kinds"]


@pytest.mark.asyncio
async def test_app_diagnostics_defaults_to_launch_advertised_json_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_path = Path("runtime-smoke-diagnostics") / "launch-advertised-app-diagnostics.json"
    diagnostic_path.parent.mkdir(parents=True)
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LaunchAdvertisedApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "launch advertised diagnostic observed",
                        "next_step": "No action required.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    session = LaunchDiagnosticSmokeSession()

    result = await _launch_diagnostics_runner(session).run(
        _launch_diagnostics_plan(
            diagnostic_path,
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
            ),
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "LaunchAdvertisedApp"
    assert probe["value"]["wait_json"]["path"] == diagnostic_path.as_posix()
    assert probe["value"]["wait_json"]["observed"] is True
    assert result["diagnostic_launch"]["evidence"]["path"] == diagnostic_path.as_posix()
    assert session.launches[0]["env"]["NETCOREDBG_MCP_APP_DIAGNOSTICS_PATH"] == (
        diagnostic_path.as_posix()
    )


@pytest.mark.asyncio
async def test_app_diagnostics_launch_advertised_default_blocks_with_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_path = Path("runtime-smoke-diagnostics") / (
        "missing-launch-advertised-app-diagnostics.json"
    )
    session = LaunchDiagnosticSmokeSession()

    result = await _launch_diagnostics_runner(session).run(
        _launch_diagnostics_plan(
            diagnostic_path,
            _app_diagnostics(
                phase="after",
                app={"name": "LaunchAdvertisedApp"},
                status="PASS",
                observations=[],
            ),
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed"
    assert probe["value"]["wait_json"]["path"] == diagnostic_path.as_posix()
    assert probe["value"]["wait_json"]["observed"] is False
    assert probe["requested"]["wait_json"]["path"] == diagnostic_path.as_posix()


@pytest.mark.asyncio
async def test_app_diagnostics_explicit_wait_json_overrides_launch_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launch_path = Path("runtime-smoke-diagnostics") / "launch-advertised-app-diagnostics.json"
    explicit_path = tmp_path / "explicit-app-diagnostics.json"
    launch_path.parent.mkdir(parents=True)
    launch_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LaunchAdvertisedApp"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    explicit_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "ExplicitDiagnosticApp"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    session = LaunchDiagnosticSmokeSession()

    result = await _launch_diagnostics_runner(session).run(
        _launch_diagnostics_plan(
            launch_path,
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(explicit_path),
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            ),
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "ExplicitDiagnosticApp"
    assert probe["value"]["wait_json"]["path"] == str(explicit_path)
    assert result["diagnostic_launch"]["evidence"]["path"] == launch_path.as_posix()


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
async def test_app_diagnostics_wait_json_fails_pass_artifact_on_freshness_mismatch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    expected_module = workspace / "bin" / "Debug" / "LiveWpfSmokeApp.dll"
    other_module = tmp_path / "other" / "OtherApp.dll"
    expected_module.parent.mkdir(parents=True)
    other_module.parent.mkdir(parents=True)
    expected_module.write_text("expected module", encoding="utf-8")
    other_module.write_text("other module", encoding="utf-8")
    diagnostic_path = tmp_path / "app-diagnostics-freshness.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={
                    "name": "LiveWpfSmokeApp",
                    "process_id": 1234,
                    "process_name": "LiveWpfSmokeApp",
                    "expected_modules": [str(expected_module)],
                },
                status="PASS",
                observations=[
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "diagnostic artifact claims the app is healthy",
                        "next_step": "No action required.",
                    }
                ],
                workspace=str(workspace),
            )
        ),
        encoding="utf-8",
    )
    session = _session_with_debug_freshness(
        process_id=9999,
        process_name="OtherApp",
        project_path=str(workspace),
        modules=[{"name": "OtherApp.dll", "path": str(other_module)}],
    )

    result = await runner(session).run(
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
    assert result["status"] == "FAIL"
    assert result["reason"] == "app diagnostics freshness mismatch"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "app diagnostics freshness mismatch"
    assert probe["value"]["status"] == "FAIL"
    freshness = probe["value"]["freshness"]
    assert freshness["status"] == "FAIL"
    mismatch_kinds = {mismatch["kind"] for mismatch in freshness["mismatches"]}
    assert {
        "process_id_mismatch",
        "process_name_mismatch",
        "expected_module_missing",
    }.issubset(mismatch_kinds)
    assert session.runtime_smoke.freshness_evidence["latest"]["status"] == "FAIL"


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_keeps_pass_with_incomplete_freshness_warning(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    expected_module = workspace / "bin" / "Debug" / "LiveWpfSmokeApp.dll"
    expected_module.parent.mkdir(parents=True)
    expected_module.write_text("expected module", encoding="utf-8")
    diagnostic_path = tmp_path / "app-diagnostics-freshness-warn.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={
                    "name": "LiveWpfSmokeApp",
                    "process_id": 1234,
                    "expected_modules": [str(expected_module)],
                },
                status="PASS",
                observations=[
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "diagnostic artifact observed",
                        "next_step": "No action required.",
                    }
                ],
                workspace=str(workspace),
            )
        ),
        encoding="utf-8",
    )
    session = _session_with_debug_freshness(
        process_id=None,
        process_name=None,
        project_path=None,
        modules=[],
    )

    result = await runner(session).run(
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
    freshness = probe["value"]["freshness"]
    assert freshness["status"] == "WARN"
    assert freshness["mismatches"] == []
    warning_kinds = {warning["kind"] for warning in freshness["warnings"]}
    assert {
        "process_id_unavailable",
        "workspace_unavailable",
        "modules_unavailable",
    }.issubset(warning_kinds)


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_honors_loaded_sources_freshness(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    expected_source = workspace / "Program.cs"
    other_source = tmp_path / "other" / "Program.cs"
    expected_source.parent.mkdir(parents=True)
    other_source.parent.mkdir(parents=True)
    expected_source.write_text("class Expected {}", encoding="utf-8")
    other_source.write_text("class Other {}", encoding="utf-8")
    diagnostic_path = tmp_path / "app-diagnostics-loaded-sources.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp"},
                status="PASS",
                observations=[],
                workspace=str(workspace),
                loaded_sources=[str(expected_source)],
            )
        ),
        encoding="utf-8",
    )
    session = _session_with_debug_freshness(
        sources={
            str(other_source): {
                "name": "Program.cs",
                "path": str(other_source),
            }
        }
    )

    result = await runner(session).run(
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
    assert result["status"] == "FAIL"
    freshness = probe["value"]["freshness"]
    assert freshness["status"] == "FAIL"
    mismatch_kinds = {mismatch["kind"] for mismatch in freshness["mismatches"]}
    assert {
        "expected_source_missing",
        "source_workspace_mismatch",
    }.issubset(mismatch_kinds)


@pytest.mark.asyncio
async def test_app_diagnostics_declared_freshness_warns_when_session_unavailable() -> None:
    result = await handle_app_diagnostics(
        _app_diagnostics(
            status="PASS",
            observations=[],
            app={
                "name": "LiveWpfSmokeApp",
                "process_id": 1234,
                "process_name": "LiveWpfSmokeApp",
                "expected_modules": ["LiveWpfSmokeApp.dll"],
            },
        ),
        SimpleNamespace(action_context=SimpleNamespace(session=None)),
        phase="after",
    )

    assert result["status"] == "PASS"
    freshness = result["value"]["freshness"]
    assert freshness["status"] == "WARN"
    assert freshness["warnings"] == [
        {
            "kind": "session_unavailable",
            "reason": "app diagnostics freshness expectations declared but no session is available",
        }
    ]
    assert freshness["mismatches"] == []


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
