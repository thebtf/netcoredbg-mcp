"""Validate-only runtime-smoke plan facade tests."""

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class PlanFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            output_buffer=deque(),
        )
        self.process_registry = None
        self.project_path: str | None = None
        self.resolved_project_root = False
        self.validated_paths: list[str] = []
        self.validated_project_paths: list[str | None] = []
        self.path_error: Exception | None = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("validate-only facade must not launch")

    def validate_path(self, path: str) -> str:
        self.validated_paths.append(path)
        if self.path_error is not None:
            raise self.path_error
        return path

    def validate_path_for_project(self, path: str, project_path: str | None) -> str:
        self.validated_project_paths.append(project_path)
        return self.validate_path(path)


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("validate-only facade must not resolve project paths")


async def _resolve_project_root_ok(_ctx: Any, session: PlanFacadeSession) -> None:
    session.project_path = "D:\\project"
    session.resolved_project_root = True


async def _resolve_project_root_readonly_ok(
    _ctx: Any,
    session: PlanFacadeSession,
) -> str:
    session.resolved_project_root = True
    return "D:\\project"


def _register(
    capturing_mcp,
    session: PlanFacadeSession,
    *,
    check_session_access: Any | None = None,
    resolve_project_root: Any | None = None,
    resolve_project_root_readonly: Any | None = None,
) -> None:
    root_resolver = resolve_project_root or _resolve_project_root
    readonly_resolver = resolve_project_root_readonly
    if readonly_resolver is None:
        readonly_resolver = (
            _resolve_project_root_readonly_ok
            if root_resolver is _resolve_project_root_ok
            else root_resolver
        )
    register_runtime_smoke_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=check_session_access or (lambda ctx: None),
        resolve_project_root=root_resolver,
        resolve_project_root_readonly=readonly_resolver,
    )


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_does_not_claim_session_ownership(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()

    def fail_access_check(_ctx: Any) -> str | None:
        raise AssertionError("validate-only facade must not claim session ownership")

    _register(capturing_mcp, session, check_session_access=fail_access_check)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "validate-only",
            "cases": [{"id": "case-1", "transitions": []}],
        },
    )
    data = response["data"]

    assert "error" not in response
    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_invalid_v2_without_execution(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={"schema": "netcoredbg.runtime_smoke.v2", "cases": "nope"},
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["cases must be a list"]
    assert data["accepted_schema_values"] == [
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]
    assert "accepted_top_level_keys_v2" in data
    assert "accepted_action_kinds" in data
    assert "accepted_probe_kinds" in data
    assert session.launch_calls == 0
    assert "cleanup" not in data
    assert "completed_steps" not in data


@pytest.mark.parametrize(
    ("diagnostics", "expected_error"),
    [
        (
            "app diagnostics requested but malformed",
            "diagnostics must be an object",
        ),
        (
            {"app_diagnostics": []},
            "diagnostics.app_diagnostics must be an object",
        ),
        (
            {"app_diagnostics": {"diagnostic_launch": []}},
            "diagnostics.app_diagnostics.diagnostic_launch must be an object",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_malformed_diagnostics_contract(
    capturing_mcp,
    diagnostics: Any,
    expected_error: str,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "diagnostics": diagnostics,
            "cases": [{"id": "case-1", "transitions": []}],
        },
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == [expected_error]
    assert data["case_count"] == 1
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_malformed_v2_case_without_exception(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={"schema": "netcoredbg.runtime_smoke.v2", "cases": ["bad"]},
    )
    data = response["data"]

    assert "error" not in response
    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["cases[0] must be an object"]
    assert data["case_count"] == 0
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_runnable_v2_contract(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "validate-only",
            "cases": [{"id": "case-1", "transitions": []}],
        },
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert data["case_count"] == 1
    assert data["generated_case_count"] == 0
    assert data["validation_errors"] == []
    assert data["evidence_contract"]["result_keys"] == [
        "status",
        "reason",
        "elapsed_ms",
        "action_count",
        "cleanup",
        "evidence_refs",
        "compact",
        "debug_preflight",
    ]
    assert data["evidence_contract"]["diagnostics"]["schema"] == (
        "netcoredbg.runtime_smoke.diagnostics.v1"
    )
    assert data["evidence_contract"]["compact_limits"]["max_text_length"] == 240
    assert data["evidence_contract"]["compact_limits"]["max_list_items"] == 8
    assert session.launch_calls == 0
    assert "cleanup" not in data
    assert "completed_steps" not in data


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_accepts_json_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema": "netcoredbg.runtime_smoke.v2",
                "name": "validate-from-file",
                "cases": [{"id": "case-1", "transitions": []}],
            }
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_accepts_yaml_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.yaml"
    plan_path.write_text(
        "\n".join(
            [
                "schema: netcoredbg.runtime_smoke.v2",
                "name: validate-from-yaml",
                "cases:",
                "  - id: case-1",
                "    transitions: []",
            ]
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "yaml",
    }
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_path_does_not_claim_session_ownership(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()

    def fail_access_check(_ctx: Any) -> str | None:
        raise AssertionError("validate-only facade must not claim session ownership")

    _register(
        capturing_mcp,
        session,
        check_session_access=fail_access_check,
        resolve_project_root=_resolve_project_root_ok,
    )
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema": "netcoredbg.runtime_smoke.v2",
                "name": "validate-from-file",
                "cases": [{"id": "case-1", "transitions": []}],
            }
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx="ctx-token",
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert "error" not in response
    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_path_does_not_mutate_project_scope(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    session.project_path = "D:\\owner-project"

    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema": "netcoredbg.runtime_smoke.v2",
                "name": "validate-from-file",
                "cases": [{"id": "case-1", "transitions": []}],
            }
        ),
        encoding="utf-8",
    )

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert session.project_path == "D:\\owner-project"
    assert session.validated_project_paths == ["D:\\project"]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_mixed_plan_inputs(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()

    def fail_access_check(_ctx: Any) -> str | None:
        raise AssertionError("mixed validate inputs must not claim session ownership")

    _register(
        capturing_mcp,
        session,
        check_session_access=fail_access_check,
        resolve_project_root=_resolve_project_root_ok,
    )
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text('{"name": "from-file"}', encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={"name": "inline"},
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == [
        "provide exactly one runtime smoke plan input: plan or plan_path"
    ]
    assert data["accepted_input"] == {
        "plan": "inline JSON object",
        "plan_path": "path to a UTF-8 JSON or YAML object plan file",
    }
    assert session.validated_paths == []
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_malformed_json_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text("{not-json", encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"][0].startswith("plan_path JSON parse failed:")
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_malformed_yaml_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.yaml"
    plan_path.write_text("name: [unterminated", encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"][0].startswith("plan_path YAML parse failed:")
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "yaml",
    }
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_non_utf8_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_bytes(b'{"name": "\xff"}')

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"][0].startswith("plan_path UTF-8 decode failed:")
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_non_object_json_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text("[1, 2, 3]", encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["plan_path JSON root must be an object"]
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_non_object_yaml_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.yaml"
    plan_path.write_text("- one\n- two\n", encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["plan_path YAML root must be an object"]
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "yaml",
    }
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_plan_path_without_project_root(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()

    async def leave_project_root_unresolved(_ctx: Any, _session: PlanFacadeSession) -> None:
        session.resolved_project_root = True

    _register(capturing_mcp, session, resolve_project_root=leave_project_root_unresolved)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text('{"name": "unscoped-file"}', encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan_path=str(plan_path),
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == [
        "plan_path validation failed: project root is not resolved"
    ]
    assert data["plan_source"] == {
        "kind": "file",
        "path": str(plan_path),
        "format": "json",
    }
    assert session.validated_paths == []
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_unvalidated_plan_path(
    capturing_mcp,
    tmp_path,
) -> None:
    session = PlanFacadeSession()
    session.path_error = ValueError("outside project root")
    _register(capturing_mcp, session, resolve_project_root=_resolve_project_root_ok)
    plan_path = tmp_path / "runtime-smoke-plan.json"
    plan_path.write_text('{"name": "blocked-path"}', encoding="utf-8")

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
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
    assert session.resolved_project_root is True
    assert session.validated_paths == [str(plan_path)]
    assert session.launch_calls == 0
