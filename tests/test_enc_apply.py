from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from netcoredbg_mcp.enc.applier import ApplyDeltasResult, apply_deltas
from netcoredbg_mcp.enc.compiler import DeltaResult
from netcoredbg_mcp.session.state import DebugState, ModuleInfo, SessionState
from netcoredbg_mcp.tools.enc import apply_code_change_to_session


@pytest.mark.asyncio
async def test_apply_deltas_sends_custom_dap_request() -> None:
    client = FakeDapClient(success=True)

    result = await apply_deltas(
        client,
        dll_name="Sample.dll",
        metadata_path="Sample.metadata",
        il_path="Sample.il",
        pdb_path="Sample.pdb",
        line_updates_path=None,
    )

    assert result.success is True
    assert client.requests == [
        (
            "applyDeltas",
            {
                "dllFileName": "Sample.dll",
                "metadataPath": "Sample.metadata",
                "ilPath": "Sample.il",
                "pdbPath": "Sample.pdb",
            },
            30.0,
        )
    ]


@pytest.mark.asyncio
async def test_apply_deltas_handles_empty_response_body() -> None:
    client = FakeDapClient(success=True, empty_body=True)

    result = await apply_deltas(
        client,
        dll_name="Sample.dll",
        metadata_path="Sample.metadata",
        il_path="Sample.il",
        pdb_path="Sample.pdb",
        line_updates_path=None,
    )

    assert result.success is True
    assert result.body == {}


@pytest.mark.asyncio
async def test_apply_code_change_stopped_success_updates_source_and_restores_state(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]
    applied: dict[str, object] = {}
    compiled: dict[str, object] = {}

    async def fake_apply(*args, **_kwargs):
        applied["dll_name"] = _kwargs["dll_name"]
        applied["line_updates_path"] = _kwargs["line_updates_path"]
        session.state_during_apply = session.state.state
        return ApplyDeltasResult(success=True, message=None, body={"ok": True})

    def fake_compiler(**kwargs):
        compiled.update(kwargs)
        return DeltaResult(
            True,
            str(tmp_path / "Sample.il"),
            str(tmp_path / "Sample.metadata"),
            str(tmp_path / "Sample.pdb"),
            (),
            (),
        )

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=fake_compiler,
        apply=fake_apply,
    )

    assert result["data"]["success"] is True
    assert compiled["module_path"] == str(tmp_path / "bin" / "Debug" / "Sample.dll")
    assert applied["dll_name"] == "Sample.dll"
    assert Path(applied["line_updates_path"]).read_bytes() == b"\x00\x00\x00\x00"
    assert "return 2;" in source.read_text(encoding="utf-8")
    assert session.state_during_apply is DebugState.APPLYING_CHANGES
    assert session.transitions == [DebugState.APPLYING_CHANGES, DebugState.STOPPED]


@pytest.mark.asyncio
async def test_apply_code_change_rejects_line_changing_edit_before_compile_or_apply(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    original_source = source.read_text(encoding="utf-8")
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    compiled = False
    applied = False

    def fake_compiler(**_kwargs):
        nonlocal compiled
        compiled = True
        return DeltaResult(True, "Sample.il", "Sample.metadata", "Sample.pdb", (), ())

    async def fake_apply(*_args, **_kwargs):
        nonlocal applied
        applied = True
        return ApplyDeltasResult(success=True, message=None, body={})

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [
            {
                "start_line": 7,
                "end_line": 7,
                "new_text": "        var value = 2;\n        return value;",
            }
        ],
        compiler=fake_compiler,
        apply=fake_apply,
    )

    assert "Line-changing edits are not supported" in result["error"]
    assert compiled is False
    assert applied is False
    assert source.read_text(encoding="utf-8") == original_source
    assert session.transitions == []


@pytest.mark.asyncio
async def test_apply_code_change_restores_source_when_apply_deltas_fails(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    original_source = source.read_text(encoding="utf-8")
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]

    async def fake_apply(*_args, **_kwargs):
        return ApplyDeltasResult(success=False, message="apply failed", body={})

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: DeltaResult(
            True,
            str(tmp_path / "Sample.il"),
            str(tmp_path / "Sample.metadata"),
            str(tmp_path / "Sample.pdb"),
            (),
            (),
        ),
        apply=fake_apply,
    )

    assert "apply failed" in result["error"]
    assert result["state"] == DebugState.STOPPED.value
    assert source.read_text(encoding="utf-8") == original_source
    assert session.transitions == [DebugState.APPLYING_CHANGES, DebugState.STOPPED]


@pytest.mark.asyncio
async def test_apply_code_change_running_returns_error(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.RUNNING, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
    )

    assert "STOPPED" in result["error"]
    assert result["state"] == DebugState.RUNNING.value


@pytest.mark.asyncio
async def test_apply_code_change_without_ncdbhook_returns_setup_error(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    (netcoredbg.parent / "ncdbhook.dll").unlink()
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
    )

    assert "setup --enc" in result["error"]
    assert result["state"] == DebugState.STOPPED.value


@pytest.mark.asyncio
async def test_apply_code_change_rejects_out_of_range_source_edit(tmp_path: Path):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 999, "end_line": 999, "new_text": "        return 2;"}],
    )

    assert "Invalid edit range 999..999" in result["error"]
    assert source.read_text(encoding="utf-8").count("return 1;") == 1


@pytest.mark.asyncio
async def test_apply_code_change_rude_edit_returns_restart_suggestion(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 5, "end_line": 5, "new_text": "    private int _value;"}],
        compiler=lambda **_kwargs: DeltaResult(
            False,
            None,
            None,
            None,
            ("rude edit: cannot add field 'Sample._value' to an existing class",),
            (),
        ),
    )

    assert "rude edit" in result["error"]
    assert "restart_debug(rebuild=True)" in result["error"]


class FakeDapClient:
    def __init__(
        self,
        *,
        success: bool,
        body: dict | None = None,
        empty_body: bool = False,
    ) -> None:
        self.success = success
        self.body = None if empty_body else (body or {"applied": True})
        self.requests: list[tuple[str, dict, float]] = []

    async def send_request(self, command: str, arguments: dict, timeout: float = 30.0):
        self.requests.append((command, arguments, timeout))
        return SimpleNamespace(success=self.success, message=None, body=self.body)


class FakeSession:
    def __init__(self, state: DebugState, project_path: Path, netcoredbg_path: Path) -> None:
        self.state = SessionState(state=state)
        self.project_path = str(project_path)
        self.netcoredbg_path = str(netcoredbg_path)
        self.client = FakeDapClient(success=True)
        self.transitions: list[DebugState] = []

    def begin_applying_changes(self) -> DebugState:
        previous = self.state.state
        self.state.state = DebugState.APPLYING_CHANGES
        self.transitions.append(DebugState.APPLYING_CHANGES)
        return previous

    def finish_applying_changes(self, previous_state: DebugState) -> None:
        self.state.state = previous_state
        self.transitions.append(previous_state)


def _write_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "Sample.csproj"
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    source = tmp_path / "Sample.cs"
    source.write_text(
        """namespace Fixture;

public class Sample
{
    public int GetValue()
    {
        return 1;
    }
}
""",
        encoding="utf-8",
    )
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_text("exe", encoding="utf-8")
    (tmp_path / "ncdbhook.dll").write_text("hook", encoding="utf-8")
    return project, source, netcoredbg
