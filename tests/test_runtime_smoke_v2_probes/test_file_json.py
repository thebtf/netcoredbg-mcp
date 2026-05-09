from __future__ import annotations

import json
from pathlib import Path

import pytest

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

    result = await runner(session).run(one_probe_plan({
        "kind": "file.json",
        "name": "spellcheck_diagnostic",
        "path": str(path),
        "jsonpath": "$.settings.spellcheck.enabled",
        "expected": True,
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] is True
    assert probe["expected"] is True
    assert probe["evidence_ref"].endswith("diagnostics.json:$.settings.spellcheck.enabled")


@pytest.mark.asyncio
async def test_file_json_probe_blocks_path_outside_project(tmp_path: Path) -> None:
    session = FileJsonProbeSession(tmp_path / "project")
    outside_path = tmp_path / "outside.json"

    result = await runner(session).run(one_probe_plan({
        "kind": "file.json",
        "name": "outside_file",
        "path": str(outside_path),
        "jsonpath": "$.value",
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "path outside project scope"
    assert probe["requested"]["path"] == str(outside_path)
    assert "project-relative" in probe["next_step"]


@pytest.mark.asyncio
async def test_file_json_probe_fails_when_file_is_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    session = FileJsonProbeSession(tmp_path)

    result = await runner(session).run(one_probe_plan({
        "kind": "file.json",
        "name": "missing_file",
        "path": str(missing_path),
        "jsonpath": "$.value",
    }))

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "json file missing"
    assert probe["missing_side"] == "file"
    assert probe["resolved_path"] == str(missing_path.resolve())
