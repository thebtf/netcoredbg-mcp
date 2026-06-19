"""Runtime smoke debug freshness verifier tests."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from netcoredbg_mcp.session.freshness import DebugFreshnessVerifier
from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState, LoadedSource, ModuleInfo


class FakeFreshnessSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.project_path: str | None = None
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=deque(),
            process_id=None,
            process_name=None,
            modules=[],
            loaded_sources={},
        )


def test_freshness_passes_for_matching_process_workspace_sources_modules_and_artifacts(
    tmp_path,
) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "Program.cs"
    artifact = workspace / "bin" / "Debug" / "App.dll"
    source.parent.mkdir(parents=True)
    artifact.parent.mkdir(parents=True)
    source.write_text("class Program {}", encoding="utf-8")
    artifact.write_text("binary", encoding="utf-8")
    session = FakeFreshnessSession()
    session.project_path = str(workspace)
    session.state.process_id = 1234
    session.state.process_name = "App"
    session.state.loaded_sources = {
        str(source): LoadedSource(name="Program.cs", path=str(source)),
    }
    session.state.modules = [
        ModuleInfo(id=1, name="App.dll", path=str(artifact)),
    ]

    result = (
        DebugFreshnessVerifier(session)
        .verify(
            expected_process_id=1234,
            expected_process_name="App",
            expected_workspace=str(workspace),
            expected_sources=[str(source)],
            expected_modules=[str(artifact)],
            expected_artifacts=[str(artifact)],
        )
        .to_dict()
    )

    assert result["status"] == "PASS"
    assert result["process"]["process_id"] == 1234
    assert result["workspace"]["expected"] == str(workspace)
    assert result["loaded_sources"]["count"] == 1
    assert result["modules"]["count"] == 1
    assert result["artifacts"]["missing"] == []
    assert result["mismatches"] == []
    assert session.runtime_smoke.freshness_evidence["latest"]["status"] == "PASS"


def test_freshness_warns_when_evidence_is_incomplete_but_not_contradictory(
    tmp_path,
) -> None:
    artifact = tmp_path / "App.dll"
    artifact.write_text("binary", encoding="utf-8")
    session = FakeFreshnessSession()

    result = (
        DebugFreshnessVerifier(session)
        .verify(
            expected_process_id=1234,
            expected_workspace=str(tmp_path),
            expected_sources=[str(tmp_path / "Program.cs")],
            expected_modules=[str(artifact)],
            expected_artifacts=[str(artifact)],
        )
        .to_dict()
    )

    assert result["status"] == "WARN"
    assert result["mismatches"] == []
    warning_kinds = {warning["kind"] for warning in result["warnings"]}
    assert {
        "process_id_unavailable",
        "workspace_unavailable",
        "loaded_sources_unavailable",
        "modules_unavailable",
    }.issubset(warning_kinds)
    assert result["artifacts"]["missing"] == []


def test_freshness_fails_for_concrete_process_path_and_artifact_mismatches(
    tmp_path,
) -> None:
    workspace = tmp_path / "repo"
    other = tmp_path / "other"
    source = other / "Program.cs"
    module = other / "Other.dll"
    workspace.mkdir()
    source.parent.mkdir()
    source.write_text("class Program {}", encoding="utf-8")
    module.write_text("binary", encoding="utf-8")
    missing_artifact = workspace / "bin" / "Debug" / "App.dll"
    session = FakeFreshnessSession()
    session.project_path = str(workspace)
    session.state.process_id = 9999
    session.state.process_name = "Other"
    session.state.loaded_sources = {
        str(source): LoadedSource(name="Program.cs", path=str(source)),
    }
    session.state.modules = [
        ModuleInfo(id=1, name="Other.dll", path=str(module)),
    ]

    result = (
        DebugFreshnessVerifier(session)
        .verify(
            expected_process_id=1234,
            expected_process_name="App",
            expected_workspace=str(workspace),
            expected_sources=[str(workspace / "Program.cs")],
            expected_modules=[str(workspace / "bin" / "Debug" / "App.dll")],
            expected_artifacts=[str(missing_artifact)],
        )
        .to_dict()
    )

    assert result["status"] == "FAIL"
    mismatch_kinds = {mismatch["kind"] for mismatch in result["mismatches"]}
    assert {
        "process_id_mismatch",
        "process_name_mismatch",
        "source_workspace_mismatch",
        "expected_source_missing",
        "expected_module_missing",
        "artifact_missing",
    }.issubset(mismatch_kinds)
    assert result["artifacts"]["missing"] == [str(missing_artifact)]


def test_freshness_preserves_module_symbol_status_records_for_pdb_proof(
    tmp_path,
) -> None:
    workspace = tmp_path / "repo"
    artifact = workspace / "bin" / "Debug" / "App.dll"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("binary", encoding="utf-8")
    session = FakeFreshnessSession()
    session.project_path = str(workspace)
    session.state.process_id = 1234
    session.state.process_name = "App"
    session.state.modules = [
        SimpleNamespace(
            name="App.dll",
            path=str(artifact),
            symbolStatus="Symbols loaded.",
        )
    ]

    result = (
        DebugFreshnessVerifier(session)
        .verify(
            expected_process_id=1234,
            expected_process_name="App",
            expected_workspace=str(workspace),
            expected_modules=[str(artifact)],
            expected_artifacts=[str(artifact)],
        )
        .to_dict()
    )

    assert result["status"] == "PASS"
    assert result["modules"]["records"] == [
        {
            "name": "App.dll",
            "path": str(artifact),
            "symbolStatus": "Symbols loaded.",
        }
    ]
