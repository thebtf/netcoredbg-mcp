"""Run-plan and evidence-bundle runtime-smoke facade tests."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class RunPlanFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="RunPlanFacade",
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None
        self.project_path: str | None = None
        self.resolved_project_root = False
        self.validated_paths: list[str] = []
        self.path_error: Exception | None = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("run-plan facade must not launch directly")

    def validate_path(self, path: str) -> str:
        self.validated_paths.append(path)
        if self.path_error is not None:
            raise self.path_error
        return path


class LargeFinalResultRegistry:
    async def get_result(self, run_id: str) -> dict[str, Any]:
        return {
            "status": "PASS",
            "reason": "runtime smoke v2 scenario passed",
            "run_id": run_id,
            "lifecycle_status": "COMPLETED",
            "final": True,
            "elapsed_ms": 12,
            "action_count": 9,
            "cleanup": {"status": "PASS", "attempted": [f"cleanup-{i}" for i in range(20)]},
            "evidence_refs": [
                {"kind": "case", "ref": f"case:{i}", "summary": "x" * 300}
                for i in range(12)
            ],
            "compact": {
                "status": "PASS",
                "reason": "runtime smoke v2 scenario passed",
                "elapsed_ms": 12,
                "action_count": 9,
                "generated_case_count": 0,
                "case_count": 12,
            },
            "cases": [{"id": f"case-{i}", "details": "x" * 500} for i in range(12)],
            "baseline": {"status": "PASS", "details": "x" * 500},
            "metrics_thresholds": {"max_ms": 100},
            "accepted_schema_values": ["netcoredbg.runtime_smoke.v1"],
            "accepted_top_level_keys_v2": ["cases"],
            "accepted_action_kinds": ["ui.text.assert"],
            "accepted_probe_kinds": ["ui.text"],
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "status": "COMPLETED",
            "run_id": run_id,
            "events": [{"cursor": 1, "kind": "completed", "details": "x" * 300}],
            "next_cursor": 1,
            "oldest_cursor": 1,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": True,
        }


class RenamedMissingRunRegistry:
    async def get_result(self, run_id: str) -> dict[str, Any]:
        return {
            "status": "FAIL",
            "reason": "no retained runtime smoke run",
            "run_id": run_id,
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        raise AssertionError("missing run should fail closed before tailing events")


class ActiveAppDiagnosticsBundleRegistry:
    async def get_result(self, run_id: str) -> dict[str, Any]:
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "plan_name": "active-app-diagnostics",
            "lifecycle_status": "RUNNING",
            "final": False,
            "evidence_refs": [],
            "cleanup": None,
            "app_diagnostics_history": [
                {
                    "case_id": "run_probe",
                    "transition_index": 0,
                    "phase": "after",
                    "probe": "app_diagnostics",
                    "status": "RUNNING",
                    "reason": "waiting for app_diagnostics.wait_json",
                    "evidence_ref": "diagnostic:app_diagnostics:WpfSmokeApp",
                }
            ],
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "events": [
                {
                    "cursor": 7,
                    "kind": "progress",
                    "status": "RUNNING",
                    "summary": "waiting for app diagnostics evidence",
                }
            ],
            "next_cursor": 7,
            "oldest_cursor": 7,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": False,
        }

    async def get_app_diagnostics_source_cursor(
        self,
        run_id: str,
    ) -> dict[str, int] | None:
        return {"after_index": 1, "entry_count": 1}


class FinalPackManifestStatusRegistry:
    def __init__(self) -> None:
        self._netcoredbg_mcp_pack_manifests = {
            "terminal-pack-run": {
                "pack_id": "terminal-oracle-pack",
                "status": "PASS",
                "manifest_ref": "pack-manifest.json",
            }
        }

    async def get_result(self, run_id: str) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "reason": "after-phase oracle blocked",
            "run_id": run_id,
            "plan_name": "terminal-pack-plan",
            "lifecycle_status": "COMPLETED",
            "final": True,
            "action_count": 1,
            "evidence_refs": [],
            "cleanup": {"status": "PASS"},
            "cases": [
                {
                    "id": "case-1",
                    "transitions": [
                        {
                            "action": {"kind": "ui.noop"},
                            "probes": {
                                "before": [
                                    {
                                        "kind": "oracle_pack",
                                        "id": "terminal-oracle-pack",
                                        "status": "PASS",
                                        "value": {"manifest": {"source_count": 1}},
                                    }
                                ],
                                "after": [
                                    {
                                        "kind": "oracle_pack",
                                        "id": "terminal-oracle-pack",
                                        "status": "BLOCKED",
                                        "value": {"manifest": {"source_count": 1}},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "reason": "after-phase oracle blocked",
            "run_id": run_id,
            "events": [{"cursor": 9, "kind": "blocked", "status": "BLOCKED"}],
            "next_cursor": 9,
            "oldest_cursor": 9,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": True,
        }


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("run-plan facade test plan must not resolve project paths")


async def _resolve_project_root_ok(_ctx: Any, session: RunPlanFacadeSession) -> None:
    session.project_path = "D:\\project"
    session.resolved_project_root = True


def _register(
    capturing_mcp,
    session: RunPlanFacadeSession,
    *,
    resolve_project_root: Any | None = None,
) -> list[Any]:
    access_calls: list[Any] = []

    def check_access(ctx: Any) -> None:
        access_calls.append(ctx)
        return None

    register_runtime_smoke_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=check_access,
        resolve_project_root=resolve_project_root or _resolve_project_root,
    )
    return access_calls


async def _wait_for_final_bundle(capturing_mcp, run_id: str) -> dict[str, Any]:
    for _ in range(20):
        response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
            ctx=None,
            run_id=run_id,
        )
        data = response["data"]
        if data.get("final"):
            return data
        await asyncio.sleep(0.01)
    raise AssertionError("runtime smoke run did not finish")


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_rejects_invalid_plan_without_starting_run(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={"name": "invalid", "actions": "not-a-list"},
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["actions must be a list"]
    assert "run_id" not in data
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_agent_mode_invalid_plan_fails_closed(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={"name": "invalid", "actions": "not-a-list"},
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_starts_durable_run_after_validation(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-run",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_id"]
    assert data["plan_name"] == "facade-run"
    assert data["final"] is False
    assert data["validation"]["can_run"] is True
    assert data["validation"]["validation_errors"] == []
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [data["run_id"]]
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_accepts_json_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(
        capturing_mcp,
        session,
        resolve_project_root=_resolve_project_root_ok,
    )
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "name": "facade-run-from-file",
                "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            }
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_id"]
    assert data["plan_name"] == "facade-run-from-file"
    assert data["validation"]["can_run"] is True
    assert data["validation"]["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [data["run_id"]]
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_accepts_yaml_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(
        capturing_mcp,
        session,
        resolve_project_root=_resolve_project_root_ok,
    )
    plan_path = tmp_path / "runtime-smoke-plan.yml"
    plan_path.write_text(
        "\n".join(
            [
                "name: facade-run-from-yaml",
                "actions:",
                "  - name: output_checkpoint",
                "    args:",
                "      name: start",
            ]
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_id"]
    assert data["plan_name"] == "facade-run-from-yaml"
    assert data["validation"]["can_run"] is True
    assert data["validation"]["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "yaml",
    }
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [data["run_id"]]
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_rejects_missing_plan_input(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](ctx=None)
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == [
        "provide exactly one runtime smoke plan input: plan or plan_path"
    ]
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.validated_paths == []
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_rejects_unvalidated_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = RunPlanFacadeSession()
    session.path_error = ValueError("outside project root")
    access_calls = _register(
        capturing_mcp,
        session,
        resolve_project_root=_resolve_project_root_ok,
    )
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text('{"name": "blocked-path"}', encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == [
        "plan_path validation failed: outside project root"
    ]
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_agent_mode_adds_cursor_guidance(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    default_response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-default-shape",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    assert "agent_mode" not in default_response["data"]

    await _wait_for_final_bundle(capturing_mcp, default_response["data"]["run_id"])
    agent_response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-agent-shape",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
        agent_mode=True,
    )
    data = agent_response["data"]

    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_evidence_bundle"
    assert data["agent_mode"]["event_cursor_tools"] == [
        "runtime_smoke_mark_event_cursor",
        "runtime_smoke_get_event_delta",
    ]
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_evidence_bundle",
        "arguments": {
            "run_id": data["run_id"],
            "agent_mode": True,
            "event_limit": 20,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_agent_mode_uses_active_run_for_blocked_start(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    first = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "already-active",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    blocked = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "blocked-agent-shape",
            "actions": [{"name": "output_checkpoint", "args": {"name": "second"}}],
        },
        agent_mode=True,
    )
    data = blocked["data"]

    assert data["status"] == "BLOCKED"
    assert data["active_run_id"] == first["data"]["run_id"]
    assert data["active_status"] == "RUNNING"
    assert data["run_created"] is False
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [first["data"]["run_id"]]
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_evidence_bundle",
        "arguments": {
            "run_id": first["data"]["run_id"],
            "agent_mode": True,
            "event_limit": 20,
        },
    }
    assert "runtime_smoke_evidence_bundle" in blocked["next_actions"]
    assert "runtime_smoke_wait_for_result" in blocked["next_actions"]
    assert "runtime_smoke_stop" in blocked["next_actions"]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_agent_mode_points_contamination_at_cleanup_contract(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)
    session.runtime_smoke.lifecycle_runs.mark_contaminated(
        reason="runtime smoke cleanup contract required",
        run_id="previous-run",
    )

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "blocked-by-contamination",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
        agent_mode=True,
    )
    data = response["data"]

    assert data["status"] == "BLOCKED"
    assert data["contaminated"] is True
    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_cleanup_contract"
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_cleanup_contract",
        "arguments": {},
    }
    assert "runtime_smoke_cleanup_contract" in response["next_actions"]
    assert "runtime_smoke_run_plan" not in response["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_returns_bounded_final_packet(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-bundle",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    run_id = started["data"]["run_id"]
    data = await _wait_for_final_bundle(capturing_mcp, run_id)

    assert data["status"] == "PASS"
    assert data["run_id"] == run_id
    assert data["final"] is True
    assert data["result"]["status"] == "PASS"
    assert data["result"]["action_count"] == 1
    assert data["cleanup"]["status"] == "PASS"
    assert data["evidence_refs"] == [
        {
            "kind": "output_checkpoint",
            "ref": "output:start",
            "summary": "output checkpoint created",
        }
    ]
    assert [event["kind"] for event in data["events"]] == ["started", "completed"]
    assert data["event_cursor"]["next_cursor"] >= 2
    assert data["event_cursor"]["oldest_cursor"] >= 1
    assert data["event_cursor"]["dropped_count"] == 0
    assert data["event_cursor"]["stale_cursor"] is False
    assert "runtime_smoke_evidence_bundle" in data["next_actions"]
    assert "runtime_smoke_run_plan" in data["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_exposes_named_pack_manifest_ref(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "run-plan-named-oracle-pack",
            "cases": [
                {
                    "id": "case-1",
                    "transitions": [
                        {
                            "id": "read-oracle-pack",
                            "action": {"kind": "ui.noop"},
                            "probes": [
                                {
                                    "kind": "oracle_pack",
                                    "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
                                    "id": "run-plan-oracle-pack",
                                    "status": "PASS",
                                    "checks": [
                                        {
                                            "id": "visible-row-count",
                                            "probe": "ui.grid",
                                            "expect": {"min_rows": 1},
                                            "on_blocked": {
                                                "next_step": "Run WPF fixture replay."
                                            },
                                        }
                                    ],
                                    "limits": {
                                        "max_text_length": 240,
                                        "max_list_items": 8,
                                        "max_json_bytes": 32768,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    data = started["data"]

    assert data["status"] == "RUNNING"
    bundle = await _wait_for_final_bundle(capturing_mcp, data["run_id"])
    assert bundle["status"] == "PASS"
    assert bundle["result"]["status"] == "PASS"
    assert bundle["evidence_refs"] == [
        {
            "case_id": "case-1",
            "phase": "before",
            "probe": "oracle_pack",
            "evidence_ref": "diagnostic:oracle_pack:run-plan-oracle-pack",
        },
        {
            "case_id": "case-1",
            "phase": "after",
            "probe": "oracle_pack",
            "evidence_ref": "diagnostic:oracle_pack:run-plan-oracle-pack",
        }
    ]
    assert bundle["pack_manifest"] == {
        "pack_id": "run-plan-oracle-pack",
        "status": "PASS",
        "manifest_ref": "pack-manifest.json",
        "materialized": False,
    }
    assert bundle["result"]["pack_manifest"] == bundle["pack_manifest"]


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_uses_terminal_pack_manifest_status(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    session.runtime_smoke.lifecycle_runs = FinalPackManifestStatusRegistry()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="terminal-pack-run",
    )
    data = response["data"]

    assert data["status"] == "BLOCKED"
    assert data["pack_manifest"] == {
        "pack_id": "terminal-oracle-pack",
        "status": "BLOCKED",
        "manifest_ref": "pack-manifest.json",
        "materialized": False,
    }
    assert data["result"]["pack_manifest"] == data["pack_manifest"]


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_sanitizes_caller_supplied_diagnostic_launch(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "caller-supplied-diagnostic-launch",
            "diagnostics": {
                "app_diagnostics": {
                    "diagnostic_launch": {
                        "kind": "app_diagnostics",
                        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
                        "env_var_names": {
                            "directory": "LEAKED_DIRECTORY_ENV",
                            "path": "LEAKED_PATH_ENV",
                            "schema": "LEAKED_SCHEMA_ENV",
                            "token": "short-secret",
                        },
                        "evidence": {
                            "directory": r"work\runtime-smoke-diagnostics",
                            "path": r"work\runtime-smoke-diagnostics\app-diagnostics.json",
                            "token": "short-secret",
                        },
                        "redacted_env_values": False,
                        "env": {"TOKEN": "short-secret"},
                        "env_values": {"TOKEN": "short-secret"},
                        "password": "short-secret",
                    }
                }
            },
            "cases": [{"id": "case-1", "transitions": []}],
        },
    )
    bundle = await _wait_for_final_bundle(capturing_mcp, started["data"]["run_id"])

    diagnostic_launch = bundle["diagnostic_launch"]
    assert set(diagnostic_launch) == {
        "kind",
        "schema",
        "env_var_names",
        "evidence",
        "redacted_env_values",
    }
    assert diagnostic_launch["kind"] == "app_diagnostics"
    assert diagnostic_launch["schema"] == "netcoredbg.runtime_smoke.diagnostics.v1"
    assert diagnostic_launch["env_var_names"] == {
        "directory": "NETCOREDBG_MCP_APP_DIAGNOSTICS_DIR",
        "path": "NETCOREDBG_MCP_APP_DIAGNOSTICS_PATH",
        "schema": "NETCOREDBG_MCP_APP_DIAGNOSTICS_SCHEMA",
    }
    assert diagnostic_launch["evidence"] == {
        "directory": "work/runtime-smoke-diagnostics",
        "path": "work/runtime-smoke-diagnostics/app-diagnostics.json",
    }
    assert diagnostic_launch["redacted_env_values"] is True
    assert bundle["result"]["diagnostic_launch"] == diagnostic_launch
    serialized_bundle = json.dumps(bundle, sort_keys=True)
    assert "short-secret" not in serialized_bundle
    assert "LEAKED_DIRECTORY_ENV" not in serialized_bundle
    assert "LEAKED_PATH_ENV" not in serialized_bundle
    assert "LEAKED_SCHEMA_ENV" not in serialized_bundle
    assert '"env"' not in serialized_bundle
    assert '"env_values"' not in serialized_bundle
    assert '"password"' not in serialized_bundle
    assert '"token"' not in serialized_bundle


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_agent_mode_adds_delta_guidance(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-agent-bundle",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    run_id = started["data"]["run_id"]
    data = await _wait_for_final_bundle(capturing_mcp, run_id)
    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id=run_id,
        after_cursor=data["event_cursor"]["next_cursor"],
        agent_mode=True,
    )
    agent = response["data"]["agent_mode"]

    assert agent["primary_next_action"] == "runtime_smoke_get_event_delta"
    assert agent["cursor"]["run_id"] == run_id
    assert agent["cursor"]["after_cursor"] == response["data"]["event_cursor"]["next_cursor"]
    assert agent["next_request"] == {
        "tool": "runtime_smoke_get_event_delta",
        "arguments": {
            "cursor": agent["cursor"],
            "agent_mode": True,
            "event_limit": 20,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_agent_mode_guides_active_app_diagnostics_delta(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    session.runtime_smoke.lifecycle_runs = ActiveAppDiagnosticsBundleRegistry()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="active-appdiag-run",
        after_cursor=0,
        event_limit=5,
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "RUNNING"
    assert data["final"] is False
    assert agent["primary_next_action"] == "runtime_smoke_get_event_delta"
    assert agent["cursor"]["run_id"] == "active-appdiag-run"
    assert agent["cursor"]["after_cursor"] == data["event_cursor"]["next_cursor"]
    assert agent["cursor"]["sources"]["app_diagnostics"] == {
        "after_index": 0,
        "entry_count": 1,
    }
    assert agent["next_request"] == {
        "tool": "runtime_smoke_get_event_delta",
        "arguments": {
            "cursor": agent["cursor"],
            "agent_mode": True,
            "event_limit": 20,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_bounds_large_final_result(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    session.runtime_smoke.lifecycle_runs = LargeFinalResultRegistry()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="large-v2-run",
    )
    result = response["data"]["result"]

    assert result["status"] == "PASS"
    assert result["action_count"] == 9
    assert response["data"]["events"][0]["details_length"] == 300
    assert response["data"]["events"][0]["omitted_fields"] == ["details"]
    assert response["data"]["cleanup"]["attempted"][-1] == {"omitted_count": 12}
    assert response["data"]["evidence_refs"][-1] == {"omitted_count": 4}
    assert result["cleanup"]["attempted"][-1] == {"omitted_count": 12}
    assert result["evidence_refs"][-1] == {"omitted_count": 4}
    assert "compact" in result
    assert "cases" not in result
    assert "baseline" not in result
    assert "debug_preflight" not in result
    assert "metrics_thresholds" not in result
    assert "accepted_schema_values" not in result
    assert "accepted_top_level_keys_v2" not in result
    assert "accepted_action_kinds" not in result
    assert "accepted_probe_kinds" not in result


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_fails_closed_without_literal_reason_match(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    session.runtime_smoke.lifecycle_runs = RenamedMissingRunRegistry()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="missing-run",
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "no retained runtime smoke run"
    assert data["final"] is True
    assert data["events"] == []
    assert data["result"] is None
    assert data["next_actions"] == ["runtime_smoke_run_plan"]


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_fails_closed_for_missing_run_id(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="missing-run",
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "runtime smoke run not found"
    assert data["run_id"] == "missing-run"
    assert data["final"] is True
    assert data["events"] == []
    assert data["result"] is None
    assert data["next_actions"] == ["runtime_smoke_run_plan"]


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_agent_mode_missing_run_has_no_delta_request(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="missing-run",
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "FAIL"
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent
