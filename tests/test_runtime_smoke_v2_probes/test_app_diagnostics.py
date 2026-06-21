from __future__ import annotations

import json
import os
from collections.abc import Callable
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
from netcoredbg_mcp.session.runtime_smoke_v2.actions import ActionContext
from netcoredbg_mcp.session.runtime_smoke_v2.probe_dispatcher import ProbeContext
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


def _app_manifest_source(probe: dict[str, Any]) -> dict[str, Any]:
    sources = probe["value"]["manifest"]["sources"]
    assert len(sources) == 1
    return sources[0]


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


class _WriteDiagnosticOnSleepClock:
    def __init__(
        self,
        path: Path,
        payload: dict[str, Any],
        *,
        mtime_ns: int,
    ) -> None:
        self._path = path
        self._payload = payload
        self._mtime_ns = mtime_ns
        self._now = 0.0
        self._written = False

    def __call__(self) -> float:
        return self._now

    @property
    def written(self) -> bool:
        return self._written

    def sleep_ms(self, idle_ms: int) -> None:
        if not self._written:
            self._path.write_text(json.dumps(self._payload), encoding="utf-8")
            os.utime(self._path, ns=(self._mtime_ns, self._mtime_ns))
            self._written = True
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
    def __init__(self, *, on_launch: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.launches: list[dict[str, Any]] = []
        self.on_launch = on_launch

    async def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.launches.append(dict(kwargs))
        if self.on_launch is not None:
            self.on_launch()
        return {"status": "PASS", "profile": kwargs.get("profile", "isolated")}


class CandidateValidationProbeSmokeSession(ProbeSmokeSession):
    def __init__(self, project_root: Path, rejected_path: Path) -> None:
        super().__init__()
        self.project_root = project_root.resolve()
        self.rejected_path = rejected_path.resolve()
        self.validated_paths: list[tuple[Path, bool]] = []

    def validate_path(self, path: str, must_exist: bool = False) -> str:
        resolved = Path(path).resolve()
        self.validated_paths.append((resolved, must_exist))
        if must_exist and resolved == self.rejected_path:
            raise ValueError("Path outside project scope")
        if self.project_root not in (resolved, *resolved.parents):
            raise ValueError("Path outside project scope")
        return str(resolved)


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


def _probe_context_with_app_diagnostics_progress(
    *,
    clock: Callable[[], float],
    progress_entries: list[dict[str, Any]],
    case_id: str,
    transition_index: int,
) -> ProbeContext:
    def publish_progress(entry: dict[str, Any]) -> None:
        progress_entries.append(dict(entry))

    return ProbeContext(
        action_context=ActionContext(
            service_adapters={},
            clock=clock,
            session=ProbeSmokeSession(),
            case_id=case_id,
            transition_index=transition_index,
            app_diagnostics_progress_notifier=publish_progress,
        )
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
async def test_app_diagnostics_launch_advertised_default_blocks_with_empty_directory(
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
    assert probe["value"]["poll"]["path"] == diagnostic_path.parent.as_posix()
    assert probe["value"]["poll"]["observed"] is False
    assert probe["requested"]["poll"]["path"] == diagnostic_path.parent.as_posix()


@pytest.mark.asyncio
async def test_app_diagnostics_launch_default_falls_back_to_directory_when_path_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_path = Path("runtime-smoke-diagnostics") / (
        "missing-launch-advertised-app-diagnostics.json"
    )
    fallback_path = diagnostic_path.parent / "newer-launch-advertised-app-diagnostics.json"
    fallback_path.parent.mkdir(parents=True)

    def write_fallback() -> None:
        fallback_path.write_text(
            json.dumps(
                _app_diagnostics(
                    app={"name": "DirectoryFallbackApp"},
                    status="PASS",
                    observations=[],
                )
            ),
            encoding="utf-8",
        )

    session = LaunchDiagnosticSmokeSession(on_launch=write_fallback)

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
    assert probe["value"]["app"]["name"] == "DirectoryFallbackApp"
    assert probe["value"]["poll"]["path"] == diagnostic_path.parent.as_posix()
    assert probe["value"]["poll"]["matched_path"] == str(fallback_path.resolve())
    assert probe["value"]["poll"]["observed"] is True


@pytest.mark.asyncio
async def test_app_diagnostics_launch_default_blocks_with_only_stale_directory_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_path = Path("runtime-smoke-diagnostics") / (
        "missing-launch-advertised-app-diagnostics.json"
    )
    stale_path = diagnostic_path.parent / "stale-launch-advertised-app-diagnostics.json"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "StaleDirectoryFallbackApp"},
                status="PASS",
                observations=[],
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
                app={"name": "LaunchAdvertisedApp"},
                status="PASS",
                observations=[],
            ),
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed after since cursor"
    assert probe["value"]["poll"]["path"] == diagnostic_path.parent.as_posix()
    assert probe["value"]["poll"]["observed"] is False
    assert probe["value"]["poll"]["since"] == {
        "mtime_ns": stale_path.stat().st_mtime_ns,
        "name": stale_path.name,
    }
    assert probe["requested"]["poll"]["path"] == diagnostic_path.parent.as_posix()


@pytest.mark.asyncio
async def test_app_diagnostics_launch_default_falls_back_to_directory_when_path_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_path = Path("runtime-smoke-diagnostics") / "launch-advertised-app-diagnostics.json"
    diagnostic_path.parent.mkdir(parents=True)
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "StaleLaunchAdvertisedApp"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    os.utime(diagnostic_path, ns=(1_000_000_000, 1_000_000_000))
    newer_path = diagnostic_path.parent / "newer-launch-advertised-app-diagnostics.json"
    def write_newer() -> None:
        newer_path.write_text(
            json.dumps(
                _app_diagnostics(
                    app={"name": "FreshLaunchAdvertisedApp"},
                    status="PASS",
                    observations=[],
                )
            ),
            encoding="utf-8",
        )
        os.utime(newer_path, ns=(2_000_000_000, 2_000_000_000))

    session = LaunchDiagnosticSmokeSession(on_launch=write_newer)

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
    assert probe["value"]["app"]["name"] == "FreshLaunchAdvertisedApp"
    assert probe["value"]["poll"]["path"] == diagnostic_path.parent.as_posix()
    assert probe["value"]["poll"]["matched_path"] == str(newer_path.resolve())
    assert probe["value"]["poll"]["since"] == {
        "mtime_ns": 1_000_000_000,
        "name": "launch-advertised-app-diagnostics.json",
    }
    assert probe["value"]["poll"]["cursor"] == {
        "mtime_ns": 2_000_000_000,
        "name": "newer-launch-advertised-app-diagnostics.json",
    }
    assert probe["value"]["poll"]["observed"] is True


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
async def test_app_diagnostics_explicit_poll_overrides_launch_directory_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launch_path = Path("runtime-smoke-diagnostics") / "launch-advertised-app-diagnostics.json"
    launch_fallback_path = launch_path.parent / "launch-fallback-app-diagnostics.json"
    explicit_dir = tmp_path / "explicit-diagnostics"
    explicit_path = explicit_dir / "explicit-app-diagnostics.json"
    launch_fallback_path.parent.mkdir(parents=True)
    explicit_dir.mkdir()
    launch_fallback_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LaunchFallbackApp"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    explicit_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "ExplicitPollApp"},
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
                poll={
                    "path": str(explicit_dir),
                    "pattern": "explicit-*.json",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            ),
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "ExplicitPollApp"
    assert probe["value"]["poll"]["path"] == str(explicit_dir)
    assert probe["value"]["poll"]["matched_path"] == str(explicit_path)
    assert "wait_json" not in probe["value"]
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
    manifest_source = _app_manifest_source(probe)
    assert manifest_source["id"] == "app_diagnostics.wait_json"
    assert manifest_source["kind"] == "app_diagnostics"
    assert manifest_source["status"] == "PASS"
    assert manifest_source["classification"] == "APP_DIAGNOSTICS_OBSERVED"
    assert manifest_source["artifact_path"] == diagnostic_path.name
    assert manifest_source["evidence_ref"] == "diagnostic:app_diagnostics:LiveWpfSmokeApp"


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_classifies_unreadable_manifest_source(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "unreadable-app-diagnostics.json"
    diagnostic_path.write_text("{not-json", encoding="utf-8")

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "UnreadableDiagnosticApp"},
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
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON is not readable"
    manifest_source = _app_manifest_source(probe)
    assert manifest_source["id"] == "app_diagnostics.wait_json"
    assert manifest_source["status"] == "BLOCKED"
    assert manifest_source["classification"] == "APP_DIAGNOSTICS_UNREADABLE"
    assert manifest_source["artifact_path"] == diagnostic_path.name
    assert manifest_source["reason"] == "diagnostic JSON is not readable"


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_waits_until_condition_matches(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-condition.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "cue selection not ready",
                        "value": {"activeCueIndex": 0},
                        "next_step": "Wait for the selected cue binding to settle.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    ready_payload = _app_diagnostics(
        app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
        status="PASS",
        observations=[
            {
                "kind": "app.snapshot",
                "status": "PASS",
                "reason": "cue selection settled",
                "value": {"activeCueIndex": 1},
                "next_step": "No action required.",
            }
        ],
    )
    clock = _RewriteJsonOnSleepClock(diagnostic_path, ready_payload)
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
                    "condition": {
                        "jsonpath": "$.observations[0].value.activeCueIndex",
                        "expected": 1,
                    },
                    "timeout_ms": 100,
                    "poll_interval_ms": 10,
                },
            )
        )
    )

    probe = before_probe(result)
    wait_json = probe["value"]["wait_json"]
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["observations"][0]["reason"] == "cue selection settled"
    assert wait_json["observed"] is True
    assert wait_json["polls"] == 2
    assert wait_json["condition"] == {
        "jsonpath": "$.observations[0].value.activeCueIndex",
        "expected": 1,
        "value": 1,
        "matched": True,
    }


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_publishes_live_source_progress_before_condition_matches(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-condition-progress.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "cue selection not ready",
                        "value": {"activeCueIndex": 0},
                        "next_step": "Wait for the selected cue binding to settle.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    ready_payload = _app_diagnostics(
        app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
        status="PASS",
        observations=[
            {
                "kind": "app.snapshot",
                "status": "PASS",
                "reason": "cue selection settled",
                "value": {"activeCueIndex": 1},
                "next_step": "No action required.",
            }
        ],
    )
    clock = _RewriteJsonOnSleepClock(diagnostic_path, ready_payload)
    session = ProbeSmokeSession()
    progress_entries: list[dict[str, Any]] = []
    smoke_runner = RuntimeSmokeRunner(
        session,
        service_adapters={"ui.invoke": session.invoke},
        clock=clock,
    )
    smoke_runner.attach_app_diagnostics_progress_notifier(progress_entries.append)

    result = await smoke_runner.run(
        one_probe_plan(
            _app_diagnostics(
                phase="before",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                wait_json={
                    "path": str(diagnostic_path),
                    "condition": {
                        "jsonpath": "$.observations[0].value.activeCueIndex",
                        "expected": 1,
                    },
                    "timeout_ms": 100,
                    "poll_interval_ms": 10,
                },
            )
        )
    )

    assert result["status"] == "PASS"
    assert progress_entries
    assert progress_entries[0]["case_id"] == "probe_case"
    assert progress_entries[0]["transition_index"] == 0
    assert progress_entries[0]["phase"] == "before"
    assert progress_entries[0]["probe"] == "app_diagnostics"
    assert progress_entries[0]["status"] == "RUNNING"
    assert progress_entries[0]["reason"] == "diagnostic JSON condition not satisfied"
    assert progress_entries[0]["progress"]["field"] == "wait_json"
    assert progress_entries[0]["progress"]["metadata"]["candidate_observed"] is True
    assert progress_entries[0]["progress"]["metadata"]["condition"]["matched"] is False


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_blocks_when_condition_times_out(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-condition-timeout.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "cue selection not ready",
                        "value": {"activeCueIndex": 0},
                        "next_step": "Wait for the selected cue binding to settle.",
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
                    "condition": {
                        "jsonpath": "$.observations[0].value.activeCueIndex",
                        "expected": 1,
                    },
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    wait_json = probe["value"]["wait_json"]
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON condition not satisfied"
    assert wait_json["observed"] is False
    assert wait_json["candidate_observed"] is True
    assert wait_json["condition"] == {
        "jsonpath": "$.observations[0].value.activeCueIndex",
        "expected": 1,
        "value": 0,
        "matched": False,
    }


@pytest.mark.asyncio
async def test_app_diagnostics_wait_json_condition_does_not_match_bool_to_int(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-condition-type.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "LiveWpfSmokeApp", "process_name": "dotnet"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "boolean readiness should not satisfy numeric condition",
                        "value": {"ready": True},
                        "next_step": "Wait for numeric readiness evidence.",
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
                    "condition": {
                        "jsonpath": "$.observations[0].value.ready",
                        "expected": 1,
                    },
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    wait_json = probe["value"]["wait_json"]
    assert result["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON condition not satisfied"
    assert wait_json["condition"] == {
        "jsonpath": "$.observations[0].value.ready",
        "expected": 1,
        "value": True,
        "matched": False,
    }


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
async def test_app_diagnostics_wait_json_preserves_declared_app_freshness(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "app-diagnostics-artifact-app.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "ArtifactReportedApp"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    session = _session_with_debug_freshness(process_id=9999)

    result = await runner(session).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "DeclaredApp", "process_id": 1234},
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
    assert probe["value"]["app"]["name"] == "ArtifactReportedApp"
    assert probe["value"]["app"]["process_id"] == 1234
    freshness = probe["value"]["freshness"]
    assert freshness["status"] == "FAIL"
    mismatch_kinds = {mismatch["kind"] for mismatch in freshness["mismatches"]}
    assert "process_id_mismatch" in mismatch_kinds


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
async def test_app_diagnostics_wait_json_publishes_progress_before_artifact_arrives(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "eventual-app-diagnostics-progress.json"
    progress_entries: list[dict[str, Any]] = []
    clock = _WriteDiagnosticOnSleepClock(
        diagnostic_path,
        _app_diagnostics(
            app={"name": "ProgressWpfSmokeApp", "process_name": "dotnet"},
            status="PASS",
            observations=[
                {
                    "kind": "artifact",
                    "status": "PASS",
                    "reason": "diagnostic artifact arrived after wait",
                    "next_step": "No action required.",
                }
            ],
        ),
        mtime_ns=2_000_000_000,
    )

    result = await handle_app_diagnostics(
        _app_diagnostics(
            phase="after",
            app={"name": "PlaceholderProgressApp", "process_name": "dotnet"},
            status="PASS",
            observations=[],
            wait_json={
                "path": str(diagnostic_path),
                "timeout_ms": 5,
                "poll_interval_ms": 1,
            },
        ),
        _probe_context_with_app_diagnostics_progress(
            clock=clock,
            progress_entries=progress_entries,
            case_id="case-wait-json-progress",
            transition_index=2,
        ),
        phase="after",
    )

    assert result["status"] == "PASS"
    assert result["value"]["app"]["name"] == "ProgressWpfSmokeApp"
    assert clock.written is True
    assert progress_entries, "expected wait_json progress before final acquisition"
    progress = progress_entries[0]
    assert progress["case_id"] == "case-wait-json-progress"
    assert progress["transition_index"] == 2
    assert progress["phase"] == "after"
    assert progress["probe"] == "app_diagnostics"
    assert progress["status"] == "RUNNING"
    assert progress["reason"] == "waiting for app_diagnostics.wait_json"
    assert progress["evidence_ref"] == "diagnostic:app_diagnostics:PlaceholderProgressApp"
    assert progress["progress"]["field"] == "wait_json"
    wait_json = progress["progress"]["metadata"]
    assert wait_json["path"] == str(diagnostic_path)
    assert wait_json["observed"] is False
    assert wait_json["polls"] == 1
    assert wait_json["timeout_ms"] == 5
    assert "matched_path" not in wait_json


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
    manifest_source = _app_manifest_source(probe)
    assert manifest_source["id"] == "app_diagnostics.poll"
    assert manifest_source["status"] == "BLOCKED"
    assert manifest_source["classification"] == "APP_DIAGNOSTICS_MISSING"
    assert manifest_source["artifact_path"] == missing_path.name
    assert manifest_source["reason"] == "diagnostic JSON not observed"


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
async def test_app_diagnostics_poll_reads_matching_json_from_directory(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-evidence"
    diagnostic_dir.mkdir()
    diagnostic_path = diagnostic_dir / "diagnostic-cue-change.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "NovaScript", "process_name": "NovaScript.Wpf"},
                status="PASS",
                observations=[
                    {
                        "kind": "app.snapshot",
                        "status": "PASS",
                        "reason": "NovaScript diagnostic snapshot observed",
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
                    "path": str(diagnostic_dir),
                    "pattern": "diagnostic-*.json",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "NovaScript"
    assert probe["value"]["poll"]["path"] == str(diagnostic_dir)
    assert probe["value"]["poll"]["pattern"] == "diagnostic-*.json"
    assert probe["value"]["poll"]["matched_path"] == str(diagnostic_path)
    assert probe["value"]["poll"]["observed"] is True


@pytest.mark.asyncio
async def test_app_diagnostics_poll_since_blocks_when_only_old_matching_json_exists(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-evidence"
    diagnostic_dir.mkdir()
    diagnostic_path = diagnostic_dir / "diagnostic-old.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "NovaScript", "process_name": "NovaScript.Wpf"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    os.utime(diagnostic_path, ns=(1_000_000_000, 1_000_000_000))

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(diagnostic_dir),
                    "pattern": "diagnostic-*.json",
                    "since": {
                        "mtime_ns": diagnostic_path.stat().st_mtime_ns,
                        "name": diagnostic_path.name,
                    },
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed after since cursor"
    assert probe["value"]["poll"]["observed"] is False
    assert "matched_path" not in probe["value"]["poll"]
    assert probe["value"]["poll"]["since"] == {
        "mtime_ns": 1_000_000_000,
        "name": "diagnostic-old.json",
    }
    manifest_source = _app_manifest_source(probe)
    assert manifest_source["id"] == "app_diagnostics.poll"
    assert manifest_source["status"] == "BLOCKED"
    assert manifest_source["classification"] == "APP_DIAGNOSTICS_STALE"
    assert "artifact_path" not in manifest_source
    assert manifest_source["reason"] == "diagnostic JSON not observed after since cursor"


@pytest.mark.asyncio
async def test_app_diagnostics_poll_since_uses_name_tiebreaker_for_equal_mtime(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-evidence"
    diagnostic_dir.mkdir()
    diagnostic_path = diagnostic_dir / "diagnostic-a.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "NovaScript", "process_name": "NovaScript.Wpf"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    os.utime(diagnostic_path, ns=(1_000_000_000, 1_000_000_000))

    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(diagnostic_dir),
                    "pattern": "diagnostic-*.json",
                    "since": {
                        "mtime_ns": diagnostic_path.stat().st_mtime_ns,
                        "name": "diagnostic-b.json",
                    },
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["reason"] == "diagnostic JSON not observed after since cursor"
    assert probe["value"]["poll"]["observed"] is False
    assert "matched_path" not in probe["value"]["poll"]


@pytest.mark.asyncio
async def test_app_diagnostics_poll_since_waits_for_new_matching_json_before_merging(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-evidence"
    diagnostic_dir.mkdir()
    old_path = diagnostic_dir / "diagnostic-old.json"
    old_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "StaleNovaScript"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    os.utime(old_path, ns=(1_000_000_000, 1_000_000_000))
    new_path = diagnostic_dir / "diagnostic-new.json"
    new_payload = _app_diagnostics(
        app={"name": "NovaScript", "process_name": "NovaScript.Wpf"},
        status="PASS",
        observations=[
            {
                "kind": "app.snapshot",
                "status": "PASS",
                "reason": "fresh diagnostic snapshot observed",
                "next_step": "No action required.",
            }
        ],
    )
    clock = _WriteDiagnosticOnSleepClock(
        new_path,
        new_payload,
        mtime_ns=2_000_000_000,
    )
    session = ProbeSmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={"ui.invoke": session.invoke},
        clock=clock,
    ).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(diagnostic_dir),
                    "pattern": "diagnostic-*.json",
                    "since": {
                        "mtime_ns": old_path.stat().st_mtime_ns,
                        "name": old_path.name,
                    },
                    "timeout_ms": 5,
                    "poll_interval_ms": 1,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["app"]["name"] == "NovaScript"
    assert probe["value"]["poll"]["matched_path"] == str(new_path)
    assert probe["value"]["poll"]["cursor"] == {
        "mtime_ns": 2_000_000_000,
        "name": "diagnostic-new.json",
    }
    assert probe["value"]["poll"]["observed"] is True
    assert probe["value"]["poll"]["polls"] >= 1
    assert clock.written is True


@pytest.mark.asyncio
async def test_app_diagnostics_poll_publishes_progress_before_artifact_arrives(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-progress-evidence"
    diagnostic_dir.mkdir()
    diagnostic_path = diagnostic_dir / "diagnostic-progress.json"
    progress_entries: list[dict[str, Any]] = []
    clock = _WriteDiagnosticOnSleepClock(
        diagnostic_path,
        _app_diagnostics(
            app={"name": "ProgressNovaScript", "process_name": "NovaScript.Wpf"},
            status="PASS",
            observations=[
                {
                    "kind": "app.snapshot",
                    "status": "PASS",
                    "reason": "fresh diagnostic snapshot observed after poll",
                    "next_step": "No action required.",
                }
            ],
        ),
        mtime_ns=3_000_000_000,
    )

    result = await handle_app_diagnostics(
        _app_diagnostics(
            phase="before",
            app={"name": "PlaceholderPollApp", "process_name": "NovaScript.Wpf"},
            status="PASS",
            observations=[],
            poll={
                "path": str(diagnostic_dir),
                "pattern": "diagnostic-*.json",
                "timeout_ms": 5,
                "poll_interval_ms": 1,
            },
        ),
        _probe_context_with_app_diagnostics_progress(
            clock=clock,
            progress_entries=progress_entries,
            case_id="case-poll-progress",
            transition_index=1,
        ),
        phase="before",
    )

    assert result["status"] == "PASS"
    assert result["value"]["app"]["name"] == "ProgressNovaScript"
    assert clock.written is True
    assert progress_entries, "expected poll progress before final acquisition"
    progress = progress_entries[0]
    assert progress["case_id"] == "case-poll-progress"
    assert progress["transition_index"] == 1
    assert progress["phase"] == "before"
    assert progress["probe"] == "app_diagnostics"
    assert progress["status"] == "RUNNING"
    assert progress["reason"] == "waiting for app_diagnostics.poll"
    assert progress["evidence_ref"] == "diagnostic:app_diagnostics:PlaceholderPollApp"
    assert progress["progress"]["field"] == "poll"
    poll = progress["progress"]["metadata"]
    assert poll["path"] == str(diagnostic_dir)
    assert poll["pattern"] == "diagnostic-*.json"
    assert poll["observed"] is False
    assert poll["polls"] == 1
    assert poll["timeout_ms"] == 5
    assert "matched_path" not in poll


@pytest.mark.asyncio
async def test_app_diagnostics_poll_revalidates_matched_directory_candidate(
    tmp_path: Path,
) -> None:
    diagnostic_dir = tmp_path / "novascript-evidence"
    diagnostic_dir.mkdir()
    diagnostic_path = diagnostic_dir / "diagnostic-cue-change.json"
    diagnostic_path.write_text(
        json.dumps(
            _app_diagnostics(
                app={"name": "NovaScript"},
                status="PASS",
                observations=[],
            )
        ),
        encoding="utf-8",
    )
    session = CandidateValidationProbeSmokeSession(
        project_root=tmp_path,
        rejected_path=diagnostic_path,
    )

    result = await runner(session).run(
        one_probe_plan(
            _app_diagnostics(
                phase="after",
                app={"name": "PlaceholderApp"},
                status="PASS",
                observations=[],
                poll={
                    "path": str(diagnostic_dir),
                    "pattern": "diagnostic-*.json",
                    "timeout_ms": 0,
                    "poll_interval_ms": 0,
                },
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "matched diagnostic JSON is outside allowed scope"
    assert probe["value"]["poll"]["matched_path"] == str(diagnostic_path.resolve())
    assert probe["value"]["poll"]["observed"] is False
    assert probe["value"]["poll"]["validation_error"] == "Path outside project scope"
    assert (diagnostic_dir.resolve(), False) in session.validated_paths
    assert (diagnostic_path.resolve(), True) in session.validated_paths


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
