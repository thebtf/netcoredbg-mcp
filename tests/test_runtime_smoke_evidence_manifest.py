from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _source_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "app-diagnostics-main",
        "kind": "app_diagnostics",
        "classification": "app_diagnostics",
        "status": "PASS",
        "evidence_ref": "sources/app-diagnostics.json",
        "artifact_path": "sources/app-diagnostics.json",
        "freshness": {"status": "PASS", "source": "live_debug_session"},
        "cleanup": {"status": "PASS", "source": "cleanup_contract"},
        "redaction": {"status": "PASS", "omitted_fields": ["secret"]},
        "limits": {"max_text_length": 240, "max_list_items": 8},
    }
    entry.update(overrides)
    return entry


def test_named_pack_manifest_can_be_written_read_and_bounded(
    tmp_path: Path,
) -> None:
    from netcoredbg_mcp.session.runtime_smoke_v2.evidence_manifest import (
        MANIFEST_SCHEMA_VERSION,
        build_pack_manifest,
        read_pack_manifest,
        write_pack_manifest,
    )

    manifest = build_pack_manifest(
        pack_id="wpf-grid-oracle-pack",
        run_id="run-123",
        evidence_dir=tmp_path,
        sources=[_source_entry()],
        rollups={
            "cleanup": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "freshness": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "redaction": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "limits": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
        },
    )

    manifest_path = write_pack_manifest(manifest, evidence_dir=tmp_path)
    loaded = read_pack_manifest(manifest_path, evidence_dir=tmp_path)

    assert manifest_path == tmp_path / "pack-manifest.json"
    assert loaded["schema"] == "netcoredbg.runtime_smoke.evidence_pack"
    assert loaded["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert loaded["pack_id"] == "wpf-grid-oracle-pack"
    assert loaded["run_id"] == "run-123"
    assert loaded["evidence_dir"] == "."
    assert loaded["sources"] == [
        {
            "id": "app-diagnostics-main",
            "kind": "app_diagnostics",
            "classification": "app_diagnostics",
            "status": "PASS",
            "evidence_ref": "sources/app-diagnostics.json",
            "artifact_path": "sources/app-diagnostics.json",
            "freshness": {"status": "PASS", "source": "live_debug_session"},
            "cleanup": {"status": "PASS", "source": "cleanup_contract"},
            "redaction": {"status": "PASS", "omitted_fields": ["secret"]},
            "limits": {"max_text_length": 240, "max_list_items": 8},
        }
    ]
    assert loaded["rollups"]["cleanup"] == {
        "status": "PASS",
        "source_ids": ["app-diagnostics-main"],
    }
    assert loaded["rollups"]["freshness"] == {
        "status": "PASS",
        "source_ids": ["app-diagnostics-main"],
    }
    assert loaded["rollups"]["redaction"] == {
        "status": "PASS",
        "source_ids": ["app-diagnostics-main"],
    }
    assert loaded["rollups"]["limits"] == {
        "status": "PASS",
        "source_ids": ["app-diagnostics-main"],
    }


def test_named_pack_manifest_rejects_malformed_or_unsafe_refs(
    tmp_path: Path,
) -> None:
    from netcoredbg_mcp.session.runtime_smoke_v2.evidence_manifest import (
        build_pack_manifest,
        validate_manifest_ref,
        write_pack_manifest,
    )

    with pytest.raises(ValueError, match="manifest ref"):
        validate_manifest_ref("../secrets/app-diagnostics.json", evidence_dir=tmp_path)

    malformed = build_pack_manifest(
        pack_id="wpf-grid-oracle-pack",
        run_id="run-123",
        evidence_dir=tmp_path,
        sources=[_source_entry(evidence_ref="../secrets/app-diagnostics.json")],
        rollups={
            "cleanup": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "freshness": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "redaction": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
            "limits": {"status": "PASS", "source_ids": ["app-diagnostics-main"]},
        },
    )

    with pytest.raises(ValueError, match="manifest"):
        write_pack_manifest(malformed, evidence_dir=tmp_path)
