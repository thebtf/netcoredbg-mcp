from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.code_search import CodeSearchEngine
from netcoredbg_mcp.session.state import DebugState, SessionState
from netcoredbg_mcp.tools.debug import register_debug_tools
from netcoredbg_mcp.tools.enc import apply_code_change_to_session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEARCH_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "SearchTestApp"


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.mark.critical
@pytest.mark.asyncio
async def test_stealth_launch_flag_reaches_session_launch(tmp_path: Path) -> None:
    """@critical category: behavioral - stealth launch stays wired to start_debug."""

    app = tmp_path / "app.dll"
    app.write_text("placeholder", encoding="utf-8")
    registry = ToolRegistry()
    session = SimpleNamespace(
        project_path=str(tmp_path),
        state=SimpleNamespace(state=DebugState.IDLE),
        validate_program=MagicMock(return_value=str(app)),
        validate_path=MagicMock(side_effect=lambda path, must_exist=True: path),
        launch=AsyncMock(return_value={"success": True, "program": str(app)}),
    )

    async def notify_state_changed(ctx: Any) -> None:
        return None

    async def resolve_project_root(ctx: Any, session: Any) -> Path:
        return tmp_path

    register_debug_tools(
        registry,
        session,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=notify_state_changed,
        check_session_access=lambda ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=resolve_project_root,
    )
    ctx = SimpleNamespace(
        report_progress=AsyncMock(),
        warning=AsyncMock(),
        info=AsyncMock(),
    )

    response = await registry.tools["start_debug"](
        ctx,
        program=str(app),
        pre_build=False,
        stealth_mode=True,
    )

    assert "error" not in response
    assert session.launch.await_args.kwargs["stealth_mode"] is True


@pytest.mark.critical
def test_code_search_basic_finds_symbol_and_xaml_usage() -> None:
    """@critical category: behavioral - project-scoped code search works."""

    engine = CodeSearchEngine(SEARCH_FIXTURE)

    symbols = engine.find_code_symbol("LoadAssignedCharacter")
    xaml_matches = engine.search_source("textBoxCue|Phrase", file_glob="*.xaml")
    context = engine.get_source_context("ViewModels/MainViewModel.cs", line=5, radius=2)

    assert any(item["name"] == "LoadAssignedCharacter" for item in symbols)
    assert any(str(item["file"]).endswith("MainWindow.xaml") for item in xaml_matches)
    assert context["file"].endswith("ViewModels/MainViewModel.cs")
    assert context["lines"]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_enc_degrades_without_ncdbhook(tmp_path: Path) -> None:
    """@critical category: behavioral - missing ncdbhook reports setup guidance."""

    project = tmp_path / "Sample.csproj"
    project.write_text("<Project Sdk=\"Microsoft.NET.Sdk\" />", encoding="utf-8")
    source = tmp_path / "Sample.cs"
    source.write_text("public class Sample { public int GetValue() => 1; }", encoding="utf-8")
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_text("exe", encoding="utf-8")
    session = SimpleNamespace(
        state=SessionState(state=DebugState.STOPPED),
        project_path=str(project),
        netcoredbg_path=str(netcoredbg),
    )

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 1, "end_line": 1, "new_text": "public class Sample {}"}],
    )

    assert result["state"] == DebugState.STOPPED.value
    assert "ncdbhook.dll not found" in result["error"]
    assert "setup --enc" in result["error"]
