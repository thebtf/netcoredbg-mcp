from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "netcoredbg.runtime_smoke.evidence_pack"
MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_FILE_NAME = "pack-manifest.json"
REQUIRED_ROLLUP_FIELDS = ("cleanup", "freshness", "redaction", "limits")
REQUIRED_SOURCE_FIELDS = ("id", "kind", "classification", "status")
SOURCE_REF_FIELDS = ("evidence_ref", "artifact_path")
DISAGREEING_SOURCES = "DISAGREEING_SOURCES"
ORACLE_SOURCE_PASS = "ORACLE_SOURCE_PASS"
ORACLE_SOURCE_FAILED = "ORACLE_SOURCE_FAILED"
ORACLE_SOURCE_IMPASSE = "ORACLE_SOURCE_IMPASSE"
ORACLE_SOURCE_BLOCKED = "ORACLE_SOURCE_BLOCKED"
APP_DIAGNOSTICS_OBSERVED = "APP_DIAGNOSTICS_OBSERVED"
APP_DIAGNOSTICS_UNREADABLE = "APP_DIAGNOSTICS_UNREADABLE"
APP_DIAGNOSTICS_MISSING = "APP_DIAGNOSTICS_MISSING"
APP_DIAGNOSTICS_STALE = "APP_DIAGNOSTICS_STALE"
APP_DIAGNOSTICS_BLOCKED = "APP_DIAGNOSTICS_BLOCKED"
APP_DIAGNOSTICS_REPORTED = "APP_DIAGNOSTICS_REPORTED"


def build_pack_manifest(
    *,
    pack_id: str,
    run_id: str,
    evidence_dir: Path | str,
    sources: list[dict[str, Any]],
    rollups: dict[str, Any],
) -> dict[str, Any]:
    """Build a JSON-compatible evidence pack manifest payload."""

    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "pack_id": _required_text(pack_id, "pack_id"),
        "run_id": _required_text(run_id, "run_id"),
        "evidence_dir": _manifest_evidence_dir_ref(evidence_dir),
        "sources": [copy.deepcopy(source) for source in sources],
        "rollups": copy.deepcopy(rollups),
    }


def write_pack_manifest(
    manifest: dict[str, Any],
    *,
    evidence_dir: Path | str,
) -> Path:
    """Validate and write a pack manifest under the evidence directory."""

    _validate_pack_manifest(manifest, evidence_dir=evidence_dir)
    root = Path(evidence_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / MANIFEST_FILE_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path


def read_pack_manifest(
    manifest_path: Path | str,
    *,
    evidence_dir: Path | str,
) -> dict[str, Any]:
    """Read and validate a pack manifest from the evidence directory."""

    path = _validate_manifest_path(manifest_path, evidence_dir=evidence_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pack manifest must be a JSON object")
    _validate_pack_manifest(payload, evidence_dir=evidence_dir)
    return payload


def validate_manifest_ref(ref: str, *, evidence_dir: Path | str) -> str:
    """Return a normalized manifest ref or raise for unsafe/out-of-scope refs."""

    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("manifest ref must be a non-empty string")
    ref_path = Path(ref)
    if ref_path.is_absolute() or ref_path.drive:
        raise ValueError("manifest ref must be relative to evidence_dir")
    if any(part in {"", ".", ".."} for part in ref_path.parts):
        raise ValueError("manifest ref must not contain traversal segments")

    root = Path(evidence_dir).resolve()
    resolved = (root / ref_path).resolve()
    if root not in (resolved, *resolved.parents):
        raise ValueError("manifest ref must stay inside evidence_dir")
    return ref_path.as_posix()


def _validate_pack_manifest(
    manifest: dict[str, Any],
    *,
    evidence_dir: Path | str,
) -> None:
    errors = validate_pack_manifest(manifest, evidence_dir=evidence_dir)
    if errors:
        raise ValueError(f"invalid pack manifest: {'; '.join(errors)}")


def validate_pack_manifest(
    manifest: dict[str, Any],
    *,
    evidence_dir: Path | str,
) -> list[str]:
    """Return deterministic validation errors for a pack manifest payload."""

    errors: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append("manifest.schema is invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("manifest.schema_version is invalid")
    _validate_required_text_field(manifest, "pack_id", errors)
    _validate_required_text_field(manifest, "run_id", errors)
    _validate_required_text_field(manifest, "evidence_dir", errors)

    sources = manifest.get("sources")
    if not isinstance(sources, list):
        errors.append("manifest.sources must be a list")
    else:
        for index, source in enumerate(sources):
            _validate_manifest_source(source, index, evidence_dir, errors)

    rollups = manifest.get("rollups")
    if not isinstance(rollups, dict):
        errors.append("manifest.rollups must be an object")
    else:
        for field_name in REQUIRED_ROLLUP_FIELDS:
            if field_name not in rollups:
                errors.append(f"manifest.rollups.{field_name} is required")
            elif not isinstance(rollups[field_name], dict):
                errors.append(f"manifest.rollups.{field_name} must be an object")
    return errors


def _validate_manifest_source(
    source: Any,
    index: int,
    evidence_dir: Path | str,
    errors: list[str],
) -> None:
    prefix = f"manifest.sources[{index}]"
    if not isinstance(source, dict):
        errors.append(f"{prefix} must be an object")
        return
    for field_name in REQUIRED_SOURCE_FIELDS:
        _validate_required_text_field(source, field_name, errors, prefix=prefix)
    for field_name in SOURCE_REF_FIELDS:
        value = source.get(field_name)
        if value is None:
            continue
        try:
            validate_manifest_ref(value, evidence_dir=evidence_dir)
        except ValueError as exc:
            errors.append(f"{prefix}.{field_name} {exc}")


def _validate_manifest_path(
    manifest_path: Path | str,
    *,
    evidence_dir: Path | str,
) -> Path:
    root = Path(evidence_dir).resolve()
    path = Path(manifest_path).resolve()
    if root not in (path, *path.parents):
        raise ValueError("pack manifest path must stay inside evidence_dir")
    return path


def _manifest_evidence_dir_ref(evidence_dir: Path | str) -> str:
    return "."


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is required")
    return value


def _validate_required_text_field(
    payload: dict[str, Any],
    field_name: str,
    errors: list[str],
    *,
    prefix: str = "manifest",
) -> None:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        errors.append(f"{prefix}.{field_name} is required")
