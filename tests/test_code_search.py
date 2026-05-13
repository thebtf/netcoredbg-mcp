"""Tests for project-scoped code search."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from netcoredbg_mcp.code_search import CodeSearchEngine
from netcoredbg_mcp.session.manager import DebugState

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "SearchTestApp"


def _relative_files(engine: CodeSearchEngine) -> set[str]:
    return {path.relative_to(engine.project_root).as_posix() for path in engine.iter_source_files()}


def test_code_search_engine_discovers_supported_project_files() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    files = _relative_files(engine)

    assert {
        "SearchTestApp.csproj",
        "ViewModels/MainViewModel.cs",
        "Views/MainWindow.xaml",
        "Views/App.axaml",
        "appsettings.json",
        "App.config",
    }.issubset(files)


def test_code_search_engine_respects_gitignore() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    files = _relative_files(engine)

    assert "ignored/IgnoredViewModel.cs" not in files
    assert "Views/Generated.generated.cs" not in files


def test_code_search_engine_keeps_ignored_dirs_with_descendant_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "ignored/\n!ignored/Keep.cs\n",
        encoding="utf-8",
    )
    ignored_dir = tmp_path / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / "Keep.cs").write_text("public class Keep { }\n", encoding="utf-8")
    (ignored_dir / "Drop.cs").write_text("public class Drop { }\n", encoding="utf-8")

    engine = CodeSearchEngine(tmp_path)
    files = _relative_files(engine)

    assert "ignored/Keep.cs" in files
    assert "ignored/Drop.cs" not in files


def test_code_search_engine_rejects_missing_project_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        CodeSearchEngine(missing)


def test_find_code_symbol_returns_csharp_method_definition() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.find_code_symbol("LoadAssignedCharacter")

    assert results == [
        {
            "file": "ViewModels/MainViewModel.cs",
            "line": 10,
            "name": "LoadAssignedCharacter",
            "kind": "method",
            "context": "public void LoadAssignedCharacter()",
        }
    ]


def test_find_code_symbol_supports_constructor_definitions(tmp_path: Path) -> None:
    (tmp_path / "Sample.cs").write_text(
        """public sealed class Sample
{
    public Sample()
    {
    }
}
""",
        encoding="utf-8",
    )
    engine = CodeSearchEngine(tmp_path)

    results = engine.find_code_symbol("Sample", kind="method")

    assert results == [
        {
            "file": "Sample.cs",
            "line": 3,
            "name": "Sample",
            "kind": "method",
            "context": "public Sample()",
        }
    ]


@pytest.mark.parametrize(
    ("name", "kind", "expected_line"),
    [
        ("MainViewModel", "class", 3),
        ("Phrase", "property", 7),
        ("_loadCount", "field", 5),
    ],
)
def test_find_code_symbol_supports_kind_filter(
    name: str,
    kind: str,
    expected_line: int,
) -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.find_code_symbol(name, kind=kind)

    assert len(results) == 1
    assert results[0]["file"] == "ViewModels/MainViewModel.cs"
    assert results[0]["line"] == expected_line
    assert results[0]["name"] == name
    assert results[0]["kind"] == kind
    assert name in results[0]["context"]


def test_find_code_references_returns_cs_and_xaml_usages() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.find_code_references("CueInputPanel")

    assert {
        ("ViewModels/MainViewModel.cs", 8),
        ("Views/MainWindow.xaml", 8),
    }.issubset({(result["file"], result["line"]) for result in results})
    assert all(result["context"] for result in results)


def test_find_code_references_does_not_match_partial_identifiers() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.find_code_references("Cue")

    assert results == []


def test_find_code_references_caps_results() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.find_code_references("CueInputPanel", max_results=1)

    assert len(results) == 1


def test_get_source_context_returns_line_range_with_numbers() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    context = engine.get_source_context("ViewModels/MainViewModel.cs", line=10, radius=2)

    assert context["file"] == "ViewModels/MainViewModel.cs"
    assert context["start_line"] == 8
    assert context["end_line"] == 12
    assert context["lines"] == [
        {"line": 8, "text": '    public string ActiveControlName => "CueInputPanel";'},
        {"line": 9, "text": ""},
        {"line": 10, "text": "    public void LoadAssignedCharacter()"},
        {"line": 11, "text": "    {"},
        {"line": 12, "text": "        _loadCount++;"},
    ]


def test_get_source_context_resolves_unique_basename() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    context = engine.get_source_context("MainViewModel.cs", line=10, radius=0)

    assert context["file"] == "ViewModels/MainViewModel.cs"
    assert context["lines"] == [
        {"line": 10, "text": "    public void LoadAssignedCharacter()"}
    ]


def test_get_source_context_rejects_path_traversal() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    with pytest.raises(ValueError, match="outside project root"):
        engine.get_source_context("../WpfSmokeApp/WpfSmokeApp.csproj", line=1, radius=0)


def test_search_source_finds_regex_matches_in_globbed_xaml() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.search_source("textBoxCue|Phrase", file_glob="*.xaml")

    assert results == [
        {
            "file": "Views/MainWindow.xaml",
            "line": 7,
            "context": '<TextBox x:Name="textBoxCue" Text="{Binding Phrase}" />',
        }
    ]


def test_search_source_caps_results() -> None:
    engine = CodeSearchEngine(FIXTURE_ROOT)

    results = engine.search_source("CueInputPanel", max_results=1)

    assert len(results) == 1


@pytest.mark.asyncio
async def test_code_search_tools_work_without_active_debug_session(capturing_mcp) -> None:
    from netcoredbg_mcp.tools.code_search import register_code_search_tools

    async def resolve_project_root(ctx, session):
        return FIXTURE_ROOT

    session = SimpleNamespace(
        project_path=None,
        state=SimpleNamespace(state=DebugState.IDLE),
    )

    register_code_search_tools(
        capturing_mcp,
        session,
        resolve_project_root=resolve_project_root,
    )

    assert {
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
        "search_source",
    }.issubset(capturing_mcp.tools)

    symbol = await capturing_mcp.tools["find_code_symbol"](
        SimpleNamespace(),
        name="LoadAssignedCharacter",
    )
    references = await capturing_mcp.tools["find_code_references"](
        SimpleNamespace(),
        name="CueInputPanel",
    )
    context = await capturing_mcp.tools["get_source_context"](
        SimpleNamespace(),
        file="MainViewModel.cs",
        line=10,
        radius=0,
    )
    search = await capturing_mcp.tools["search_source"](
        SimpleNamespace(),
        pattern="textBoxCue|Phrase",
        file_glob="*.xaml",
    )

    assert symbol["state"] == DebugState.IDLE.value
    assert symbol["data"]["results"][0]["name"] == "LoadAssignedCharacter"
    assert references["data"]["count"] >= 2
    assert context["data"]["lines"][0]["line"] == 10
    assert search["data"]["results"][0]["file"] == "Views/MainWindow.xaml"


@pytest.mark.asyncio
async def test_code_search_tools_register_on_server() -> None:
    from netcoredbg_mcp.server import create_server

    server = create_server(str(FIXTURE_ROOT))
    tool_names = {tool.name for tool in await server.list_tools()}

    assert {
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
        "search_source",
    }.issubset(tool_names)
