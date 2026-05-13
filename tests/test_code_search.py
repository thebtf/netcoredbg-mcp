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
