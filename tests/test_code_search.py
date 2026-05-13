"""Tests for project-scoped code search."""

from pathlib import Path

import pytest

from netcoredbg_mcp.code_search import CodeSearchEngine

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
            "line": 9,
            "name": "LoadAssignedCharacter",
            "kind": "method",
            "context": "public void LoadAssignedCharacter()",
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
