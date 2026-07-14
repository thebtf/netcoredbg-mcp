"""CR-113 read-only debug launch compatibility preflight tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from netcoredbg_mcp.mux import SessionOwnership
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.setup import dbgshim
from netcoredbg_mcp.tools.debug import register_debug_tools
from netcoredbg_mcp.utils import version as version_utils
from netcoredbg_mcp.utils.version import VersionInfo

UNKNOWN_TARGET_WARNING = (
    "Target runtime version is unavailable; compatibility cannot be determined."
)
UNKNOWN_ACTIVE_SWAP_WARNING = (
    "Active dbgshim version is unavailable; start_debug would select the cached candidate."
)
UNKNOWN_ACTIVE_WARNING = (
    "Active dbgshim version is unavailable; compatibility cannot be determined."
)
SWAP_WARNING = (
    "A compatible cached dbgshim is available; start_debug would replace the shared debugger copy."
)
BLOCKED_WARNING = "No cached dbgshim matches the target runtime; start_debug remains fail-open."
CACHE_UNREADABLE_WARNING = (
    "The dbgshim cache could not be read; compatibility cannot be determined."
)
CANDIDATE_MALFORMED_WARNING = (
    "The selected cached dbgshim version is malformed; compatibility cannot be determined."
)


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.annotations: dict[str, object] = {}

    def tool(self, annotations=None):
        def decorator(func):
            self.tools[func.__name__] = func
            self.annotations[func.__name__] = annotations
            return func

        return decorator


def _target(status: str = "known", major: int = 10) -> dict[str, object]:
    known = status == "known"
    return {
        "version": f"{major}.0.1" if known else None,
        "major": major if known else None,
        "runtimeconfigPath": "C:/project/App.runtimeconfig.json",
        "source": "runtimeconfig_framework" if known else None,
        "status": status,
    }


def _active(status: str = "known", major: int = 6) -> dict[str, object]:
    known = status == "known"
    source = "unsupported_platform" if status == "unsupported_platform" else "windows_file_version"
    return {
        "version": f"{major}.0.2" if known else None,
        "major": major if known else None,
        "path": "C:/debugger/dbgshim.dll",
        "source": source,
        "status": status,
    }


def _candidate(status: str = "known", major: int = 10) -> dict[str, object]:
    selected = status in {"known", "malformed"}
    return {
        "version": f"{major}.0.3" if status == "known" else None,
        "major": major if status == "known" else None,
        "path": f"C:/cache/{major}.0.3/dbgshim.dll" if selected else None,
        "provenance": "cache_directory_name" if selected else None,
        "selection": "same_major_highest_numeric_tuple",
        "status": status,
    }


@pytest.mark.parametrize(
    (
        "target",
        "active",
        "candidate",
        "expected_verdict",
        "expected_compatible",
        "expected_mutation",
        "expected_warning",
    ),
    [
        (
            _target("missing"),
            _active("known", 6),
            {**_candidate("missing"), "selection": "not_attempted"},
            "unknown",
            None,
            False,
            UNKNOWN_TARGET_WARNING,
        ),
        (
            _target("malformed"),
            _active("known", 6),
            {**_candidate("missing"), "selection": "not_attempted"},
            "unknown",
            None,
            False,
            UNKNOWN_TARGET_WARNING,
        ),
        (
            _target("known", 10),
            _active("unsupported_platform"),
            _candidate("known", 10),
            "compatible_after_swap",
            True,
            True,
            SWAP_WARNING,
        ),
        (
            _target("known", 10),
            _active("missing"),
            _candidate("known", 10),
            "compatible_after_swap",
            True,
            True,
            SWAP_WARNING,
        ),
        (
            _target("known", 10),
            _active("missing"),
            _candidate("missing", 10),
            "unknown",
            None,
            False,
            UNKNOWN_ACTIVE_WARNING,
        ),
        (
            _target("known", 10),
            _active("known", 10),
            _candidate("known", 10),
            "compatible",
            True,
            True,
            SWAP_WARNING,
        ),
        (
            _target("known", 10),
            _active("known", 10),
            _candidate("no_match", 10),
            "compatible",
            True,
            False,
            None,
        ),
        (
            _target("known", 10),
            _active("known", 6),
            _candidate("known", 10),
            "compatible_after_swap",
            True,
            True,
            SWAP_WARNING,
        ),
        (
            _target("known", 10),
            _active("known", 6),
            _candidate("missing", 10),
            "blocked_no_matching_shim",
            False,
            False,
            BLOCKED_WARNING,
        ),
        (
            _target("known", 10),
            _active("known", 6),
            _candidate("unreadable", 10),
            "unknown",
            None,
            False,
            CACHE_UNREADABLE_WARNING,
        ),
    ],
)
def test_launch_compatibility_decision_covers_truth_table(
    target: dict[str, object],
    active: dict[str, object],
    candidate: dict[str, object],
    expected_verdict: str,
    expected_compatible: bool | None,
    expected_mutation: bool,
    expected_warning: str | None,
) -> None:
    result = dbgshim.build_debug_launch_compatibility(
        program="C:/project/App.dll",
        target_runtime=target,
        active_dbgshim=active,
        cached_candidate=candidate,
    )

    assert result == {
        "verdict": expected_verdict,
        "program": "C:/project/App.dll",
        "targetRuntime": target,
        "activeDbgshim": active,
        "cachedCandidate": candidate,
        "compatible": expected_compatible,
        "willMutateSharedDebugger": expected_mutation,
        "mutationPerformed": False,
        "warning": expected_warning,
    }


def test_launch_compatibility_selected_malformed_candidate_is_unknown() -> None:
    result = dbgshim.build_debug_launch_compatibility(
        program="C:/project/App.dll",
        target_runtime=_target("known", 10),
        active_dbgshim=_active("known", 6),
        cached_candidate=_candidate("malformed", 10),
    )

    assert (
        result["verdict"],
        result["compatible"],
        result["willMutateSharedDebugger"],
        result["warning"],
    ) == ("unknown", None, True, CANDIDATE_MALFORMED_WARNING)


def _write_cached_shim(cache: Path, version: str, content: bytes = b"shim") -> Path:
    path = cache / version / dbgshim._DBGSHIM_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def test_select_dbgshim_decision_uses_highest_same_major_numeric_tuple(
    tmp_path: Path,
) -> None:
    _write_cached_shim(tmp_path, "6.0.99")
    _write_cached_shim(tmp_path, "10.0.2")
    expected = _write_cached_shim(tmp_path, "10.0.12")

    decision = dbgshim.select_dbgshim_decision("10.0.0", tmp_path)

    assert decision.as_candidate() == {
        "version": "10.0.12",
        "major": 10,
        "path": str(expected),
        "provenance": "cache_directory_name",
        "selection": "same_major_highest_numeric_tuple",
        "status": "known",
    }


def test_select_dbgshim_decision_preserves_malformed_selected_candidate(
    tmp_path: Path,
) -> None:
    _write_cached_shim(tmp_path, "10.0.12")
    expected = _write_cached_shim(tmp_path, "10.bad.99")

    decision = dbgshim.select_dbgshim_decision("10.0.0", tmp_path)

    assert decision.as_candidate() == {
        "version": None,
        "major": None,
        "path": str(expected),
        "provenance": "cache_directory_name",
        "selection": "same_major_highest_numeric_tuple",
        "status": "malformed",
    }


@pytest.mark.parametrize(
    ("cache_setup", "expected_status"),
    [
        ("missing", "missing"),
        ("empty", "no_match"),
        ("other_major", "no_match"),
        ("regular_file", "no_match"),
    ],
)
def test_select_dbgshim_decision_reports_readable_no_match_states(
    tmp_path: Path,
    cache_setup: str,
    expected_status: str,
) -> None:
    cache = tmp_path / "cache"
    if cache_setup == "regular_file":
        cache.write_bytes(b"not-a-directory")
    elif cache_setup != "missing":
        cache.mkdir()
    if cache_setup == "other_major":
        _write_cached_shim(cache, "6.0.99")

    decision = dbgshim.select_dbgshim_decision("10.0.0", cache)

    assert decision.as_candidate()["status"] == expected_status


@pytest.mark.parametrize("probe", ["exists", "is_dir", "iterdir"])
def test_select_dbgshim_decision_reports_unreadable_cache(
    tmp_path: Path,
    probe: str,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()

    with patch.object(Path, probe, side_effect=PermissionError("denied")):
        decision = dbgshim.select_dbgshim_decision("10.0.0", cache)

    assert decision.as_candidate() == {
        "version": None,
        "major": None,
        "path": None,
        "provenance": None,
        "selection": "same_major_highest_numeric_tuple",
        "status": "unreadable",
    }
    assert isinstance(decision._lookup_error, PermissionError)


def test_target_runtime_evidence_reports_framework_source(tmp_path: Path) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")
    runtimeconfig.write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "framework": {
                        "name": "Microsoft.NETCore.App",
                        "version": "10.0.1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence == {
        "version": "10.0.1",
        "major": 10,
        "runtimeconfigPath": str(runtimeconfig),
        "source": "runtimeconfig_framework",
        "status": "known",
    }


def test_target_runtime_evidence_reports_frameworks_source(tmp_path: Path) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")
    runtimeconfig.write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "frameworks": [
                        {"name": "Other.Framework", "version": "1.0.0"},
                        {"name": "Microsoft.NETCore.App", "version": "10.0.2"},
                        "ignored-after-netcore-match",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence["source"] == "runtimeconfig_frameworks"
    assert evidence["version"] == "10.0.2"


@pytest.mark.parametrize(
    "runtime_options",
    [
        {
            "framework": [],
            "frameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.2"}],
        },
        {
            "frameworks": [
                "not-an-object",
                {"name": "Microsoft.NETCore.App", "version": "10.0.2"},
            ]
        },
    ],
)
def test_target_runtime_evidence_rejects_malformed_framework_container(
    tmp_path: Path,
    runtime_options: dict[str, object],
) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")
    runtimeconfig.write_text(
        json.dumps({"runtimeOptions": runtime_options}),
        encoding="utf-8",
    )

    evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence == {
        "version": None,
        "major": None,
        "runtimeconfigPath": str(runtimeconfig),
        "source": None,
        "status": "malformed",
    }


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [
        (None, "missing"),
        ("not-json", "malformed"),
        (json.dumps({"runtimeOptions": {}}), "malformed"),
        (
            json.dumps(
                {
                    "runtimeOptions": {
                        "framework": {
                            "name": "Microsoft.NETCore.App",
                            "version": "not-a-version",
                        }
                    }
                }
            ),
            "malformed",
        ),
    ],
)
def test_target_runtime_evidence_reports_missing_and_malformed(
    tmp_path: Path,
    contents: str | None,
    expected_status: str,
) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")
    if contents is not None:
        runtimeconfig.write_text(contents, encoding="utf-8")

    evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence["status"] == expected_status


def test_target_runtime_evidence_reports_unreadable(tmp_path: Path) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")
    runtimeconfig.write_text("{}", encoding="utf-8")
    original_open = Path.open

    def deny_runtimeconfig(path: Path, *args, **kwargs):
        if path == runtimeconfig:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    with patch.object(Path, "open", deny_runtimeconfig):
        evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence["status"] == "unreadable"


def test_target_runtime_evidence_reports_unreadable_resolution(
    tmp_path: Path,
) -> None:
    program = tmp_path / "App.dll"
    program.write_bytes(b"")
    runtimeconfig = program.with_suffix(".runtimeconfig.json")

    with patch.object(Path, "resolve", side_effect=PermissionError("denied")):
        evidence = version_utils.inspect_target_runtime_version(str(program))

    assert evidence == {
        "version": None,
        "major": None,
        "runtimeconfigPath": str(runtimeconfig),
        "source": None,
        "status": "unreadable",
    }


def test_active_dbgshim_evidence_is_explicitly_unsupported_off_windows(
    tmp_path: Path,
) -> None:
    netcoredbg = tmp_path / "netcoredbg"
    netcoredbg.write_bytes(b"")

    with (
        patch("netcoredbg_mcp.utils.version.platform.system", return_value="Linux"),
        patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version",
            side_effect=AssertionError("must not inspect unsupported platform"),
        ),
    ):
        evidence = version_utils.inspect_active_dbgshim_version(str(netcoredbg))

    assert evidence == {
        "version": None,
        "major": None,
        "path": str(tmp_path / "libdbgshim.so"),
        "source": "unsupported_platform",
        "status": "unsupported_platform",
    }


def test_active_dbgshim_evidence_reports_windows_file_version(tmp_path: Path) -> None:
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_bytes(b"")
    (tmp_path / "dbgshim.dll").write_bytes(b"shim")

    with (
        patch("netcoredbg_mcp.utils.version.platform.system", return_value="Windows"),
        patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version",
            return_value=VersionInfo(major=10, minor=0, patch=3, build=4),
        ),
    ):
        evidence = version_utils.inspect_active_dbgshim_version(str(netcoredbg))

    assert evidence == {
        "version": "10.0.3.4",
        "major": 10,
        "path": str(tmp_path / "dbgshim.dll"),
        "source": "windows_file_version",
        "status": "known",
    }


def test_active_dbgshim_evidence_reports_unreadable_metadata_probe(
    tmp_path: Path,
) -> None:
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_bytes(b"")

    with (
        patch("netcoredbg_mcp.utils.version.platform.system", return_value="Windows"),
        patch.object(Path, "is_file", side_effect=PermissionError("denied")),
    ):
        evidence = version_utils.inspect_active_dbgshim_version(str(netcoredbg))

    assert evidence == {
        "version": None,
        "major": None,
        "path": str(tmp_path / "dbgshim.dll"),
        "source": "windows_file_version",
        "status": "unreadable",
    }


def _manager(project_path: Path | None, netcoredbg_path: Path | None = None) -> SessionManager:
    client = SimpleNamespace(
        netcoredbg_path=str(netcoredbg_path or Path("netcoredbg.exe")),
        is_running=False,
    )
    with patch("netcoredbg_mcp.session.manager.DAPClient", return_value=client):
        return SessionManager(
            netcoredbg_path=str(netcoredbg_path) if netcoredbg_path else None,
            project_path=str(project_path) if project_path else None,
        )


def _write_program(root: Path, name: str = "App.dll", version: str = "10.0.1") -> Path:
    program = root / name
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_bytes(b"program")
    program.with_suffix(".runtimeconfig.json").write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "framework": {
                        "name": "Microsoft.NETCore.App",
                        "version": version,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return program


def test_validate_program_for_project_root_accepts_relative_dll(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    program = _write_program(project)
    manager = _manager(tmp_path / "session-owner")

    validated = manager.validate_program_for_project_root("App.dll", str(project))

    assert validated == str(program.resolve())


def test_validate_program_for_project_root_preserves_exe_to_dll_resolution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    exe = project / "App.exe"
    exe.write_bytes(b"host")
    dll = _write_program(project)
    manager = _manager(project)

    validated = manager.validate_program_for_project_root(str(exe), str(project))

    assert validated == str(dll.resolve())


def test_validate_program_for_project_root_rejects_unresolved_root(
    tmp_path: Path,
) -> None:
    manager = _manager(None)

    with pytest.raises(ValueError, match="project root"):
        manager.validate_program_for_project_root("App.dll", str(tmp_path / "missing"))


def test_validate_program_for_project_root_rejects_invalid_extension(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    invalid = project / "App.txt"
    invalid.write_text("not an assembly", encoding="utf-8")
    manager = _manager(project)

    with pytest.raises(ValueError, match=r"must be \.NET assembly"):
        manager.validate_program_for_project_root(str(invalid), str(project))


@pytest.mark.parametrize("program_kind", ["traversal", "outside_absolute"])
def test_validate_program_for_project_root_rejects_outside_paths(
    tmp_path: Path,
    program_kind: str,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    outside_program = _write_program(outside)
    manager = _manager(project)
    program = (
        str(project / ".." / "outside" / "App.dll")
        if program_kind == "traversal"
        else str(outside_program)
    )

    with pytest.raises(ValueError, match="outside exact project root"):
        manager.validate_program_for_project_root(program, str(project))


def test_validate_program_for_project_root_ignores_worktrees_and_allowlist(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "related-worktree"
    project.mkdir()
    outside.mkdir()
    outside_program = _write_program(outside)
    manager = _manager(project)

    with (
        patch.object(manager, "_get_worktree_paths", return_value=[str(outside)]) as worktrees,
        patch.dict(os.environ, {"NETCOREDBG_ALLOWED_PATHS": str(outside)}),
        pytest.raises(ValueError, match="outside exact project root"),
    ):
        manager.validate_program_for_project_root(str(outside_program), str(project))

    worktrees.assert_not_called()


def test_validate_program_for_project_root_rejects_program_symlink_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    outside_program = _write_program(outside)
    linked_program = project / "Linked.dll"
    linked_program.symlink_to(outside_program)
    manager = _manager(project)

    with pytest.raises(ValueError, match="outside exact project root"):
        manager.validate_program_for_project_root(str(linked_program), str(project))


def test_validate_program_for_project_root_rejects_runtimeconfig_symlink_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    program = project / "App.dll"
    program.write_bytes(b"program")
    outside_runtimeconfig = outside / "App.runtimeconfig.json"
    outside_runtimeconfig.write_text("{}", encoding="utf-8")
    program.with_suffix(".runtimeconfig.json").symlink_to(outside_runtimeconfig)
    manager = _manager(project)

    with pytest.raises(ValueError, match="[Rr]untimeconfig.*outside exact project root"):
        manager.validate_program_for_project_root(str(program), str(project))


def _snapshot_tree(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}

    entries: dict[str, object] = {}
    nodes = [path, *sorted(path.rglob("*"))]
    for node in nodes:
        relative = "." if node == path else node.relative_to(path).as_posix()
        stat_result = node.stat()
        entry: dict[str, object] = {
            "kind": "dir" if node.is_dir() else "file",
            "mtime_ns": stat_result.st_mtime_ns,
        }
        if node.is_file():
            content = node.read_bytes()
            entry["size"] = len(content)
            entry["sha256"] = hashlib.sha256(content).hexdigest()
        entries[relative] = entry
    return {"exists": True, "entries": entries}


def _register_preflight(
    manager: SessionManager,
    *,
    ownership: SessionOwnership | object,
    resolve_project_root_readonly=None,
):
    registry = ToolRegistry()
    mutable_resolver = AsyncMock(side_effect=AssertionError("mutable resolver called"))
    access_check = MagicMock(side_effect=AssertionError("ownership/access check called"))
    notify = AsyncMock(side_effect=AssertionError("notification sent"))
    execute = AsyncMock(side_effect=AssertionError("debug control executed"))
    register_debug_tools(
        registry,
        manager,
        ownership=ownership,
        notify_state_changed=notify,
        check_session_access=access_check,
        execute_and_wait=execute,
        resolve_project_root=mutable_resolver,
        resolve_project_root_readonly=resolve_project_root_readonly,
    )
    return registry, mutable_resolver, access_check, notify, execute


@pytest.mark.asyncio
async def test_preflight_is_repeatable_and_side_effect_free(tmp_path: Path) -> None:
    project = tmp_path / "project"
    debugger = tmp_path / "debugger"
    home = tmp_path / "home"
    project.mkdir()
    debugger.mkdir()
    home.mkdir()
    program = _write_program(project)
    netcoredbg = debugger / "netcoredbg.exe"
    netcoredbg.write_bytes(b"debugger")
    (debugger / "dbgshim.dll").write_bytes(b"active-six")
    cache = home / ".netcoredbg-mcp" / "dbgshim"
    selected = _write_cached_shim(cache, "10.0.8", b"candidate-ten")
    manager = _manager(project, netcoredbg)
    client_before = manager.client
    state_before = manager.state
    project_path_before = manager.project_path
    ownership = SessionOwnership()
    ownership.claim("owner-session")
    ownership_before = vars(ownership).copy()
    readonly_resolver = AsyncMock(return_value=project)
    registry, mutable_resolver, access_check, notify, execute = _register_preflight(
        manager,
        ownership=ownership,
        resolve_project_root_readonly=readonly_resolver,
    )
    filesystem_before = {
        "project": _snapshot_tree(project),
        "debugger": _snapshot_tree(debugger),
        "cache": _snapshot_tree(cache),
        "home": _snapshot_tree(home),
    }
    manager.launch = AsyncMock(side_effect=AssertionError("launch called"))
    manager.start = AsyncMock(side_effect=AssertionError("DAP start called"))
    manager.pre_launch_build = AsyncMock(side_effect=AssertionError("build called"))
    manager.check_dbgshim_compatibility = MagicMock(
        side_effect=AssertionError("mutating compatibility wrapper called")
    )
    active = {
        "version": "6.0.36.1",
        "major": 6,
        "path": str(debugger / "dbgshim.dll"),
        "source": "windows_file_version",
        "status": "known",
    }

    with (
        patch.object(Path, "home", return_value=home),
        patch("netcoredbg_mcp.setup.dbgshim.get_home_dir", side_effect=AssertionError),
        patch(
            "netcoredbg_mcp.setup.dbgshim.extract_dbgshim_versions",
            side_effect=AssertionError,
        ),
        patch(
            "netcoredbg_mcp.setup.dbgshim.scan_installed_runtimes",
            side_effect=AssertionError,
        ),
        patch(
            "netcoredbg_mcp.setup.dbgshim.select_and_swap_dbgshim",
            side_effect=AssertionError,
        ),
        patch("netcoredbg_mcp.setup.dbgshim.swap_dbgshim", side_effect=AssertionError),
        patch(
            "netcoredbg_mcp.setup.dbgshim.inspect_active_dbgshim_version",
            return_value=active,
        ),
    ):
        tool = registry.tools["inspect_debug_launch_compatibility"]
        first = await tool(SimpleNamespace(), str(program))
        second = await tool(SimpleNamespace(), str(program))

    assert first == second
    assert first["data"]["verdict"] == "compatible_after_swap"
    assert first["data"]["cachedCandidate"]["path"] == str(selected)
    assert first["data"]["mutationPerformed"] is False
    assert filesystem_before == {
        "project": _snapshot_tree(project),
        "debugger": _snapshot_tree(debugger),
        "cache": _snapshot_tree(cache),
        "home": _snapshot_tree(home),
    }
    assert manager.project_path == project_path_before
    assert manager.client is client_before
    assert manager.state is state_before
    assert vars(ownership) == ownership_before
    mutable_resolver.assert_not_awaited()
    access_check.assert_not_called()
    notify.assert_not_awaited()
    execute.assert_not_awaited()
    readonly_resolver.assert_has_awaits(
        [
            call(SimpleNamespace(), manager),
            call(SimpleNamespace(), manager),
        ]
    )


@pytest.mark.asyncio
async def test_preflight_missing_cache_does_not_create_home_or_cache(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    debugger = tmp_path / "debugger"
    home = tmp_path / "home"
    project.mkdir()
    debugger.mkdir()
    home.mkdir()
    program = _write_program(project)
    netcoredbg = debugger / "netcoredbg.exe"
    netcoredbg.write_bytes(b"debugger")
    manager = _manager(project, netcoredbg)
    registry, *_ = _register_preflight(manager, ownership=object())
    active = {
        "version": "6.0.36.1",
        "major": 6,
        "path": str(debugger / "dbgshim.dll"),
        "source": "windows_file_version",
        "status": "known",
    }
    home_before = _snapshot_tree(home)

    with (
        patch.object(Path, "home", return_value=home),
        patch(
            "netcoredbg_mcp.setup.dbgshim.inspect_active_dbgshim_version",
            return_value=active,
        ),
    ):
        response = await registry.tools["inspect_debug_launch_compatibility"](
            SimpleNamespace(), str(program)
        )

    assert response["data"]["verdict"] == "blocked_no_matching_shim"
    assert response["data"]["cachedCandidate"]["status"] == "missing"
    assert _snapshot_tree(home) == home_before
    assert not (home / ".netcoredbg-mcp").exists()


@pytest.mark.asyncio
async def test_preflight_uses_readonly_root_without_mutating_session_scope(
    tmp_path: Path,
) -> None:
    owner_project = tmp_path / "owner"
    inspected_project = tmp_path / "inspected"
    debugger = tmp_path / "debugger"
    owner_project.mkdir()
    inspected_project.mkdir()
    debugger.mkdir()
    program = _write_program(inspected_project)
    netcoredbg = debugger / "netcoredbg.exe"
    netcoredbg.write_bytes(b"debugger")
    manager = _manager(owner_project, netcoredbg)
    readonly_resolver = AsyncMock(return_value=inspected_project)
    registry, mutable_resolver, *_ = _register_preflight(
        manager,
        ownership=object(),
        resolve_project_root_readonly=readonly_resolver,
    )

    with (
        patch.object(Path, "home", return_value=tmp_path / "missing-home"),
        patch(
            "netcoredbg_mcp.setup.dbgshim.inspect_active_dbgshim_version",
            return_value=_active("missing"),
        ),
    ):
        response = await registry.tools["inspect_debug_launch_compatibility"](
            SimpleNamespace(), str(program)
        )

    assert "error" not in response
    assert response["data"]["program"] == str(program.resolve())
    assert manager.project_path == str(owner_project.resolve())
    mutable_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_root_resolver_does_not_fallback_to_fixed_session_path(
    tmp_path: Path,
) -> None:
    from netcoredbg_mcp.server import resolve_project_root_readonly

    fixed_root = tmp_path / "fixed-root"
    fixed_root.mkdir()
    set_project_path = MagicMock(side_effect=AssertionError("session root mutated"))
    session = SimpleNamespace(
        project_path=str(fixed_root),
        set_project_path=set_project_path,
    )
    ctx = SimpleNamespace()

    with patch(
        "netcoredbg_mcp.server.get_project_root",
        new=AsyncMock(return_value=None),
    ):
        resolved = await resolve_project_root_readonly(ctx, session)

    assert resolved is None
    assert session.project_path == str(fixed_root)
    set_project_path.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_fails_closed_when_root_is_unresolved(tmp_path: Path) -> None:
    manager = _manager(None, tmp_path / "netcoredbg.exe")
    readonly_resolver = AsyncMock(return_value=None)
    registry, *_ = _register_preflight(
        manager,
        ownership=object(),
        resolve_project_root_readonly=readonly_resolver,
    )

    response = await registry.tools["inspect_debug_launch_compatibility"](
        SimpleNamespace(), "App.dll"
    )

    assert "resolved project root is required" in response["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["invalid_extension", "outside_root"])
async def test_preflight_rejects_invalid_program_before_inspection(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    debugger = tmp_path / "debugger"
    project.mkdir()
    outside.mkdir()
    debugger.mkdir()
    if invalid_kind == "invalid_extension":
        program = project / "App.txt"
        program.write_text("not an assembly", encoding="utf-8")
    else:
        program = _write_program(outside)
    manager = _manager(project, debugger / "netcoredbg.exe")
    registry, mutable_resolver, access_check, notify, execute = _register_preflight(
        manager,
        ownership=object(),
    )

    with patch(
        "netcoredbg_mcp.setup.dbgshim.inspect_debug_launch_compatibility",
        side_effect=AssertionError("inspection ran before validation"),
    ) as inspect:
        response = await registry.tools["inspect_debug_launch_compatibility"](
            SimpleNamespace(), str(program)
        )

    assert "error" in response
    inspect.assert_not_called()
    mutable_resolver.assert_not_awaited()
    access_check.assert_not_called()
    notify.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_debug_keeps_mutable_resolver_and_ignores_readonly_resolver(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    session = SimpleNamespace(
        project_path=str(tmp_path),
        state=SimpleNamespace(state="idle"),
        validate_program=MagicMock(side_effect=lambda program, must_exist=True: program),
        validate_path=MagicMock(side_effect=lambda path, must_exist=True: path),
        launch=AsyncMock(return_value={"success": True, "program": "App.dll"}),
    )
    mutable_resolver = AsyncMock()
    readonly_resolver = AsyncMock(side_effect=AssertionError("readonly resolver called"))

    register_debug_tools(
        registry,
        session,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=AsyncMock(),
        check_session_access=lambda _ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=mutable_resolver,
        resolve_project_root_readonly=readonly_resolver,
    )

    response = await registry.tools["start_debug"](
        SimpleNamespace(
            report_progress=AsyncMock(),
            warning=AsyncMock(),
            info=AsyncMock(),
        ),
        program="App.dll",
        pre_build=False,
        args=["one"],
        env={"MODE": "test"},
        stop_at_entry=True,
        build_configuration="Release",
        stealth_mode=True,
    )

    assert "error" not in response
    mutable_resolver.assert_awaited_once()
    readonly_resolver.assert_not_awaited()
    assert session.launch.await_args.kwargs == {
        "program": "App.dll",
        "cwd": None,
        "args": ["one"],
        "env": {"MODE": "test"},
        "stop_at_entry": True,
        "pre_build": False,
        "build_project": None,
        "build_configuration": "Release",
        "stealth_mode": True,
        "progress_callback": session.launch.await_args.kwargs["progress_callback"],
        "output_callback": session.launch.await_args.kwargs["output_callback"],
    }
