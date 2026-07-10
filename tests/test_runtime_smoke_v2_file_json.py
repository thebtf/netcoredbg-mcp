"""file.json probe tests: diagnostic-latest alias resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.probes.file_json import handle_file_json


class _Context:
    session: Any = None


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_latest_alias_resolves_to_newest_diagnostic_snapshot(tmp_path: Path) -> None:
    import os

    older = tmp_path / "diagnostic-startup.json"
    newer = tmp_path / "diagnostic-cue-change.json"
    _write(older, {"value": "old"})
    _write(newer, {"value": "new"})
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    result = await handle_file_json(
        {
            "name": "latest",
            "path": str(tmp_path / "diagnostic-latest.json"),
            "jsonpath": "$.value",
        },
        _Context(),
        phase="after",
    )

    assert result["status"] == "PASS"
    assert result["value"] == "new"
    assert result["resolved_path"].endswith("diagnostic-cue-change.json")


@pytest.mark.asyncio
async def test_latest_alias_prefers_literal_file_when_present(tmp_path: Path) -> None:
    _write(tmp_path / "diagnostic-startup.json", {"value": "stage"})
    _write(tmp_path / "diagnostic-latest.json", {"value": "literal"})

    result = await handle_file_json(
        {
            "name": "latest",
            "path": str(tmp_path / "diagnostic-latest.json"),
            "jsonpath": "$.value",
        },
        _Context(),
        phase="after",
    )

    assert result["status"] == "PASS"
    assert result["value"] == "literal"


@pytest.mark.asyncio
async def test_latest_alias_fails_honestly_when_no_snapshots_exist(tmp_path: Path) -> None:
    result = await handle_file_json(
        {
            "name": "latest",
            "path": str(tmp_path / "diagnostic-latest.json"),
            "jsonpath": "$.value",
        },
        _Context(),
        phase="after",
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "json file missing"


@pytest.mark.asyncio
async def test_non_alias_missing_file_still_fails(tmp_path: Path) -> None:
    _write(tmp_path / "diagnostic-startup.json", {"value": "stage"})

    result = await handle_file_json(
        {
            "name": "explicit",
            "path": str(tmp_path / "diagnostic-other.json"),
            "jsonpath": "$.value",
        },
        _Context(),
        phase="after",
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "json file missing"
