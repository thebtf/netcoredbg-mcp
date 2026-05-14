from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import jsonpath_ng
import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.probes.file_json import handle_file_json

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class FileJsonProbeSession(ProbeSmokeSession):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root.resolve()

    def validate_path(self, path: str, must_exist: bool = False) -> str:
        resolved = Path(path).resolve()
        self.calls.append(("validate_path", str(resolved), must_exist))
        if self.project_root not in (resolved, *resolved.parents):
            raise ValueError("Path outside project scope")
        return str(resolved)


@pytest.mark.asyncio
async def test_file_json_probe_reads_jsonpath_value(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text(
        json.dumps({"settings": {"spellcheck": {"enabled": True}}}),
        encoding="utf-8",
    )
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "spellcheck_diagnostic",
                "path": str(path),
                "jsonpath": "$.settings.spellcheck.enabled",
                "expected": True,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] is True
    assert probe["expected"] is True
    assert probe["evidence_ref"].endswith("diagnostics.json:$.settings.spellcheck.enabled")


@pytest.mark.asyncio
async def test_file_json_after_only_probe_can_run_without_action(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "runtime-smoke-v2-commit-before-rebind-1000",
                "verdict": "PASS",
                "row_count": 1000,
            }
        ),
        encoding="utf-8",
    )
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "state_only_file_probe",
                    "transitions": [
                        {
                            "settle": {"idle_ms": 0},
                            "probes": [
                                {
                                    "kind": "file.json",
                                    "phase": "after",
                                    "name": "run_id",
                                    "path": str(path),
                                    "jsonpath": "$.run_id",
                                    "expected": "runtime-smoke-v2-commit-before-rebind-1000",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    transition = result["cases"][0]["transitions"][0]
    probe = transition["probes"]["after"][0]
    assert result["status"] == "PASS"
    assert result["action_count"] == 0
    assert transition["actions"] == []
    assert transition["probes"]["before"] == []
    assert probe["status"] == "PASS"
    assert probe["value"] == "runtime-smoke-v2-commit-before-rebind-1000"
    assert probe["expected"] == "runtime-smoke-v2-commit-before-rebind-1000"


@pytest.mark.asyncio
async def test_file_json_probe_blocks_path_outside_project(tmp_path: Path) -> None:
    session = FileJsonProbeSession(tmp_path / "project")
    outside_path = tmp_path / "outside.json"

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "outside_file",
                "path": str(outside_path),
                "jsonpath": "$.value",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "path outside project scope"
    assert probe["requested"]["path"] == str(outside_path)
    assert "project-relative" in probe["next_step"]


@pytest.mark.asyncio
async def test_file_json_probe_blocks_missing_required_path() -> None:
    session = ProbeSmokeSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "missing_path",
                "jsonpath": "$.value",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "missing required path"
    assert probe["accepted"]["path"] == "non-empty file path"


@pytest.mark.asyncio
async def test_file_json_probe_blocks_missing_required_jsonpath(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps({"value": True}), encoding="utf-8")
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "missing_jsonpath",
                "path": str(path),
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "missing required jsonpath"
    assert probe["accepted"]["jsonpath"] == "non-empty JSONPath expression"


@pytest.mark.asyncio
async def test_file_json_probe_without_session_uses_resolved_path(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps({"value": True}), encoding="utf-8")

    result = await handle_file_json(
        {
            "kind": "file.json",
            "name": "sessionless_file",
            "path": str(path),
            "jsonpath": "$.value",
            "expected": True,
        },
        SimpleNamespace(),
        phase="after",
    )

    assert result["status"] == "PASS"
    assert result["value"] is True
    assert result["resolved_path"] == str(path.resolve())


@pytest.mark.asyncio
async def test_file_json_probe_propagates_unexpected_jsonpath_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps({"value": True}), encoding="utf-8")
    session = FileJsonProbeSession(tmp_path)

    def fail_parse(_: str) -> None:
        raise RuntimeError("internal jsonpath adapter bug")

    monkeypatch.setattr(jsonpath_ng, "parse", fail_parse)

    with pytest.raises(RuntimeError, match="internal jsonpath adapter bug"):
        await handle_file_json(
            {
                "kind": "file.json",
                "name": "unexpected_jsonpath_error",
                "path": str(path),
                "jsonpath": "$.value",
            },
            SimpleNamespace(session=session),
            phase="after",
        )


@pytest.mark.asyncio
async def test_file_json_probe_fails_when_file_is_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "missing_file",
                "path": str(missing_path),
                "jsonpath": "$.value",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "json file missing"
    assert probe["missing_side"] == "file"
    assert probe["resolved_path"] == str(missing_path.resolve())


@pytest.mark.asyncio
async def test_file_json_probe_fails_when_path_is_directory(tmp_path: Path) -> None:
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "file.json",
                "name": "directory_path",
                "path": str(tmp_path),
                "jsonpath": "$.value",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "path is not a file"
    assert probe["missing_side"] == "file"
