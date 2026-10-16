"""Behavioral RED contracts for the A1 opt-in preview artifact runway."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from http.client import HTTPMessage
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.response import addinfourl

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from scripts import stateless_preview_artifact as artifact_contract

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_CONTRACT_PATH = PROJECT_ROOT / "scripts" / "stateless_preview_artifact.py"
EXACT_HEAD_RUNNER_PATH = PROJECT_ROOT / "scripts" / "run_sonarqube_exact_head.py"
RELEASE_GATE_CATALOG_CONTRACT = (
    PROJECT_ROOT / "specs" / "010-a1-preview-artifact" / "contracts" / "release-gate-catalog.md"
)
SOURCE_REF = "refs/heads/main"
REPOSITORY = "thebtf/netcoredbg-mcp"
VERSION = "1.2.3-preview.4"
TAG = f"stateless-preview-v{VERSION}"
EXECUTABLE_NAME = "netcoredbg-mcp-stateless-preview.exe"
AUTHORITY_RULES = [
    "AGENTS_POLICY",
    "CONTRIBUTING_POLICY",
    "RELEASE_PROTOCOL",
    "ADR_004_STATELESS_PREVIEW",
    "A1_PREVIEW_ARTIFACT_SPEC",
]

assert ARTIFACT_CONTRACT_PATH.is_file(), (
    "T005 preview artifact contract is missing: scripts/stateless_preview_artifact.py"
)

assemble_candidate_identity = artifact_contract.assemble_candidate_identity
verify_retained_artifact = artifact_contract.verify_retained_artifact
resolve_release_gate_catalog = artifact_contract.resolve_release_gate_catalog
admit_build = artifact_contract.admit_build
prepare_preview_payload = artifact_contract.prepare_preview_payload
seal_build_records = artifact_contract.seal_build_records
parse_args = artifact_contract.parse_args
seal_artifact_consumer_proof = artifact_contract.seal_artifact_consumer_proof
consumer_proof_scenario_catalog = artifact_contract.consumer_proof_scenario_catalog

PREVIEW_ARTIFACT_VALIDATOR_PATH = (
    PROJECT_ROOT / "tests" / "preview" / "validate_preview_artifact.py"
)

assert PREVIEW_ARTIFACT_VALIDATOR_PATH.is_file(), (
    "T011 retained-artifact validator is missing: tests/preview/validate_preview_artifact.py"
)

_validator_spec = importlib.util.spec_from_file_location(
    "preview_artifact_validator",
    PREVIEW_ARTIFACT_VALIDATOR_PATH,
)
assert _validator_spec is not None and _validator_spec.loader is not None
preview_validator = importlib.util.module_from_spec(_validator_spec)
sys.modules[_validator_spec.name] = preview_validator
_validator_spec.loader.exec_module(preview_validator)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _authority_paths() -> tuple[str, ...]:
    catalog_text = RELEASE_GATE_CATALOG_CONTRACT.read_text(encoding="utf-8")
    marker = "The catalog snapshots these tracked files at that exact commit:"
    section = catalog_text.split(marker, maxsplit=1)[1].split(
        "An untracked `.agent` file",
        maxsplit=1,
    )[0]
    paths = tuple(re.findall(r"^\d+\. `([^`]+)`$", section, flags=re.MULTILINE))

    assert paths, "release-gate catalog declares no tracked policy authorities"
    return paths


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _create_authority_repository(
    tmp_path: Path,
) -> tuple[Path, str, list[dict[str, str]]]:
    origin = tmp_path / "origin.git"
    authority_root = tmp_path / "authority-root"
    _git(tmp_path, "init", "--bare", str(origin))
    authority_root.mkdir()
    _git(authority_root, "init")
    _git(authority_root, "config", "user.email", "artifact-tests@example.test")
    _git(authority_root, "config", "user.name", "Artifact Tests")
    _git(authority_root, "checkout", "-b", "main")

    policy_payloads: dict[str, bytes] = {}
    for relative_path in _authority_paths():
        source = PROJECT_ROOT / relative_path
        target = authority_root / relative_path
        assert source.is_file(), f"missing authority source: {relative_path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(payload)
        policy_payloads[relative_path] = payload

    _git(authority_root, "add", ".")
    _git(authority_root, "commit", "-m", "snapshot release authorities")
    source_commit = _git(authority_root, "rev-parse", "HEAD")
    _git(authority_root, "remote", "add", "origin", str(origin))
    _git(authority_root, "push", "--set-upstream", "origin", "main")

    snapshots = [
        {
            "path": relative_path,
            "sha256": _sha256_bytes(policy_payloads[relative_path]),
            "source_commit": source_commit,
        }
        for relative_path in _authority_paths()
    ]
    return authority_root, source_commit, snapshots


def _create_candidate_input(
    tmp_path: Path,
    source_commit: str,
) -> tuple[dict[str, Any], Path, Path, Path]:
    retained_root = tmp_path / "retained"
    retained_root.mkdir()
    archive_name = f"netcoredbg-mcp-stateless-preview-win-x64-{VERSION}.zip"
    manifest_name = f"netcoredbg-mcp-stateless-preview-win-x64-{VERSION}.manifest.json"
    archive_path = retained_root / archive_name
    manifest_path = retained_root / manifest_name
    executable_path = retained_root / EXECUTABLE_NAME
    executable_bytes = b"MZ preview artifact executable\r\n"
    executable_path.write_bytes(executable_bytes)

    entry = zipfile.ZipInfo(EXECUTABLE_NAME, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_STORED
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(entry, executable_bytes)

    manifest_contents = {
        "schema_version": "1.0",
        "version": VERSION,
        "tag": TAG,
        "commit": source_commit,
        "rid": "win-x64",
        "archive": {
            "name": archive_name,
            "size_bytes": archive_path.stat().st_size,
            "sha256": _sha256_path(archive_path),
        },
        "executable": {
            "name": EXECUTABLE_NAME,
            "size_bytes": len(executable_bytes),
            "sha256": _sha256_bytes(executable_bytes),
        },
    }
    manifest_bytes = (
        json.dumps(manifest_contents, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    manifest_path.write_bytes(manifest_bytes)
    manifest_file = {
        "name": manifest_name,
        "size_bytes": len(manifest_bytes),
        "sha256": _sha256_bytes(manifest_bytes),
    }
    payload_artifact = {
        "id": "501",
        "name": f"stateless-preview-{VERSION}",
        "sha256": _sha256_bytes(archive_path.read_bytes() + manifest_bytes),
        "retention": {
            "configured_days": 7,
            "expires_at": "2030-01-01T00:00:00Z",
        },
    }
    manifest_reference = {
        "repository": REPOSITORY,
        "run_id": "101",
        "artifact_id": "501",
        "path": manifest_name,
        "sha256": manifest_file["sha256"],
    }
    archive_reference = {
        "repository": REPOSITORY,
        "run_id": "101",
        "artifact_id": "501",
        "path": archive_name,
        "sha256": manifest_contents["archive"]["sha256"],
    }
    return (
        {
            "schema_version": "1.0",
            "candidate": {
                "source": {
                    "repository": REPOSITORY,
                    "ref": SOURCE_REF,
                    "commit": source_commit,
                    "origin_main_target": source_commit,
                },
                "build": {
                    "repository": REPOSITORY,
                    "workflow_path": ".github/workflows/stateless-preview.yml",
                    "mode": "build",
                    "event": "workflow_dispatch",
                    "run_id": "101",
                    "run_attempt": 1,
                    "ref": SOURCE_REF,
                    "commit": source_commit,
                    "artifact": payload_artifact,
                },
                "preview_manifest": {
                    "file": manifest_file,
                    "manifest_reference": manifest_reference,
                    "archive_reference": archive_reference,
                    "contents": manifest_contents,
                },
                "destination": {
                    "provider": "github",
                    "repository": REPOSITORY,
                    "tag": TAG,
                    "prerelease": True,
                },
            },
        },
        archive_path,
        manifest_path,
        executable_path,
    )


def _fixed_gate_descriptors() -> list[dict[str, Any]]:
    requirements = {
        "retained-downloaded-consumer-proof": "RETAINED_ARTIFACT_PROOF",
        "s2-s3-seven-lens-evidence": "S2_S3_SEVEN_LENS_AGGREGATE",
        "independent-review": "INDEPENDENT_PR_REVIEW",
        "remote-downloaded-consumer-proof": "REMOTE_CONSUMER_PROOF",
    }
    rows = [
        ("pre-decision", "retained-downloaded-consumer-proof", "artifact-consumer-proof"),
        ("pre-decision", "s2-s3-seven-lens-evidence", "s2-s3-seven-lens-aggregate"),
        ("pre-decision", "independent-review", "independent-pr-review"),
        ("post-publication", "remote-downloaded-consumer-proof", "artifact-consumer-proof"),
    ]
    return [
        {
            "stage": stage,
            "gate_id": gate_id,
            "record_type": record_type,
            "authority_rules": AUTHORITY_RULES,
            "evidence_requirements": [
                "CANDIDATE_IDENTITY_MATCH",
                "CANONICAL_MAIN_PROVENANCE",
                "PASSING_OUTCOME",
                requirements[gate_id],
            ],
        }
        for stage, gate_id, record_type in rows
    ]


def test_assemble_candidate_identity_binds_one_canonical_main_commit(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)

    identity = assemble_candidate_identity(_freeze(candidate_input))

    assert identity["candidate"]["source"] == candidate_input["candidate"]["source"]


@pytest.mark.parametrize(
    "violation",
    [
        "wrong_source_ref",
        "different_build_commit",
        "untrusted_build_event",
        "different_origin_main_target",
    ],
)
def test_assemble_candidate_identity_refuses_noncanonical_provenance(
    tmp_path: Path,
    violation: str,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    candidate = candidate_input["candidate"]

    if violation == "wrong_source_ref":
        candidate["source"]["ref"] = "refs/heads/preview"
    elif violation == "different_build_commit":
        candidate["build"]["commit"] = "b" * 40
    elif violation == "untrusted_build_event":
        candidate["build"]["event"] = "push"
    else:
        candidate["source"]["origin_main_target"] = "c" * 40

    frozen_candidate_input = _freeze(candidate_input)
    with pytest.raises(ValueError):
        assemble_candidate_identity(frozen_candidate_input)


def test_verify_retained_artifact_replays_all_recorded_hash_equations(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, archive_path, manifest_path, executable_path = _create_candidate_input(
        tmp_path,
        source_commit,
    )
    identity = assemble_candidate_identity(_freeze(candidate_input))

    verification = verify_retained_artifact(
        identity,
        archive_path,
        manifest_path,
        executable_path,
    )

    assert verification == {
        "source_ref": SOURCE_REF,
        "source_commit": source_commit,
        "archive": candidate_input["candidate"]["preview_manifest"]["contents"]["archive"],
        "manifest": candidate_input["candidate"]["preview_manifest"]["file"],
        "executable": candidate_input["candidate"]["preview_manifest"]["contents"]["executable"],
    }


@pytest.mark.parametrize("changed_file", ["archive", "manifest", "executable"])
def test_verify_retained_artifact_refuses_any_recorded_hash_mismatch(
    tmp_path: Path,
    changed_file: str,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, archive_path, manifest_path, executable_path = _create_candidate_input(
        tmp_path,
        source_commit,
    )
    identity = assemble_candidate_identity(_freeze(candidate_input))
    changed_path = {
        "archive": archive_path,
        "manifest": manifest_path,
        "executable": executable_path,
    }[changed_file]
    changed_path.write_bytes(changed_path.read_bytes() + b"\nchanged")

    with pytest.raises(ValueError):
        verify_retained_artifact(identity, archive_path, manifest_path, executable_path)


def test_resolve_release_gate_catalog_snapshots_exact_authorities_and_fixed_gates(
    tmp_path: Path,
) -> None:
    authority_root, source_commit, snapshots = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    identity = assemble_candidate_identity(_freeze(candidate_input))
    (authority_root / "docs" / "RELEASE-PROTOCOL.md").write_text(
        "later working-tree policy drift\n",
        encoding="utf-8",
    )
    caller_catalog = authority_root / ".agent" / "caller-release-gate-catalog.json"
    caller_catalog.parent.mkdir()
    caller_catalog.write_text('{"gate_descriptors": []}\n', encoding="utf-8")

    catalog = resolve_release_gate_catalog(identity, authority_root)

    assert {
        "producer": catalog["catalog"]["producer"],
        "source_ref": catalog["catalog"]["source_ref"],
        "source_commit": catalog["catalog"]["source_commit"],
        "policy_authority_snapshots": catalog["catalog"]["policy_authority_snapshots"],
        "gate_descriptors": catalog["catalog"]["gate_descriptors"],
    } == {
        "producer": {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "resolve_release_gate_catalog",
        },
        "source_ref": SOURCE_REF,
        "source_commit": source_commit,
        "policy_authority_snapshots": snapshots,
        "gate_descriptors": _fixed_gate_descriptors(),
    }


@pytest.mark.parametrize("mutation", [None, "wrong_source_ref", "missing_descriptor", "sonar_gate"])
def test_resolved_release_gate_catalog_validates_the_whole_local_schema(
    tmp_path: Path,
    mutation: str | None,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    identity = assemble_candidate_identity(candidate_input)
    catalog = json.loads(json.dumps(resolve_release_gate_catalog(identity, authority_root)))
    schema_path = RELEASE_GATE_CATALOG_CONTRACT.with_suffix(".schema.json")
    schema_paths = (
        *schema_path.parent.glob("*.schema.json"),
        *(PROJECT_ROOT / "specs/005-stateless-preview/contracts").glob("*.schema.json"),
    )
    registry = Registry().with_resources(
        (
            path.resolve().as_uri(),
            Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))),
        )
        for path in schema_paths
    )
    validator = Draft202012Validator(
        {"$ref": schema_path.resolve().as_uri()},
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )

    if mutation == "wrong_source_ref":
        catalog["catalog"]["source_ref"] = "refs/heads/preview"
    elif mutation == "missing_descriptor":
        catalog["catalog"]["gate_descriptors"].pop()
    elif mutation == "sonar_gate":
        catalog["catalog"]["gate_descriptors"][0]["gate_id"] = "candidate-exact-head-sonar"

    if mutation is None:
        validator.validate(catalog)
    else:
        with pytest.raises(ValidationError):
            validator.validate(catalog)


@pytest.mark.parametrize("field", ["manifest_reference", "archive_reference"])
def test_release_gate_catalog_refuses_missing_functional_file_reference(
    tmp_path: Path,
    field: str,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    del candidate_input["candidate"]["preview_manifest"][field]

    with pytest.raises(ValueError, match="unsupported or missing fields"):
        resolve_release_gate_catalog(candidate_input, authority_root)


def test_assemble_candidate_identity_refuses_a_caller_supplied_gate_catalog(
    tmp_path: Path,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    candidate_input["release_gate_catalog"] = {"gate_descriptors": []}

    frozen_candidate_input = _freeze(candidate_input)
    with pytest.raises(ValueError):
        assemble_candidate_identity(frozen_candidate_input)


def _build_environment(source_commit: str) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": SOURCE_REF,
        "GITHUB_SHA": source_commit,
        "GITHUB_RUN_ID": "101",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/stateless-preview.yml@{SOURCE_REF}",
        "A1_PREVIEW_VERSION": VERSION,
        "A1_PREVIEW_TAG": TAG,
        "A1_RETENTION_DAYS": "7",
    }


def _write_post_merge_scan_receipt(
    repository_root: Path,
    source_commit: str,
    *,
    outcome: str = "PASS",
    captured_head: str | None = None,
) -> Path:
    path = (
        repository_root
        / ".agent"
        / "e"
        / "sonarqube"
        / "thebtf_netcoredbg_mcp"
        / source_commit
        / "post-merge.json"
    )
    path.parent.mkdir(parents=True)
    receipt = (
        _blocked_v3_exact_head_receipt(
            source_commit,
            role="post-merge",
            release_intent="v0.23.12",
        )
        if outcome == "BLOCKED"
        else _complete_v3_exact_head_receipt(
            source_commit,
            role="post-merge",
            outcome=outcome,
            release_intent="v0.23.12",
        )
    )
    if outcome != "BLOCKED":
        inventory_reference = receipt["global_inventory"]["artifact"]
        inventory_path = repository_root / inventory_reference["relative_path"]
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_bytes(
            _canonical_json_bytes(_complete_inventory_document(source_commit))
        )
    if captured_head is not None:
        receipt["identity"]["captured_head"] = captured_head
    path.write_bytes(_canonical_json_bytes(receipt))
    return path


def _create_publish_output(repository_root: Path) -> Path:
    publish_directory = repository_root / "artifacts" / "stateless-preview" / "publish"
    publish_directory.mkdir(parents=True)
    executable = publish_directory / EXECUTABLE_NAME
    executable.write_bytes(b"MZ self-contained preview executable\r\n")
    return executable


def test_admit_build_derives_canonical_main_without_source_input(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)

    admission = admit_build(authority_root, _build_environment(source_commit))

    assert admission["source"] == {
        "repository": REPOSITORY,
        "ref": SOURCE_REF,
        "commit": source_commit,
    }
    assert admission["preview"] == {
        "version": VERSION,
        "tag": TAG,
        "retention_days": 7,
    }
    assert set(admission["artifacts"]) == {
        "payload",
        "candidate_identity",
        "release_gate_catalog",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("GITHUB_REF", "refs/heads/unmerged-preview"),
        ("GITHUB_SHA", "b" * 40),
        (
            "GITHUB_WORKFLOW_REF",
            f"{REPOSITORY}/.github/workflows/foreign.yml@{SOURCE_REF}",
        ),
        ("A1_PREVIEW_TAG", "stateless-preview-v9.9.9-preview.9"),
        ("A1_RETENTION_DAYS", "91"),
    ],
)
def test_admit_build_refuses_noncanonical_or_unbounded_inputs(
    tmp_path: Path, field: str, value: str
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    environment = _build_environment(source_commit)
    environment[field] = value

    with pytest.raises(ValueError):
        admit_build(authority_root, environment)


def test_coverage_preview_fixture_builder_emits_verified_archive_and_manifest(
    tmp_path: Path,
) -> None:
    executable = tmp_path / EXECUTABLE_NAME
    executable.write_bytes(b"source-run-preview")
    output = tmp_path / "artifact"
    script = PROJECT_ROOT / "build" / "prepare_preview_fixture.py"

    subprocess.run(
        [
            sys.executable,
            str(script),
            "--executable",
            str(executable),
            "--output",
            str(output),
            "--commit",
            "a" * 40,
        ],
        check=True,
    )

    manifest_path = (
        output / "netcoredbg-mcp-stateless-preview-win-x64-0.0.0-preview.1.manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_path = output / manifest["archive"]["name"]
    assert _sha256_path(archive_path) == manifest["archive"]["sha256"]
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [EXECUTABLE_NAME]
        assert archive.read(EXECUTABLE_NAME) == executable.read_bytes()


def test_prepare_preview_payload_seals_the_inherited_manifest_equations(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    executable = _create_publish_output(authority_root)

    payload = prepare_preview_payload(authority_root, _build_environment(source_commit))

    archive_path = (
        authority_root / "artifacts" / "stateless-preview" / "payload" / payload["archive"]["name"]
    )
    manifest_path = (
        authority_root / "artifacts" / "stateless-preview" / "payload" / payload["manifest"]["name"]
    )
    assert payload["manifest"]["sha256"] == _sha256_path(manifest_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [EXECUTABLE_NAME]
        assert archive.read(EXECUTABLE_NAME) == executable.read_bytes()


@pytest.mark.parametrize("audit_outcome", [None, "BLOCKED"])
def test_seal_build_records_admits_payload_without_passing_sonar(
    tmp_path: Path,
    audit_outcome: str | None,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    _create_publish_output(authority_root)
    environment = _build_environment(source_commit)
    admission = admit_build(authority_root, environment)
    prepare_preview_payload(authority_root, environment)
    audit_path = None
    audit_bytes = None
    if audit_outcome is not None:
        audit_path = _write_post_merge_scan_receipt(
            authority_root, source_commit, outcome=audit_outcome
        )
        audit_bytes = audit_path.read_bytes()
    environment.update(
        {
            "A1_PAYLOAD_ARTIFACT_ID": "501",
            "GITHUB_TOKEN": "test-token",
        }
    )

    def artifact_reader(_repository: str, artifact_id: str, _token: str) -> Mapping[str, Any]:
        return {
            "id": int(artifact_id),
            "name": admission["artifacts"]["payload"],
            "expired": False,
            "expires_at": "2030-01-01T00:00:00Z",
            "workflow_run": {"id": 101},
        }

    sealed = seal_build_records(
        authority_root,
        environment,
        artifact_reader=artifact_reader,
    )

    candidate = sealed["candidate_identity"]
    assert candidate["candidate"]["build"]["artifact"]["id"] == "501"
    assert candidate["candidate"]["source"] == {
        "repository": REPOSITORY,
        "ref": SOURCE_REF,
        "commit": source_commit,
        "origin_main_target": source_commit,
    }
    if audit_path is not None:
        assert audit_path.read_bytes() == audit_bytes
    assert sealed["release_gate_catalog"]["catalog"]["source_commit"] == source_commit
    assert (
        authority_root / "artifacts" / "stateless-preview" / "records" / "candidate-identity.json"
    ).is_file()
    assert (
        authority_root / "artifacts" / "stateless-preview" / "records" / "release-gate-catalog.json"
    ).is_file()


def test_seal_build_records_refuses_an_artifact_from_another_run(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    _create_publish_output(authority_root)
    environment = _build_environment(source_commit)
    prepare_preview_payload(authority_root, environment)
    environment.update(
        {
            "A1_PAYLOAD_ARTIFACT_ID": "501",
            "GITHUB_TOKEN": "test-token",
        }
    )

    def artifact_reader(_repository: str, artifact_id: str, _token: str) -> Mapping[str, Any]:
        return {
            "id": int(artifact_id),
            "name": "foreign-artifact",
            "expired": False,
            "expires_at": "2030-01-01T00:00:00Z",
            "workflow_run": {"id": 102},
        }

    with pytest.raises(ValueError):
        seal_build_records(authority_root, environment, artifact_reader=artifact_reader)


@pytest.mark.parametrize(
    "command",
    (
        "admit-build",
        "prepare-payload",
        "seal-build-records",
    ),
)
def test_build_cli_exposes_only_defined_foundation_operations(command: str) -> None:
    assert parse_args([command]).command == command

    with pytest.raises(SystemExit):
        parse_args([command, "--source", "b" * 40])


def test_build_cli_refuses_the_removed_sonar_receipt_operation() -> None:
    with pytest.raises(SystemExit):
        parse_args(["produce-post-merge-receipt"])


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )


def _consumer_proof_reference(
    *,
    artifact_id: str,
    artifact_name: str,
    path: str,
    sha256: str,
    run_id: str = "101",
) -> dict[str, Any]:
    return {
        "repository": REPOSITORY,
        "run_id": run_id,
        "artifact": {
            "id": artifact_id,
            "name": artifact_name,
            "sha256": _sha256_bytes(f"artifact-{artifact_id}".encode()),
            "retention": {
                "configured_days": 7,
                "expires_at": "2030-01-01T00:00:00Z",
            },
        },
        "path": path,
        "sha256": sha256,
    }


@pytest.mark.parametrize("include_sonar_reference", [False, True])
def test_candidate_reference_bundle_requires_only_functional_artifacts(
    tmp_path: Path,
    include_sonar_reference: bool,
) -> None:
    reference = _consumer_proof_reference(
        artifact_id="501", artifact_name="retained-preview", path="payload.zip", sha256="a" * 64
    )
    bundle = {
        "reference_schema_version": "1.0",
        "candidate_identity": reference,
        "archive": reference,
        "manifest": {**reference, "path": "payload.manifest.json"},
        "release_gate_catalog": {**reference, "path": "release-gate-catalog.json"},
    }
    if include_sonar_reference:
        bundle["receipt_provenance"] = {**reference, "path": "post-merge-exact-head.json"}
    path = tmp_path / "candidate-references.json"
    path.write_bytes(_canonical_json_bytes(bundle))

    if include_sonar_reference:
        with pytest.raises(ValueError, match="unsupported or missing fields"):
            preview_validator.CandidateReferences.load(path)
    else:
        references = preview_validator.CandidateReferences.load(path)
        assert references.archive.same_artifact(references.manifest)
        assert references.candidate_identity.as_mapping() == reference


def _valid_retained_consumer_proof(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, Any], dict[str, Any]]:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    identity = json.loads(json.dumps(assemble_candidate_identity(candidate_input)))
    identity_bytes = _canonical_json_bytes(identity)
    identity_reference = _consumer_proof_reference(
        artifact_id="601",
        artifact_name="stateless-preview-candidate-identity",
        path="candidate-identity.json",
        sha256=_sha256_bytes(identity_bytes),
        run_id="601",
    )
    catalog = json.loads(json.dumps(resolve_release_gate_catalog(identity, authority_root)))
    catalog_definition = consumer_proof_scenario_catalog()
    scenarios = catalog_definition["scenarios"]
    scenario_ids = [scenario["scenario_id"] for scenario in scenarios]
    observed_outcomes = {
        "EXPECT_SUCCESS": "SUCCESS",
        "EXPECT_FAILURE": "FAILURE",
        "EXPECT_REFUSAL": "REFUSAL",
    }
    receipt = {
        "receipt_schema_version": "1.0",
        "receipt_id": "retained-proof-001",
        "candidate_identity_record": identity_reference,
        "candidate": identity["candidate"],
        "proof_stage": "retained_artifact",
        "download_origin": {
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/stateless-preview.yml",
            "run_id": "101",
            "artifact": identity["candidate"]["build"]["artifact"],
            "archive_path": identity["candidate"]["preview_manifest"]["archive_reference"]["path"],
            "manifest_path": identity["candidate"]["preview_manifest"]["manifest_reference"][
                "path"
            ],
        },
        "input_identity_results": {
            "archive": identity["candidate"]["preview_manifest"]["contents"]["archive"],
            "manifest": identity["candidate"]["preview_manifest"]["file"],
            "executable": identity["candidate"]["preview_manifest"]["contents"]["executable"],
            "archive_matches_candidate": True,
            "manifest_matches_candidate": True,
            "executable_matches_manifest": True,
            "inherited_verifier_equations_pass": True,
        },
        "fixture_identity": {
            "fixture_id": "preview-search-fixture-v1",
            "fixture_sha256": "f" * 64,
            "scenario_catalog_id": catalog_definition["scenario_catalog_id"],
            "scenario_catalog_sha256": catalog_definition["scenario_catalog_sha256"],
        },
        "scenario_matrix": {
            "required_scenario_ids": scenario_ids,
            "executed_scenario_ids": scenario_ids,
            "required_count": len(scenario_ids),
            "executed_count": len(scenario_ids),
            "missing_scenario_ids": [],
            "unexpected_scenario_ids": [],
            "results": [
                {
                    "scenario_id": scenario["scenario_id"],
                    "surface": scenario["surface"],
                    "documented_outcome": scenario["documented_outcome"],
                    "observed_outcome": observed_outcomes[scenario["documented_outcome"]],
                    "status": "PASS",
                    "no_partial_output": True,
                    "no_unintended_side_effect": True,
                }
                for scenario in scenarios
            ],
            "outcome": "PASS",
        },
        "runtime_results": {
            "explicit_project_argument": True,
            "catalog": ["find_code_symbol"],
            "catalog_is_closed": True,
            "valid_journey_passed": True,
            "stdout_jsonrpc_only": True,
            "clean_eof": {
                "stdin_closed": True,
                "exited_cleanly": True,
                "cancellation_result_emitted": False,
                "state_retained_after_exit": False,
            },
        },
        "python_rollback_result": {
            "only_preview_selection_removed": True,
            "python_package_reinstalled": False,
            "python_package_replaced": False,
            "console_entrypoint_changed": False,
            "default_selector_changed": False,
            "legacy_journey_outcome": "PRODUCT_WORKS",
        },
        "outcome": "PASS",
        "recorded_at": "2030-01-01T00:00:00Z",
    }
    return receipt, identity, identity_bytes, identity_reference, catalog


def _seal_valid_retained_consumer_proof(tmp_path: Path) -> tuple[dict[str, Any], Any]:
    receipt, identity, identity_bytes, identity_reference, catalog = _valid_retained_consumer_proof(
        tmp_path
    )
    sealed = seal_artifact_consumer_proof(
        receipt,
        candidate_identity=identity,
        candidate_identity_bytes=identity_bytes,
        candidate_identity_reference=identity_reference,
        release_gate_catalog=catalog,
    )
    return receipt, sealed


def test_seal_retained_consumer_proof_binds_downloaded_candidate_and_full_matrix(
    tmp_path: Path,
) -> None:
    receipt, sealed = _seal_valid_retained_consumer_proof(tmp_path)

    assert sealed["candidate"] == receipt["candidate"]
    assert sealed["candidate_identity_record"] == receipt["candidate_identity_record"]
    assert sealed["scenario_matrix"]["required_count"] > 0
    assert (
        sealed["scenario_matrix"]["required_count"] == sealed["scenario_matrix"]["executed_count"]
    )
    with pytest.raises(TypeError):
        sealed["outcome"] = "FAIL"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda receipt: receipt["input_identity_results"]["archive"].update(
                {"sha256": "0" * 64}
            ),
            "archive",
        ),
        (
            lambda receipt: receipt["scenario_matrix"]["results"].pop(),
            "scenario matrix",
        ),
        (
            lambda receipt: receipt["scenario_matrix"].update(
                {
                    "required_scenario_ids": [],
                    "executed_scenario_ids": [],
                    "required_count": 0,
                    "executed_count": 0,
                    "results": [],
                }
            ),
            "scenario matrix",
        ),
        (
            lambda receipt: receipt["fixture_identity"].update(
                {"fixture_id": "C:\\preview\\secret"}
            ),
            "fixture",
        ),
        (
            lambda receipt: receipt.update({"receipt_id": "ghp_disclosed-secret"}),
            "receipt",
        ),
        (
            lambda receipt: receipt["python_rollback_result"].update(
                {"python_package_reinstalled": True}
            ),
            "rollback",
        ),
    ],
)
def test_seal_retained_consumer_proof_refuses_mutated_evidence(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    receipt, identity, identity_bytes, identity_reference, catalog = _valid_retained_consumer_proof(
        tmp_path
    )
    changed = json.loads(json.dumps(receipt))
    mutation(changed)

    with pytest.raises(ValueError, match=message):
        seal_artifact_consumer_proof(
            changed,
            candidate_identity=identity,
            candidate_identity_bytes=identity_bytes,
            candidate_identity_reference=identity_reference,
            release_gate_catalog=catalog,
        )


def test_seal_retained_consumer_proof_refuses_local_rebuild_and_identity_substitution(
    tmp_path: Path,
) -> None:
    receipt, identity, identity_bytes, identity_reference, catalog = _valid_retained_consumer_proof(
        tmp_path
    )
    local_rebuild = json.loads(json.dumps(receipt))
    local_rebuild["download_origin"]["archive_path"] = "C:/preview/local-build.zip"

    with pytest.raises(ValueError, match="download origin"):
        seal_artifact_consumer_proof(
            local_rebuild,
            candidate_identity=identity,
            candidate_identity_bytes=identity_bytes,
            candidate_identity_reference=identity_reference,
            release_gate_catalog=catalog,
        )

    with pytest.raises(ValueError, match="candidate identity bytes"):
        seal_artifact_consumer_proof(
            receipt,
            candidate_identity=identity,
            candidate_identity_bytes=b"{}\n",
            candidate_identity_reference=identity_reference,
            release_gate_catalog=catalog,
        )


def test_seal_retained_consumer_proof_refuses_catalog_from_other_candidate(tmp_path: Path) -> None:
    receipt, identity, identity_bytes, identity_reference, catalog = _valid_retained_consumer_proof(
        tmp_path
    )
    mismatched_catalog = json.loads(json.dumps(catalog))
    mismatched_catalog["catalog"]["source_commit"] = "a" * 40

    with pytest.raises(ValueError, match="catalog"):
        seal_artifact_consumer_proof(
            receipt,
            candidate_identity=identity,
            candidate_identity_bytes=identity_bytes,
            candidate_identity_reference=identity_reference,
            release_gate_catalog=mismatched_catalog,
        )


@pytest.mark.parametrize(
    "gate_id",
    [
        "retained-downloaded-consumer-proof",
        "s2-s3-seven-lens-evidence",
        "independent-review",
        "remote-downloaded-consumer-proof",
    ],
)
def test_seal_retained_consumer_proof_requires_every_functional_release_gate(
    tmp_path: Path,
    gate_id: str,
) -> None:
    receipt, identity, identity_bytes, identity_reference, catalog = _valid_retained_consumer_proof(
        tmp_path
    )
    catalog["catalog"]["gate_descriptors"] = [
        descriptor
        for descriptor in catalog["catalog"]["gate_descriptors"]
        if descriptor["gate_id"] != gate_id
    ]

    with pytest.raises(ValueError, match="descriptor closure"):
        seal_artifact_consumer_proof(
            receipt,
            candidate_identity=identity,
            candidate_identity_bytes=identity_bytes,
            candidate_identity_reference=identity_reference,
            release_gate_catalog=catalog,
        )


_S2_S3_LENSES = (
    "dependency-cve",
    "dotnet-unsafe-pattern",
    "owasp-input-output-path",
    "attack-surface",
    "secret-asset-manifest",
    "path-process-fail-closed",
    "exploitability",
)
_REVIEW_SEALERS = (
    ("seal_s2_s3_lens_result", "lens"),
    ("seal_s2_s3_review_aggregate", "aggregate"),
    ("seal_independent_pr_review", "pr_review"),
)


def _simulated_review_checks(count: int) -> list[dict[str, Any]]:
    return [
        {
            "check_id": f"simulated-check-{index:03d}",
            "result": {"expected": "EXPECT_SUCCESS", "observed": "SUCCESS"},
            "status": "PASS",
        }
        for index in range(1, count + 1)
    ]


def _simulated_review_inputs(tmp_path: Path) -> dict[str, Any]:
    # Simulated validator inputs only; these are never reviewer or acceptance receipts.
    consumer, identity, identity_bytes, consumer_reference, catalog = (
        _valid_retained_consumer_proof(tmp_path)
    )
    identity_reference = {
        "repository": consumer_reference["repository"],
        "run_id": consumer_reference["run_id"],
        "artifact_id": consumer_reference["artifact"]["id"],
        "path": consumer_reference["path"],
        "sha256": consumer_reference["sha256"],
    }
    binding = {
        "candidate_identity_record": identity_reference,
        "candidate_identity_record_sha256": _sha256_bytes(identity_bytes),
        "candidate": identity["candidate"],
    }
    review = {
        **binding,
        "reviewer_provenance": {
            "reviewer_principal_id": "simulated-reviewer",
            "reviewer_role": "SECURITY_REVIEWER",
            "independence_basis": "DISTINCT_REVIEWER_AND_PRODUCER",
        },
        "producer_provenance": {
            "producer_principal_id": "simulated-producer",
            "operation": "seal_s2_s3_lens_result",
        },
        "denominator": {"required_evidence_count": 1, "completed_evidence_count": 1},
        "checks": _simulated_review_checks(1),
        "findings": {
            "records": [
                {
                    "finding_id": "simulated-evidence-001",
                    "severity": "INFO",
                    "disposition": "NOT_APPLICABLE",
                    "observed_result": "SUCCESS",
                }
            ],
            "unresolved_high_severity_count": 0,
        },
        "result": {"expected": "EXPECT_SUCCESS", "observed": "SUCCESS"},
        "outcome": "PASS",
        "recorded_at": "2030-01-01T00:00:00Z",
    }
    lenses = [
        {**review, "lens": lens, "lens_order": order}
        for order, lens in enumerate(_S2_S3_LENSES, start=1)
    ]
    aggregate = {
        "aggregate_schema_version": "1.0",
        "aggregate_id": "simulated-aggregate-001",
        "aggregate_producer": {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "seal_s2_s3_review_aggregate",
        },
        **binding,
        "lens_results": lenses,
        "coverage_summary": {
            "required_lens_count": 7,
            "completed_lens_count": 7,
            "unresolved_high_severity_count": 0,
        },
        "outcome": "PASS",
        "sealed_at": "2030-01-01T00:00:00Z",
    }
    pr_review = {
        **review,
        "receipt_schema_version": "1.0",
        "receipt_id": "simulated-pr-review-001",
        "review_target": {
            "repository": REPOSITORY,
            "pull_request_id": "801",
            "reviewed_commit": identity["candidate"]["source"]["commit"],
            "base_ref": SOURCE_REF,
        },
        "reviewer_provenance": {
            "reviewer_principal_id": "simulated-pr-reviewer",
            "reviewed_author_principal_id": "simulated-author",
            "independence_basis": "DISTINCT_REVIEWER_AND_AUTHOR",
        },
        "producer_provenance": {
            "producer_principal_id": "simulated-producer",
            "operation": "seal_independent_pr_review",
        },
    }
    inputs = json.loads(
        json.dumps(
            {
                "candidate_identity": identity,
                "candidate_identity_reference": identity_reference,
                "consumer_identity_reference": consumer_reference,
                "consumer": consumer,
                "catalog": catalog,
                "lens": lenses[0],
                "aggregate": aggregate,
                "pr_review": pr_review,
            }
        )
    )
    inputs["candidate_identity_bytes"] = identity_bytes
    inputs["repository_root"] = tmp_path / "authority-root"
    return inputs


@pytest.fixture(scope="module")
def _semantic_review_seed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _simulated_review_inputs(tmp_path_factory.mktemp("simulated-review-source"))


@pytest.fixture
def semantic_review_inputs(_semantic_review_seed: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(_semantic_review_seed)


def _review_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_identity": inputs["candidate_identity"],
        "candidate_identity_bytes": inputs["candidate_identity_bytes"],
        "candidate_identity_reference": inputs["consumer_identity_reference"],
    }


def _validate_review_schema(value: Mapping[str, Any], schema_reference: str) -> None:
    contracts = RELEASE_GATE_CATALOG_CONTRACT.parent
    schema_name, _, fragment = schema_reference.partition("#")
    schema_paths = (
        *contracts.glob("*.schema.json"),
        *(PROJECT_ROOT / "specs/005-stateless-preview/contracts").glob("*.schema.json"),
    )
    registry = Registry().with_resources(
        (
            path.resolve().as_uri(),
            Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))),
        )
        for path in schema_paths
    )
    Draft202012Validator(
        {
            "$ref": (contracts / schema_name).resolve().as_uri()
            + (f"#{fragment}" if fragment else "")
        },
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(value)


@pytest.mark.parametrize(
    ("operation", "record_key", "schema_reference"),
    [
        (*_REVIEW_SEALERS[0], "s2-s3-review.schema.json#/$defs/dependency_cve_lens"),
        (*_REVIEW_SEALERS[1], "s2-s3-review.schema.json"),
        (*_REVIEW_SEALERS[2], "independent-pr-review.schema.json"),
    ],
)
def test_review_semantics_seal_exact_candidate_and_closed_schema(
    semantic_review_inputs: dict[str, Any], operation: str, record_key: str, schema_reference: str
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    reviews = record["lens_results"] if record_key == "aggregate" else [record]
    for review in reviews:
        review["checks"] = _simulated_review_checks(11)
        review["checks"][-1]["result"] = {"expected": "EXPECT_REFUSAL", "observed": "REFUSAL"}
        review["checks"][-2]["result"] = {"expected": "EXPECT_FAILURE", "observed": "FAILURE"}
        review["denominator"] = {"required_evidence_count": 11, "completed_evidence_count": 11}
        review["findings"]["records"].clear()
    original = deepcopy(record)
    _validate_review_schema(inputs[record_key], schema_reference)

    sealed = getattr(artifact_contract, operation)(inputs[record_key], **_review_binding(inputs))

    _validate_review_schema(sealed, schema_reference)
    assert sealed["candidate"] == inputs["candidate_identity"]["candidate"]
    assert sealed["candidate_identity_record"] == inputs["candidate_identity_reference"]
    assert sealed["candidate_identity_record_sha256"] == _sha256_bytes(
        inputs["candidate_identity_bytes"]
    )
    assert sealed["outcome"] == "PASS"
    assert sealed == original
    assert record == original
    sealed_reviews = sealed["lens_results"] if record_key == "aggregate" else [sealed]
    for review in sealed_reviews:
        assert len(review["checks"]) == 11
        assert review["denominator"] == {
            "required_evidence_count": 11,
            "completed_evidence_count": 11,
        }
        assert not review["findings"]["records"]
        with pytest.raises(TypeError):
            review["checks"][0]["status"] = "INCOMPLETE"
    if record_key == "aggregate":
        assert [row["lens"] for row in sealed["lens_results"]] == list(_S2_S3_LENSES)
        assert sealed["coverage_summary"] == {
            "required_lens_count": 7,
            "completed_lens_count": 7,
            "unresolved_high_severity_count": 0,
        }


@pytest.mark.parametrize(("operation", "record_key"), _REVIEW_SEALERS)
@pytest.mark.parametrize(
    "violation",
    [
        "candidate_source",
        "candidate_artifact",
        "candidate_manifest",
        "identity_repository",
        "identity_run",
        "identity_artifact",
        "identity_path",
        "identity_hash",
        "candidate_hash",
        "raw_identity_bytes",
    ],
)
def test_review_semantics_refuse_stale_or_substituted_candidate(
    semantic_review_inputs: dict[str, Any], operation: str, record_key: str, violation: str
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    if violation == "candidate_source":
        record["candidate"]["source"]["commit"] = "a" * 40
    elif violation == "candidate_artifact":
        record["candidate"]["build"]["artifact"]["id"] = "999"
    elif violation == "candidate_manifest":
        record["candidate"]["preview_manifest"]["file"]["sha256"] = "a" * 64
    elif violation.startswith("identity_"):
        field, replacement = {
            "identity_repository": ("repository", "other/repository"),
            "identity_run": ("run_id", "999"),
            "identity_artifact": ("artifact_id", "999"),
            "identity_path": ("path", "other-candidate.json"),
            "identity_hash": ("sha256", "a" * 64),
        }[violation]
        record["candidate_identity_record"][field] = replacement
    elif violation == "candidate_hash":
        record["candidate_identity_record_sha256"] = "a" * 64
    else:
        inputs["candidate_identity_bytes"] += b" "

    with pytest.raises(ValueError):
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))


@pytest.mark.parametrize(("operation", "record_key"), (_REVIEW_SEALERS[0], _REVIEW_SEALERS[2]))
@pytest.mark.parametrize(
    "violation",
    [
        "zero_denominator",
        "zero_completed_count",
        "incomplete_denominator",
        "overcompleted_denominator",
        "duplicate_finding",
        "hidden_high_finding",
        "hidden_critical_finding",
        "producer_is_reviewer",
        "unredacted_field",
        "absolute_path",
        "uri",
        "credential_shaped_id",
        "control_character",
        "raw_diagnostic_result",
        "mismatched_result",
        "nonpassing_outcome",
    ],
)
def test_review_semantics_refuse_ineligible_rows(
    semantic_review_inputs: dict[str, Any], operation: str, record_key: str, violation: str
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    if violation == "zero_denominator":
        record["denominator"] = {"required_evidence_count": 0, "completed_evidence_count": 0}
    elif violation == "zero_completed_count":
        record["denominator"]["completed_evidence_count"] = 0
    elif violation == "incomplete_denominator":
        record["denominator"]["required_evidence_count"] = 2
    elif violation == "overcompleted_denominator":
        record["denominator"]["completed_evidence_count"] = 2
    elif violation == "duplicate_finding":
        record["findings"]["records"].append(dict(record["findings"]["records"][0]))
    elif violation in ("hidden_high_finding", "hidden_critical_finding"):
        record["findings"]["records"][0].update(
            {
                "severity": "HIGH" if violation == "hidden_high_finding" else "CRITICAL",
                "disposition": "UNRESOLVED",
                "observed_result": "FAILURE",
            }
        )
    elif violation == "producer_is_reviewer":
        record["reviewer_provenance"]["reviewer_principal_id"] = record["producer_provenance"][
            "producer_principal_id"
        ]
    elif violation == "unredacted_field":
        record["findings"]["records"][0]["diagnostic"] = "simulated raw diagnostic"
    elif violation == "mismatched_result":
        record["result"]["observed"] = "FAILURE"
    elif violation == "raw_diagnostic_result":
        record["result"]["observed"] = "simulated raw diagnostic"
    elif violation == "nonpassing_outcome":
        record["outcome"] = "FAIL"
    else:
        record["reviewer_provenance"]["reviewer_principal_id"] = {
            "absolute_path": "C:\\simulated\\private-project",
            "uri": "https://example.invalid/simulated-review",
            "credential_shaped_id": "ghp_simulated-not-a-credential",
            "control_character": "simulated-reviewer\n",
        }[violation]

    with pytest.raises(ValueError):
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))


@pytest.mark.parametrize(
    "violation",
    [
        "missing_lens",
        "extra_lens",
        "duplicate_lens",
        "out_of_order",
        "wrong_lens_order",
        "stale_nested_candidate",
        "nested_identity_reference",
        "nested_candidate_hash",
        "nested_nonindependent_reviewer",
        "nested_zero_denominator",
        "nested_incomplete_denominator",
        "nested_hidden_high_finding",
        "nested_unredacted_field",
        "wrong_required_count",
        "wrong_completed_count",
        "wrong_high_count",
        "wrong_outcome",
    ],
)
def test_review_semantics_aggregate_does_not_trust_claimed_seven_seven_zero(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = semantic_review_inputs
    aggregate = inputs["aggregate"]
    rows = aggregate["lens_results"]
    if violation == "missing_lens":
        rows.pop()
    elif violation == "extra_lens":
        rows.append(json.loads(json.dumps(rows[0])))
    elif violation == "duplicate_lens":
        rows[-1] = json.loads(json.dumps(rows[0]))
    elif violation == "out_of_order":
        rows[0], rows[1] = rows[1], rows[0]
    elif violation == "wrong_lens_order":
        rows[0]["lens_order"] = 2
    elif violation == "stale_nested_candidate":
        rows[0]["candidate"]["build"]["artifact"]["id"] = "999"
    elif violation == "nested_identity_reference":
        rows[0]["candidate_identity_record"]["artifact_id"] = "999"
    elif violation == "nested_candidate_hash":
        rows[0]["candidate_identity_record_sha256"] = "a" * 64
    elif violation == "nested_nonindependent_reviewer":
        rows[0]["reviewer_provenance"]["reviewer_principal_id"] = "simulated-producer"
    elif violation == "nested_zero_denominator":
        rows[0]["denominator"] = {"required_evidence_count": 0, "completed_evidence_count": 0}
    elif violation == "nested_incomplete_denominator":
        rows[0]["denominator"]["required_evidence_count"] = 2
    elif violation == "nested_hidden_high_finding":
        rows[0]["findings"]["records"][0].update(
            {"severity": "HIGH", "disposition": "UNRESOLVED", "observed_result": "FAILURE"}
        )
    elif violation == "wrong_required_count":
        aggregate["coverage_summary"]["required_lens_count"] = 6
    elif violation == "wrong_completed_count":
        aggregate["coverage_summary"]["completed_lens_count"] = 6
    elif violation == "wrong_high_count":
        aggregate["coverage_summary"]["unresolved_high_severity_count"] = 1
    elif violation == "wrong_outcome":
        aggregate["outcome"] = "FAIL"
    else:
        rows[0]["findings"]["records"][0]["diagnostic"] = "simulated raw diagnostic"

    with pytest.raises(ValueError):
        artifact_contract.seal_s2_s3_review_aggregate(aggregate, **_review_binding(inputs))


@pytest.mark.parametrize("violation", ["author_is_reviewer", "wrong_repository", "wrong_commit"])
def test_review_semantics_pr_review_is_independent_and_targets_candidate(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = semantic_review_inputs
    review = inputs["pr_review"]
    if violation == "author_is_reviewer":
        review["reviewer_provenance"]["reviewer_principal_id"] = "simulated-author"
    elif violation == "wrong_repository":
        review["review_target"]["repository"] = "other/repository"
    else:
        review["review_target"]["reviewed_commit"] = "a" * 40

    with pytest.raises(ValueError):
        artifact_contract.seal_independent_pr_review(review, **_review_binding(inputs))


@pytest.mark.parametrize(("operation", "record_key"), _REVIEW_SEALERS)
@pytest.mark.parametrize(
    ("violation", "schema_refuses"),
    [
        ("missing_checks", True),
        ("zero_checks", True),
        ("too_many_checks", True),
        ("checks_not_array", True),
        ("check_not_object", True),
        ("missing_check_id", True),
        ("missing_result", True),
        ("missing_status", True),
        ("missing_expected", True),
        ("missing_observed", True),
        ("duplicate_check", True),
        ("duplicate_id_different_result", False),
        ("failed_check", True),
        ("refused_check", True),
        ("incomplete_check", True),
        ("mismatched_result", False),
        ("check_lookup_contradiction", False),
        ("invalid_status", True),
        ("invalid_expected", True),
        ("invalid_observed", True),
        ("extra_check_field", True),
        ("extra_result_field", True),
        ("unsafe_check_id", True),
    ],
)
def test_review_semantics_check_records_are_closed_complete_and_unique(
    semantic_review_inputs: dict[str, Any],
    operation: str,
    record_key: str,
    violation: str,
    schema_refuses: bool,
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    schema_reference = {
        "lens": "s2-s3-review.schema.json#/$defs/dependency_cve_lens",
        "aggregate": "s2-s3-review.schema.json",
        "pr_review": "independent-pr-review.schema.json",
    }[record_key]
    _validate_review_schema(record, schema_reference)
    assert (
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))["outcome"]
        == "PASS"
    )
    review = record["lens_results"][-1] if record_key == "aggregate" else record
    check = review["checks"][0]
    if violation == "missing_checks":
        del review["checks"]
    elif violation == "zero_checks":
        review["checks"].clear()
    elif violation == "too_many_checks":
        review["checks"] = _simulated_review_checks(129)
    elif violation == "checks_not_array":
        review["checks"] = {}
    elif violation == "check_not_object":
        review["checks"][0] = None
    elif violation in ("missing_check_id", "missing_result", "missing_status"):
        del check[violation.removeprefix("missing_")]
    elif violation in ("missing_expected", "missing_observed"):
        del check["result"][violation.removeprefix("missing_")]
    elif violation in ("duplicate_check", "duplicate_id_different_result"):
        duplicate = deepcopy(check)
        if violation == "duplicate_id_different_result":
            duplicate["result"] = {"expected": "EXPECT_FAILURE", "observed": "FAILURE"}
        review["checks"].append(duplicate)
        review["denominator"] = {"required_evidence_count": 2, "completed_evidence_count": 2}
    elif violation in ("failed_check", "refused_check", "incomplete_check", "invalid_status"):
        check["status"] = {
            "failed_check": "FAIL",
            "refused_check": "REFUSED",
            "incomplete_check": "INCOMPLETE",
            "invalid_status": "CLEAN",
        }[violation]
    elif violation == "mismatched_result":
        check["result"]["observed"] = "FAILURE"
    elif violation == "check_lookup_contradiction":
        check["result"]["expected"] = "EXPECT_REFUSAL"
    elif violation in ("invalid_expected", "invalid_observed"):
        check["result"][violation.removeprefix("invalid_")] = "UNKNOWN"
    elif violation == "extra_check_field":
        check["diagnostic"] = "simulated raw diagnostic"
    elif violation == "extra_result_field":
        check["result"]["diagnostic"] = "simulated raw diagnostic"
    else:
        check["check_id"] = "../simulated-check"

    if schema_refuses:
        with pytest.raises(ValidationError):
            _validate_review_schema(record, schema_reference)
    else:
        _validate_review_schema(record, schema_reference)
    original = deepcopy(record)
    with pytest.raises(ValueError):
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))
    assert record == original


@pytest.mark.parametrize(("operation", "record_key"), _REVIEW_SEALERS)
@pytest.mark.parametrize(
    ("required", "completed", "passing_checks"),
    [
        pytest.param(12, 12, 11, id="overclaim"),
        pytest.param(11, 10, 11, id="false-completed"),
        pytest.param(10, 10, 11, id="underclaim"),
        pytest.param(11, 11, 10, id="partial-checks"),
        pytest.param(True, 11, 11, id="boolean-required"),
        pytest.param(11, True, 11, id="boolean-completed"),
        pytest.param(11.5, 11, 11, id="fractional-required"),
        pytest.param(11, 10.5, 11, id="fractional-completed"),
    ],
)
def test_review_semantics_check_denominator_is_derived_from_completed_checks(
    semantic_review_inputs: dict[str, Any],
    operation: str,
    record_key: str,
    required: int | float,
    completed: int | float,
    passing_checks: int,
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    review = record["lens_results"][-1] if record_key == "aggregate" else record
    review["checks"] = _simulated_review_checks(11)
    review["denominator"] = {"required_evidence_count": 11, "completed_evidence_count": 11}
    assert (
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))["outcome"]
        == "PASS"
    )

    review["denominator"] = {
        "required_evidence_count": required,
        "completed_evidence_count": completed,
    }
    for check in review["checks"][passing_checks:]:
        check["status"] = "INCOMPLETE"
    with pytest.raises(ValueError):
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))


@pytest.mark.parametrize(("operation", "record_key"), _REVIEW_SEALERS)
def test_review_semantics_check_coverage_is_independent_of_finding_count(
    semantic_review_inputs: dict[str, Any],
    operation: str,
    record_key: str,
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    review = record["lens_results"][-1] if record_key == "aggregate" else record
    review["findings"]["records"] = [
        {
            "finding_id": "simulated-resolved-high",
            "severity": "HIGH",
            "disposition": "RESOLVED",
            "observed_result": "SUCCESS",
        },
        {
            "finding_id": "simulated-unresolved-low",
            "severity": "LOW",
            "disposition": "UNRESOLVED",
            "observed_result": "FAILURE",
        },
    ]
    original = deepcopy(record)

    sealed = getattr(artifact_contract, operation)(record, **_review_binding(inputs))

    assert sealed == original
    assert record == original
    sealed_review = sealed["lens_results"][-1] if record_key == "aggregate" else sealed
    assert len(sealed_review["checks"]) == 1
    assert len(sealed_review["findings"]["records"]) == 2
    assert sealed_review["denominator"] == {
        "required_evidence_count": 1,
        "completed_evidence_count": 1,
    }
    assert sealed_review["findings"]["unresolved_high_severity_count"] == 0


@pytest.mark.parametrize(("operation", "record_key"), _REVIEW_SEALERS)
@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_review_semantics_check_completion_cannot_override_unresolved_high_findings(
    semantic_review_inputs: dict[str, Any],
    operation: str,
    record_key: str,
    severity: str,
) -> None:
    inputs = semantic_review_inputs
    record = inputs[record_key]
    assert (
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))["outcome"]
        == "PASS"
    )
    review = record["lens_results"][-1] if record_key == "aggregate" else record
    review["findings"]["records"].append(
        {
            "finding_id": "simulated-blocking-finding",
            "severity": severity,
            "disposition": "UNRESOLVED",
            "observed_result": "FAILURE",
        }
    )
    review["findings"]["unresolved_high_severity_count"] = 1
    if record_key == "aggregate":
        record["coverage_summary"]["unresolved_high_severity_count"] = 1

    with pytest.raises(ValueError):
        getattr(artifact_contract, operation)(record, **_review_binding(inputs))


def _simulated_stage_inputs(inputs: dict[str, Any], stage: str) -> dict[str, Any]:
    remote = json.loads(json.dumps(inputs["consumer"]))
    manifest = inputs["candidate_identity"]["candidate"]["preview_manifest"]
    remote.update(
        {
            "receipt_id": "simulated-remote-proof-001",
            "proof_stage": "remote_release",
            "download_origin": {
                "repository": REPOSITORY,
                "release_id": "901",
                "tag": TAG,
                "prerelease": True,
                "archive_asset": {"id": "902", **manifest["contents"]["archive"]},
                "manifest_asset": {"id": "903", **manifest["file"]},
            },
        }
    )
    inputs["records"] = {
        "retained-downloaded-consumer-proof.json": _canonical_json_bytes(inputs["consumer"]),
        "s2-s3-seven-lens-evidence.json": _canonical_json_bytes(inputs["aggregate"]),
        "independent-review.json": _canonical_json_bytes(inputs["pr_review"]),
        "remote-downloaded-consumer-proof.json": _canonical_json_bytes(remote),
    }

    def reference(path: str, raw: bytes) -> dict[str, Any]:
        return {
            "repository": REPOSITORY,
            "run_id": "801",
            "artifact_id": "802",
            "path": path,
            "sha256": _sha256_bytes(raw),
        }

    def record_resolver(record_reference: Mapping[str, Any]) -> bytes:
        if (
            record_reference["repository"] != REPOSITORY
            or record_reference["run_id"] != "801"
            or record_reference["artifact_id"] != "802"
        ):
            raise ValueError("simulated artifact is unavailable")
        try:
            return inputs["records"][record_reference["path"]]
        except KeyError as error:
            raise ValueError("simulated record is unavailable") from error

    catalog_bytes = _canonical_json_bytes(inputs["catalog"])
    catalog_reference = reference("release-gate-catalog.json", catalog_bytes)
    inputs["release_gate_catalog_bytes"] = catalog_bytes
    inputs["release_gate_catalog_reference"] = catalog_reference
    inputs["record_resolver"] = record_resolver
    inputs["stage_record"] = {
        "evidence_schema_version": "1.0",
        "evidence_set_id": "simulated-stage-evidence-001",
        "producer": {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "seal_stage_gate_evidence",
        },
        "release_gate_catalog": dict(catalog_reference),
        "candidate_identity_record": dict(inputs["candidate_identity_reference"]),
        "candidate_identity_record_sha256": _sha256_bytes(inputs["candidate_identity_bytes"]),
        "candidate": json.loads(json.dumps(inputs["candidate_identity"]["candidate"])),
        "source_ref": SOURCE_REF,
        "source_commit": inputs["candidate_identity"]["candidate"]["source"]["commit"],
        "stage": stage,
        "gate_evidence": [
            {
                **descriptor,
                "record_reference": reference(
                    descriptor["gate_id"] + ".json",
                    inputs["records"][descriptor["gate_id"] + ".json"],
                ),
                "candidate_identity_record_sha256": _sha256_bytes(
                    inputs["candidate_identity_bytes"]
                ),
                "source_commit": inputs["candidate_identity"]["candidate"]["source"]["commit"],
                "outcome": "PASS",
            }
            for descriptor in _fixed_gate_descriptors()
            if descriptor["stage"] == stage
        ],
        "outcome": "PASS",
        "sealed_at": "2030-01-01T00:00:00Z",
    }
    inputs["records"] = {
        entry["record_reference"]["path"]: inputs["records"][entry["record_reference"]["path"]]
        for entry in inputs["stage_record"]["gate_evidence"]
    }
    return inputs


def _stage_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_review_binding(inputs),
        "release_gate_catalog": inputs["catalog"],
        **{
            key: inputs[key]
            for key in (
                "release_gate_catalog_bytes",
                "release_gate_catalog_reference",
                "record_resolver",
                "repository_root",
            )
        },
    }


def _replace_simulated_gate_record(
    inputs: dict[str, Any], gate_index: int, record: Mapping[str, Any]
) -> None:
    reference = inputs["stage_record"]["gate_evidence"][gate_index]["record_reference"]
    raw = _canonical_json_bytes(record)
    inputs["records"][reference["path"]] = raw
    reference["sha256"] = _sha256_bytes(raw)


@pytest.mark.parametrize("stage", ["pre-decision", "pre-publication", "post-publication"])
def test_stage_evidence_semantics_seal_only_ordered_catalog_subset(
    semantic_review_inputs: dict[str, Any], stage: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, stage)
    _validate_review_schema(inputs["stage_record"], "stage-gate-evidence.schema.json")
    record_schemas = {
        "artifact-consumer-proof": "artifact-consumer-proof.schema.json",
        "s2-s3-seven-lens-aggregate": "s2-s3-review.schema.json",
        "independent-pr-review": "independent-pr-review.schema.json",
    }
    for entry in inputs["stage_record"]["gate_evidence"]:
        _validate_review_schema(
            json.loads(inputs["records"][entry["record_reference"]["path"]]),
            record_schemas[entry["record_type"]],
        )

    sealed = artifact_contract.seal_stage_gate_evidence(
        inputs["stage_record"], **_stage_binding(inputs)
    )

    _validate_review_schema(sealed, "stage-gate-evidence.schema.json")
    assert [
        {key: entry[key] for key in descriptor}
        for entry, descriptor in zip(
            sealed["gate_evidence"],
            [row for row in _fixed_gate_descriptors() if row["stage"] == stage],
            strict=True,
        )
    ] == [row for row in _fixed_gate_descriptors() if row["stage"] == stage]
    assert sealed["candidate_identity_record"] == inputs["candidate_identity_reference"]
    assert sealed["candidate"] == inputs["candidate_identity"]["candidate"]
    assert sealed["candidate_identity_record_sha256"] == _sha256_bytes(
        inputs["candidate_identity_bytes"]
    )
    assert sealed["release_gate_catalog"] == inputs["release_gate_catalog_reference"]
    assert sealed["source_ref"] == SOURCE_REF
    assert sealed["source_commit"] == inputs["candidate_identity"]["candidate"]["source"]["commit"]
    assert sealed["outcome"] == "PASS"


@pytest.mark.parametrize(
    "violation",
    [
        "missing_gate",
        "duplicate_gate",
        "out_of_order",
        "future_stage_gate",
        "wrong_record_type",
        "authority_rule_drift",
        "evidence_requirement_drift",
        "wrong_gate_candidate_hash",
        "wrong_gate_source",
        "wrong_candidate",
        "wrong_identity_reference",
        "wrong_identity_hash",
        "wrong_source_ref",
        "wrong_source_commit",
        "wrong_catalog_reference",
        "wrong_catalog_hash",
        "raw_catalog_substitution",
        "unredacted_stage_field",
    ],
)
def test_stage_evidence_semantics_refuse_unclosed_or_mismatched_set(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, "pre-decision")
    record = inputs["stage_record"]
    entries = record["gate_evidence"]
    if violation == "missing_gate":
        entries.pop()
    elif violation == "duplicate_gate":
        entries[-1] = json.loads(json.dumps(entries[0]))
    elif violation == "out_of_order":
        entries[0], entries[1] = entries[1], entries[0]
    elif violation == "future_stage_gate":
        entries[0]["stage"] = "post-publication"
    elif violation == "wrong_record_type":
        entries[-1]["record_type"] = "s2-s3-seven-lens-aggregate"
    elif violation == "authority_rule_drift":
        entries[0]["authority_rules"] = list(reversed(AUTHORITY_RULES))
    elif violation == "evidence_requirement_drift":
        entries[0]["evidence_requirements"] = list(reversed(entries[0]["evidence_requirements"]))
    elif violation == "wrong_gate_candidate_hash":
        entries[0]["candidate_identity_record_sha256"] = "a" * 64
    elif violation == "wrong_gate_source":
        entries[0]["source_commit"] = "a" * 40
    elif violation == "wrong_candidate":
        record["candidate"]["build"]["artifact"]["id"] = "999"
    elif violation == "wrong_identity_reference":
        record["candidate_identity_record"]["run_id"] = "999"
    elif violation == "wrong_identity_hash":
        record["candidate_identity_record_sha256"] = "a" * 64
    elif violation == "wrong_source_ref":
        record["source_ref"] = "refs/heads/other"
    elif violation == "wrong_source_commit":
        record["source_commit"] = "a" * 40
    elif violation == "wrong_catalog_reference":
        record["release_gate_catalog"]["artifact_id"] = "999"
    elif violation == "wrong_catalog_hash":
        record["release_gate_catalog"]["sha256"] = "a" * 64
    elif violation == "raw_catalog_substitution":
        inputs["release_gate_catalog_bytes"] += b" "
    else:
        record["diagnostic"] = "simulated raw diagnostic"

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(record, **_stage_binding(inputs))


@pytest.mark.parametrize("violation", ["source_commit", "snapshot_hash", "descriptor"])
def test_stage_evidence_semantics_revalidate_catalog_authority(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, "pre-publication")
    catalog = inputs["catalog"]["catalog"]
    if violation == "source_commit":
        catalog["source_commit"] = "a" * 40
    elif violation == "snapshot_hash":
        catalog["policy_authority_snapshots"][0]["sha256"] = "a" * 64
    else:
        catalog["gate_descriptors"].pop()
    raw = _canonical_json_bytes(inputs["catalog"])
    inputs["release_gate_catalog_bytes"] = raw
    inputs["release_gate_catalog_reference"]["sha256"] = _sha256_bytes(raw)
    inputs["stage_record"]["release_gate_catalog"]["sha256"] = _sha256_bytes(raw)

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(inputs["stage_record"], **_stage_binding(inputs))


def test_stage_evidence_semantics_policy_snapshots_are_raw_blobs_under_git_eol_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "input")
    inputs = _simulated_review_inputs(tmp_path)
    root = inputs["repository_root"]
    source_commit = inputs["candidate_identity"]["candidate"]["source"]["commit"]
    raw_snapshots = [
        {
            "path": path,
            "source_commit": source_commit,
            "sha256": _sha256_bytes(
                subprocess.run(
                    ["git", "cat-file", "blob", f"{source_commit}:{path}"],
                    cwd=root,
                    capture_output=True,
                    check=True,
                ).stdout
            ),
        }
        for path in _authority_paths()
    ]
    generated_snapshots = []
    for autocrlf in ("false", "true"):
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", autocrlf)
        catalog = resolve_release_gate_catalog(inputs["candidate_identity"], root)
        generated_snapshots.append(catalog["catalog"]["policy_authority_snapshots"])
        stage_inputs = _simulated_stage_inputs(deepcopy(inputs), "pre-publication")
        stage_inputs["catalog"]["catalog"]["policy_authority_snapshots"] = deepcopy(raw_snapshots)
        raw_catalog = _canonical_json_bytes(stage_inputs["catalog"])
        catalog_hash = _sha256_bytes(raw_catalog)
        stage_inputs["release_gate_catalog_bytes"] = raw_catalog
        stage_inputs["release_gate_catalog_reference"]["sha256"] = catalog_hash
        stage_inputs["stage_record"]["release_gate_catalog"]["sha256"] = catalog_hash

        sealed = artifact_contract.seal_stage_gate_evidence(
            stage_inputs["stage_record"], **_stage_binding(stage_inputs)
        )

        assert sealed["outcome"] == "PASS", autocrlf
    assert generated_snapshots == [raw_snapshots, raw_snapshots]


@pytest.mark.parametrize(
    "violation",
    [
        "missing_raw_record",
        "raw_byte_substitution",
        "aggregate_instead_of_pr_review",
        "stale_pr_review",
        "nonindependent_pr_review",
        "unredacted_pr_review",
        "false_aggregate_coverage",
        "incomplete_consumer_matrix",
        "failed_consumer_runtime",
        "failed_raw_review_with_claimed_passing_gate",
        "incomplete_pr_checks",
        "false_lens_check_count",
    ],
)
def test_stage_evidence_semantics_recompute_predecision_from_raw_typed_records(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, "pre-decision")
    if violation in ("incomplete_pr_checks", "false_lens_check_count"):
        assert (
            artifact_contract.seal_stage_gate_evidence(
                inputs["stage_record"], **_stage_binding(inputs)
            )["outcome"]
            == "PASS"
        )
    if violation == "missing_raw_record":
        del inputs["records"]["independent-review.json"]
    elif violation == "raw_byte_substitution":
        inputs["records"]["independent-review.json"] += b" "
    elif violation == "aggregate_instead_of_pr_review":
        inputs["stage_record"]["gate_evidence"][2]["record_reference"] = dict(
            inputs["stage_record"]["gate_evidence"][1]["record_reference"]
        )
    elif violation == "false_aggregate_coverage":
        aggregate = inputs["aggregate"]
        aggregate["lens_results"][0]["denominator"]["required_evidence_count"] = 2
        _replace_simulated_gate_record(inputs, 1, aggregate)
    elif violation == "false_lens_check_count":
        aggregate = inputs["aggregate"]
        aggregate["lens_results"][-1]["denominator"] = {
            "required_evidence_count": 2,
            "completed_evidence_count": 2,
        }
        _replace_simulated_gate_record(inputs, 1, aggregate)
    elif violation in ("incomplete_consumer_matrix", "failed_consumer_runtime"):
        consumer = inputs["consumer"]
        if violation == "incomplete_consumer_matrix":
            consumer["scenario_matrix"]["results"].pop()
        else:
            consumer["runtime_results"]["stdout_jsonrpc_only"] = False
        _replace_simulated_gate_record(inputs, 0, consumer)
    else:
        review = inputs["pr_review"]
        if violation == "stale_pr_review":
            review["review_target"]["reviewed_commit"] = "a" * 40
        elif violation == "nonindependent_pr_review":
            review["reviewer_provenance"]["reviewer_principal_id"] = "simulated-author"
        elif violation == "unredacted_pr_review":
            review["findings"]["records"][0]["diagnostic"] = "simulated raw diagnostic"
        elif violation == "incomplete_pr_checks":
            review["checks"][0]["status"] = "INCOMPLETE"
        else:
            review["outcome"] = "FAIL"
            review["result"]["observed"] = "FAILURE"
        _replace_simulated_gate_record(inputs, 2, review)

    if violation in ("incomplete_pr_checks", "false_lens_check_count"):
        for entry in inputs["stage_record"]["gate_evidence"]:
            reference = entry["record_reference"]
            assert reference["sha256"] == _sha256_bytes(inputs["records"][reference["path"]])

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(inputs["stage_record"], **_stage_binding(inputs))


@pytest.mark.parametrize("stage", ["pre-publication", "post-publication"])
def test_stage_evidence_semantics_refuse_extra_entries_even_when_claimed_passing(
    semantic_review_inputs: dict[str, Any], stage: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, stage)
    inputs["stage_record"]["gate_evidence"].append(
        {
            **_fixed_gate_descriptors()[0],
            "record_reference": inputs["candidate_identity_reference"],
            "candidate_identity_record_sha256": _sha256_bytes(inputs["candidate_identity_bytes"]),
            "source_commit": inputs["candidate_identity"]["candidate"]["source"]["commit"],
            "outcome": "PASS",
        }
    )

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(inputs["stage_record"], **_stage_binding(inputs))


@pytest.mark.parametrize(
    "violation",
    [
        "missing_remote_proof",
        "retained_proof_only",
        "remote_archive_mismatch",
        "failed_remote_matrix",
    ],
)
def test_stage_evidence_semantics_postpublication_requires_matching_remote_proof(
    semantic_review_inputs: dict[str, Any], violation: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, "post-publication")
    if violation == "missing_remote_proof":
        inputs["stage_record"]["gate_evidence"].clear()
    elif violation == "retained_proof_only":
        _replace_simulated_gate_record(inputs, 0, inputs["consumer"])
    else:
        remote = json.loads(inputs["records"]["remote-downloaded-consumer-proof.json"])
        if violation == "remote_archive_mismatch":
            remote["download_origin"]["archive_asset"]["sha256"] = "a" * 64
        else:
            remote["scenario_matrix"]["results"].pop()
        _replace_simulated_gate_record(inputs, 0, remote)

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(inputs["stage_record"], **_stage_binding(inputs))


@pytest.mark.parametrize("stage", ["pre-decision", "pre-publication", "post-publication"])
def test_stage_evidence_semantics_refuse_outcome_inconsistent_with_resolved_subset(
    semantic_review_inputs: dict[str, Any], stage: str
) -> None:
    inputs = _simulated_stage_inputs(semantic_review_inputs, stage)
    inputs["stage_record"]["outcome"] = "FAIL"

    with pytest.raises(ValueError):
        artifact_contract.seal_stage_gate_evidence(inputs["stage_record"], **_stage_binding(inputs))


def _validator_wire_zip(tmp_path: Path, entries: Mapping[str, bytes]) -> bytes:
    path = tmp_path / "retained-wire.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return path.read_bytes()


def _validator_artifact_reference(*, artifact_sha256: str, path: str, sha256: str) -> Any:
    return artifact_contract.ArtifactFileReference(
        repository=REPOSITORY,
        run_id="701",
        artifact_id="701",
        artifact_name="retained-preview-payload",
        artifact_sha256=artifact_sha256,
        retention_days=7,
        expires_at="2030-01-01T00:00:00Z",
        path=path,
        sha256=sha256,
    )


_V3_RUN_ID = "7d551ec6-40bc-4f8a-99e8-903a30783871"
_V3_ANALYSIS_ID = "wave3-analysis-fixture"


def _fixture_sha256(label: str) -> str:
    return _sha256_bytes(label.encode("utf-8"))


def _source_set_sha256(paths: list[str]) -> str:
    return _sha256_bytes(_canonical_json_bytes(sorted(paths)))


def _empty_inventory_summary() -> dict[str, Any]:
    empty_keys_sha256 = _sha256_bytes(_canonical_json_bytes([]))
    return {
        "complete": True,
        "result_empty": True,
        "page_count": 0,
        "total": 0,
        "record_count": 0,
        "keys_sha256": empty_keys_sha256,
        "blocking_key_count": 0,
        "blocking_keys_sha256": empty_keys_sha256,
    }


def _complete_inventory_document(source_commit: str) -> dict[str, Any]:
    summary = _empty_inventory_summary()
    pagination = {"page_size": 500, **summary}
    return {
        "schema_version": 1,
        "write_mode": "create_new",
        "identity": {
            "captured_head": source_commit,
            "project_key": "thebtf_netcoredbg_mcp",
            "analysis_id": _V3_ANALYSIS_ID,
        },
        "issues": {"pagination": pagination, "records": []},
        "hotspots": {"pagination": pagination, "records": []},
    }


def _final_report_evidence(
    run_id: str,
    report_id: str,
    source_paths: list[str],
) -> dict[str, Any]:
    report_bytes = f"{report_id}-cobertura-fixture".encode()
    return {
        "id": report_id,
        "language": report_id,
        "format": "cobertura",
        "relative_path": f".tmp/sonarqube-coverage/{run_id}/{report_id}/coverage.xml",
        "sha256": _sha256_bytes(report_bytes),
        "bytes": len(report_bytes),
        "xml_root": "coverage",
        "lines_valid": 10,
        "lines_covered": 8,
        "branches_valid": 4,
        "branches_covered": 3,
        "source_paths": source_paths,
        "source_set_sha256": _source_set_sha256(source_paths),
    }


def _dotnet_producer_evidence(
    run_id: str,
    producer_id: str,
    project: str,
    source_path: str,
) -> dict[str, Any]:
    input_bytes = f"{producer_id}-cobertura-fixture".encode()
    return {
        "id": producer_id,
        "project": project,
        "relative_path": (
            f".tmp/sonarqube-coverage/{run_id}/dotnet/inputs/{producer_id}/coverage.cobertura.xml"
        ),
        "sha256": _sha256_bytes(input_bytes),
        "bytes": len(input_bytes),
        "lines_valid": 4,
        "lines_covered": 3,
        "branches_valid": 2,
        "branches_covered": 1,
        "source_paths": [source_path],
        "source_set_sha256": _source_set_sha256([source_path]),
    }


def _complete_v3_exact_head_receipt(
    source_commit: str,
    *,
    role: str,
    outcome: str,
    release_intent: str,
) -> dict[str, Any]:
    dotnet_inputs = [
        _dotnet_producer_evidence(
            _V3_RUN_ID,
            "codesearch-core",
            "host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj",
            "host/NetCoreDbg.Mcp.CodeSearch.Core/SymbolSearchEngine.cs",
        ),
        _dotnet_producer_evidence(
            _V3_RUN_ID,
            "host",
            "host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
            "host/NetCoreDbg.Mcp.Host/Program.cs",
        ),
        _dotnet_producer_evidence(
            _V3_RUN_ID,
            "stateless-preview",
            "host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj",
            "host/NetCoreDbg.Mcp.Stateless.Preview/Program.cs",
        ),
        _dotnet_producer_evidence(
            _V3_RUN_ID,
            "stateless",
            "host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj",
            "host/NetCoreDbg.Mcp.Stateless/Program.cs",
        ),
        _dotnet_producer_evidence(
            _V3_RUN_ID,
            "host-prompts",
            "tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj",
            "host/NetCoreDbg.Mcp.Host/NativePrompts.cs",
        ),
    ]
    dotnet_source_paths = sorted(
        source_path
        for input_evidence in dotnet_inputs
        for source_path in input_evidence["source_paths"]
    )
    inventory_document = _complete_inventory_document(source_commit)
    inventory_bytes = _canonical_json_bytes(inventory_document)
    inventory_summary = _empty_inventory_summary()
    marker_bytes = _canonical_json_bytes({"captured_head": source_commit, "run_id": _V3_RUN_ID})

    def component_evidence(paths: list[str]) -> dict[str, Any]:
        return {
            "source_set_sha256": _source_set_sha256(paths),
            "page_count": 1,
            "complete": True,
            "mapped_path_count": len(paths),
            "lines_to_cover": 10,
            "covered_lines": 8,
            "branch_measure_path_count": 1,
            "mapped_paths_sha256": _source_set_sha256(paths),
        }

    return {
        "schema_version": 3,
        "role": role,
        "outcome": outcome,
        "release_intent": release_intent,
        "identity": {
            "captured_head": source_commit,
            "project_key": "thebtf_netcoredbg_mcp",
            "analysis_id": _V3_ANALYSIS_ID,
        },
        "coverage": {
            "run_id": _V3_RUN_ID,
            "marker": {
                "relative_path": f".tmp/sonarqube-coverage/{_V3_RUN_ID}/coverage-run.json",
                "sha256": _sha256_bytes(marker_bytes),
                "bytes": len(marker_bytes),
            },
            "final_reports": [
                _final_report_evidence(
                    _V3_RUN_ID,
                    "python",
                    ["src/netcoredbg_mcp/server.py"],
                ),
                _final_report_evidence(_V3_RUN_ID, "dotnet", dotnet_source_paths),
            ],
            "dotnet_producers": dotnet_inputs,
            "normalization": {
                "algorithm": "cobertura-merge-normalize-v1",
                "input_set_sha256": _sha256_bytes(_canonical_json_bytes(dotnet_inputs)),
                "output_report_id": "dotnet",
                "source_union_complete": True,
            },
            "stateless_host_binary": {
                "dll_sha256": _fixture_sha256("stateless-dll"),
                "pdb_sha256": _fixture_sha256("stateless-pdb"),
                "restored": True,
            },
        },
        "analysis": {
            "observations": {
                "submitted": True,
                "current_before_measures": True,
                "current_after_measures": True,
                "current_final": True,
            },
            "aggregate": {
                "coverage": 85.0,
                "lines_to_cover": 100,
                "new_coverage": 85.0,
                "new_lines_to_cover": 20,
            },
            "new_coverage_condition": {
                "status": "OK",
                "threshold": 80,
                "actual_value": 85.0,
            },
            "python_components": component_evidence(["src/netcoredbg_mcp/server.py"]),
            "dotnet_components": component_evidence(dotnet_source_paths),
        },
        "global_inventory": {
            "artifact": {
                "relative_path": (
                    ".agent/e/sonarqube/thebtf_netcoredbg_mcp/"
                    f"{source_commit}/diagnostic/{_V3_RUN_ID}.inventory.json"
                ),
                "sha256": _sha256_bytes(inventory_bytes),
                "bytes": len(inventory_bytes),
            },
            "artifact_schema_version": 1,
            "issues": inventory_summary,
            "hotspots": inventory_summary.copy(),
        },
        "release_gate": (
            None
            if role == "diagnostic"
            else {
                "quality_gate_status": "OK",
                "blocking_issue_count": 0,
                "blocking_hotspot_count": 0,
            }
        ),
        "cleanup": {
            "claimed_root": f".tmp/sonarqube-coverage/{_V3_RUN_ID}",
            "producer_terminal": True,
            "removed_paths": [f".tmp/sonarqube-coverage/{_V3_RUN_ID}"],
            "parent_removed_if_empty": True,
            "status": "OK",
            "failure": None,
        },
        "failure": None,
    }


def _blocked_v3_exact_head_receipt(
    source_commit: str,
    *,
    role: str,
    release_intent: str,
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "role": role,
        "outcome": "BLOCKED",
        "release_intent": release_intent,
        "identity": {
            "captured_head": source_commit,
            "project_key": "thebtf_netcoredbg_mcp",
            "analysis_id": None,
        },
        "coverage": None,
        "analysis": None,
        "global_inventory": None,
        "release_gate": None,
        "cleanup": None,
        "failure": {
            "code": "COVERAGE_RUN_BLOCKED",
            "stage": "PRODUCING",
            "language": None,
            "project_id": None,
            "safe_message": "fixture coverage transaction blocked",
        },
    }


def _exact_head_runner_for_receipt_tests() -> Any:
    assert EXACT_HEAD_RUNNER_PATH.is_file(), "R15 exact-head runner is missing"
    spec = importlib.util.spec_from_file_location(
        "wave3_exact_head_runner_for_receipt_tests",
        EXACT_HEAD_RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    return runner


def _v3_receipt_validator() -> tuple[Any, Any]:
    runner = _exact_head_runner_for_receipt_tests()
    validator = getattr(runner, "validate_exact_head_receipt_v3", None)
    assert callable(validator), "R15 requires a unified v3 receipt validator"
    return runner, validator


@pytest.mark.parametrize(
    ("role", "outcome", "release_intent"),
    [
        ("diagnostic", "DIAGNOSTIC_COMPLETE", "none"),
        ("candidate", "PASS", "v0.23.12"),
        ("post-merge", "PASS", "v0.23.12"),
    ],
)
def test_unified_v3_receipt_accepts_each_complete_legal_role_outcome(
    role: str,
    outcome: str,
    release_intent: str,
) -> None:
    _, validator = _v3_receipt_validator()

    validator(
        _complete_v3_exact_head_receipt(
            "a" * 40,
            role=role,
            outcome=outcome,
            release_intent=release_intent,
        )
    )


@pytest.mark.parametrize(
    ("role", "outcome", "release_intent"),
    [
        ("diagnostic", "PASS", "none"),
        ("candidate", "DIAGNOSTIC_COMPLETE", "v0.23.12"),
        ("post-merge", "PASS", "none"),
    ],
)
def test_unified_v3_receipt_refuses_illegal_role_outcome_combinations(
    role: str,
    outcome: str,
    release_intent: str,
) -> None:
    runner, validator = _v3_receipt_validator()

    receipt = _complete_v3_exact_head_receipt(
        "a" * 40,
        role=role,
        outcome=outcome,
        release_intent=release_intent,
    )
    with pytest.raises(runner.RunnerError):
        validator(receipt)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.update({"schema_version": 2}),
        lambda receipt: receipt["coverage"].update({"final_reports": []}),
        lambda receipt: receipt["global_inventory"]["issues"].pop("complete"),
        lambda receipt: receipt["global_inventory"]["artifact"].update({"sha256": "0" * 64}),
        lambda receipt: receipt["identity"].update({"captured_head": "b" * 40}),
        lambda receipt: receipt["cleanup"].update(
            {
                "status": "FAILED",
                "failure": {
                    "code": "COVERAGE_CLEANUP_FAILED",
                    "message": "fixture cleanup failed",
                },
            }
        ),
    ],
)
def test_unified_v3_receipt_refuses_unbound_or_incomplete_completion_evidence(
    mutate: Any,
) -> None:
    receipt = _complete_v3_exact_head_receipt(
        "a" * 40,
        role="diagnostic",
        outcome="DIAGNOSTIC_COMPLETE",
        release_intent="none",
    )
    mutate(receipt)
    runner, validator = _v3_receipt_validator()

    with pytest.raises(runner.RunnerError):
        validator(receipt)


@pytest.mark.parametrize(
    ("role", "field"),
    [
        ("candidate", "blocking_issue_count"),
        ("candidate", "blocking_hotspot_count"),
        ("post-merge", "blocking_issue_count"),
        ("post-merge", "blocking_hotspot_count"),
    ],
)
def test_unified_v3_pass_refuses_blocking_findings_or_hotspots(
    role: str,
    field: str,
) -> None:
    receipt = _complete_v3_exact_head_receipt(
        "a" * 40,
        role=role,
        outcome="PASS",
        release_intent="v0.23.12",
    )
    assert receipt["release_gate"] is not None
    receipt["release_gate"][field] = 1
    runner, validator = _v3_receipt_validator()

    with pytest.raises(runner.RunnerError):
        validator(receipt)


def _artifact_transport_responses(reference: Any, wire_bytes: bytes) -> dict[str, bytes]:
    endpoint = f"repos/{reference.repository}/actions/artifacts/{reference.artifact_id}"
    return {
        endpoint: _canonical_json_bytes(
            {
                "id": int(reference.artifact_id),
                "name": reference.artifact_name,
                "expired": False,
                "digest": f"sha256:{reference.artifact_sha256}",
                "expires_at": reference.expires_at,
                "workflow_run": {"id": int(reference.run_id)},
            }
        ),
        f"{endpoint}/zip": wire_bytes,
    }


def test_artifact_downloader_refuses_preexisting_destination_without_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    member = b"retained payload member"
    wire_bytes = _validator_wire_zip(tmp_path, {"payload.zip": member})
    reference = _validator_artifact_reference(
        artifact_sha256=_sha256_bytes(wire_bytes),
        path="payload.zip",
        sha256=_sha256_bytes(member),
    )
    archive_path = root / "artifacts" / f"{reference.artifact_id}-{reference.artifact_sha256}.zip"
    archive_path.parent.mkdir()
    archive_path.write_bytes(b"planted archive")
    downloader = artifact_contract.ArtifactDownloader(root)

    responses = _artifact_transport_responses(reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])

    for _ in range(2):
        with pytest.raises(ValueError, match="destination already exists"):
            downloader.download_file(reference)

    assert archive_path.read_bytes() == b"planted archive"
    assert not (root / "downloaded").exists()


def test_artifact_downloader_refuses_racing_destination_without_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    member = b"retained payload member"
    wire_bytes = _validator_wire_zip(tmp_path, {"payload.zip": member})
    reference = _validator_artifact_reference(
        artifact_sha256=_sha256_bytes(wire_bytes),
        path="payload.zip",
        sha256=_sha256_bytes(member),
    )
    archive_path = root / "artifacts" / f"{reference.artifact_id}-{reference.artifact_sha256}.zip"
    downloader = artifact_contract.ArtifactDownloader(root)
    original_open = Path.open
    race_injected = False

    def race_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal race_injected
        if path == archive_path and mode == "xb" and not race_injected:
            race_injected = True
            with original_open(path, "wb") as planted:
                planted.write(b"raced archive")
        return original_open(path, mode, *args, **kwargs)

    responses = _artifact_transport_responses(reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])
    monkeypatch.setattr(Path, "open", race_open)

    for _ in range(2):
        with pytest.raises(ValueError, match="destination already exists"):
            downloader.download_file(reference)

    assert race_injected
    assert archive_path.read_bytes() == b"raced archive"
    assert not (root / "downloaded").exists()


def test_artifact_downloader_refuses_mismatched_wire_digest_before_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    member = b"retained payload member"
    wire_bytes = _validator_wire_zip(tmp_path, {"payload.zip": member})
    reference = _validator_artifact_reference(
        artifact_sha256="d" * 64,
        path="payload.zip",
        sha256=_sha256_bytes(member),
    )
    downloader = artifact_contract.ArtifactDownloader(root)

    responses = _artifact_transport_responses(reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])

    with pytest.raises(ValueError, match="wire bytes"):
        downloader.download_file(reference)

    assert not (root / "artifacts").exists()
    assert not (root / "downloaded").exists()


def test_artifact_downloader_refuses_tampered_cached_wire_before_member_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    archive_member = b"retained preview archive"
    manifest_member = b"retained preview manifest"
    wire_bytes = _validator_wire_zip(
        tmp_path,
        {"payload.zip": archive_member, "payload.manifest.json": manifest_member},
    )
    wire_sha256 = _sha256_bytes(wire_bytes)
    archive_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.zip",
        sha256=_sha256_bytes(archive_member),
    )
    manifest_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.manifest.json",
        sha256=_sha256_bytes(manifest_member),
    )
    downloader = artifact_contract.ArtifactDownloader(root)
    responses = _artifact_transport_responses(archive_reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])

    assert downloader.download_file(archive_reference).read_bytes() == archive_member
    cached_path = root / "artifacts" / f"{archive_reference.artifact_id}-{wire_sha256}.zip"
    cached_path.write_bytes(
        _validator_wire_zip(
            tmp_path,
            {"payload.zip": b"substituted archive", "payload.manifest.json": manifest_member},
        )
    )

    with pytest.raises(ValueError, match="cached retained artifact"):
        downloader.download_file(manifest_reference)

    assert not (root / "downloaded" / "payload.manifest.json").exists()


def test_artifact_downloader_retains_payload_after_distinct_digest_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    archive_member = b"retained preview archive"
    manifest_member = b"retained preview manifest"
    candidate_payload_digest = _sha256_bytes(archive_member + manifest_member)
    wire_bytes = _validator_wire_zip(
        tmp_path,
        {"payload.zip": archive_member, "payload.manifest.json": manifest_member},
    )
    wire_sha256 = _sha256_bytes(wire_bytes)
    archive_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.zip",
        sha256=_sha256_bytes(archive_member),
    )
    manifest_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.manifest.json",
        sha256=_sha256_bytes(manifest_member),
    )
    downloader = artifact_contract.ArtifactDownloader(root)

    responses = _artifact_transport_responses(archive_reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])

    archive_path, manifest_path = downloader.download_payload(
        archive_reference,
        manifest_reference,
        candidate_payload_digest,
    )

    assert wire_sha256 != candidate_payload_digest
    assert archive_path.read_bytes() == archive_member
    assert manifest_path.read_bytes() == manifest_member
    retained_wire = root / "artifacts" / f"{archive_reference.artifact_id}-{wire_sha256}.zip"
    assert retained_wire.read_bytes() == wire_bytes


def test_artifact_downloader_requires_candidate_payload_digest_after_member_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-download"
    root.mkdir()
    archive_member = b"retained preview archive"
    manifest_member = b"retained preview manifest"
    declared_payload_digest = "0" * 64
    wire_bytes = _validator_wire_zip(
        tmp_path,
        {"payload.zip": archive_member, "payload.manifest.json": manifest_member},
    )
    wire_sha256 = _sha256_bytes(wire_bytes)
    archive_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.zip",
        sha256=_sha256_bytes(archive_member),
    )
    manifest_reference = _validator_artifact_reference(
        artifact_sha256=wire_sha256,
        path="payload.manifest.json",
        sha256=_sha256_bytes(manifest_member),
    )
    downloader = artifact_contract.ArtifactDownloader(root)

    responses = _artifact_transport_responses(archive_reference, wire_bytes)
    monkeypatch.setattr(downloader, "_run_gh", lambda arguments, _: responses[arguments[1]])

    with pytest.raises(ValueError, match="candidate artifact"):
        downloader.download_payload(
            archive_reference,
            manifest_reference,
            declared_payload_digest,
        )

    assert not (root / "downloaded").exists()


def test_python_rollback_controlled_child_prefers_selected_environment_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controller-boundary proof only; this is not live Python PRODUCT_WORKS."""
    # The existing interpreter doubles as the helper, avoiding a new environment.
    command = Path(sys.executable).absolute()
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    ambient_helper = ambient / command.name
    ambient_helper.write_bytes(b"ambient helper must not be selected\n")
    ambient_helper.chmod(0o700)
    inherited_helper = ambient / (
        "a1-inherited-helper.exe" if os.name == "nt" else "a1-inherited-helper"
    )
    inherited_helper.write_bytes(b"inherited helper remains discoverable\n")
    inherited_helper.chmod(0o700)
    monkeypatch.setenv("PATH", str(ambient) + os.pathsep + os.environ.get("PATH", ""))
    assert Path(shutil.which(command.name)).resolve() == ambient_helper.resolve()
    parent_environment = dict(os.environ)
    child = """
import json
import shutil
import sys
from pathlib import Path

selected = shutil.which(sys.argv[1])
assert selected is not None, "selected environment helper is unavailable"
assert Path(selected).resolve() == Path(sys.argv[2]).resolve(), selected
inherited = shutil.which(sys.argv[3])
assert inherited is not None, "inherited PATH entries were discarded"
assert Path(inherited).resolve() == Path(sys.argv[4]).resolve(), inherited
print(json.dumps({
    "product_works": True,
    "denominator": "5/5",
    "tool_count": 135,
    "stopped_at_entry": True,
}))
"""

    try:
        result = preview_validator._run_python_rollback(
            str(command),
            [
                "-I",
                "-S",
                "-c",
                child,
                command.name,
                str(command),
                inherited_helper.name,
                str(inherited_helper),
            ],
        )
    finally:
        assert dict(os.environ) == parent_environment

    assert result["legacy_journey_outcome"] == "PRODUCT_WORKS"


def test_python_rollback_refuses_zero_exit_without_product_works_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_output = b'{"result":"zero exit without rollback marker"}\n'

    monkeypatch.setattr(
        preview_validator.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[sys.executable],
            returncode=0,
            stdout=raw_output,
            stderr=b"",
        ),
    )

    with pytest.raises(ValueError, match="did not emit PRODUCT_WORKS") as error:
        preview_validator._run_python_rollback(sys.executable, ["consumer.py"])

    assert raw_output.decode("utf-8") not in str(error.value)


def test_python_rollback_records_product_works_after_observable_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = {
        "product_works": True,
        "denominator": "5/5",
        "tool_count": 135,
        "stopped_at_entry": True,
    }

    monkeypatch.setattr(
        preview_validator.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[sys.executable],
            returncode=0,
            stdout=json.dumps(marker, sort_keys=True).encode("utf-8") + b"\n",
            stderr=b"",
        ),
    )

    assert preview_validator._run_python_rollback(sys.executable, ["consumer.py"]) == {
        "only_preview_selection_removed": True,
        "python_package_reinstalled": False,
        "python_package_replaced": False,
        "console_entrypoint_changed": False,
        "default_selector_changed": False,
        "legacy_journey_outcome": "PRODUCT_WORKS",
    }


@pytest.fixture
def promotion_decision_inputs(semantic_review_inputs: dict[str, Any]) -> dict[str, Any]:
    """Simulated semantic inputs, never live approval or promotion authority."""
    inputs = _simulated_stage_inputs(semantic_review_inputs, "pre-decision")
    stage = artifact_contract.seal_stage_gate_evidence(
        inputs["stage_record"], **_stage_binding(inputs)
    )
    stage_bytes = _canonical_json_bytes(stage)
    stage_reference = {
        **inputs["stage_record"]["gate_evidence"][0]["record_reference"],
        "path": "simulated-pre-decision-stage.json",
        "sha256": _sha256_bytes(stage_bytes),
    }
    inputs["records"][stage_reference["path"]] = stage_bytes
    inputs["decision_record"] = {
        "schema_version": "1.0",
        "candidate_identity_record": deepcopy(inputs["candidate_identity_reference"]),
        "candidate": deepcopy(inputs["candidate_identity"]["candidate"]),
        "evidence": {
            "pre_decision_stage_evidence": {
                "record_reference": stage_reference,
                **{
                    key: stage[key]
                    for key in (
                        "candidate_identity_record_sha256",
                        "source_ref",
                        "source_commit",
                        "stage",
                        "outcome",
                    )
                },
            }
        },
        "decision": "APPROVE",
        "promotion_attempt": {
            "decision_author": {"id": "101", "login": "simulated-author"},
            "authorized_dispatcher": {"id": "102", "login": "simulated-dispatcher"},
        },
        "decided_at": "2030-01-01T00:00:01Z",
    }
    return inputs


def _replace_simulated_decision_stage(inputs: dict[str, Any]) -> None:
    reference = inputs["decision_record"]["evidence"]["pre_decision_stage_evidence"][
        "record_reference"
    ]
    raw = _canonical_json_bytes(inputs["stage_record"])
    inputs["records"][reference["path"]] = raw
    reference["sha256"] = _sha256_bytes(raw)


@pytest.mark.parametrize("decision", ["APPROVE", "DECLINE"])
def test_promotion_decision_semantics_seal_without_attempt_or_remote_proof(
    promotion_decision_inputs: dict[str, Any], decision: str
) -> None:
    inputs = promotion_decision_inputs
    record = inputs["decision_record"]
    record["decision"] = decision
    original = deepcopy(record)
    _validate_review_schema(record, "promotion-decision.schema.json")

    sealed = artifact_contract.seal_promotion_decision(record, **_stage_binding(inputs))

    _validate_review_schema(sealed, "promotion-decision.schema.json")
    assert sealed == original
    assert record == original
    with pytest.raises(TypeError):
        sealed["decision"] = "DECLINE" if decision == "APPROVE" else "APPROVE"
    with pytest.raises(TypeError):
        sealed["promotion_attempt"]["authorized_dispatcher"]["id"] = "999"


@pytest.mark.parametrize(
    "violation",
    [
        "candidate_source",
        "candidate_payload",
        "identity_repository",
        "identity_run",
        "identity_artifact",
        "identity_path",
        "identity_hash",
        "raw_identity_bytes",
        "supplied_identity_object",
    ],
)
def test_promotion_decision_semantics_refuse_substituted_candidate(
    promotion_decision_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_decision_inputs
    record = inputs["decision_record"]
    if violation == "candidate_source":
        record["candidate"]["source"]["commit"] = "a" * 40
    elif violation == "candidate_payload":
        record["candidate"]["build"]["artifact"]["id"] = "999"
    elif violation.startswith("identity_"):
        field, value = {
            "identity_repository": ("repository", "other/repository"),
            "identity_run": ("run_id", "999"),
            "identity_artifact": ("artifact_id", "999"),
            "identity_path": ("path", "other-candidate.json"),
            "identity_hash": ("sha256", "a" * 64),
        }[violation]
        record["candidate_identity_record"][field] = value
    elif violation == "raw_identity_bytes":
        inputs["candidate_identity_bytes"] += b" "
    else:
        inputs["candidate_identity"]["candidate"]["source"]["commit"] = "a" * 40

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_decision(record, **_stage_binding(inputs))


@pytest.mark.parametrize(
    "violation",
    [
        "missing_raw_stage",
        "raw_stage_substitution",
        "stage_reference_origin",
        "stage_summary_hash",
        "stage_summary_source",
        "stage_summary_outcome",
        "pre_publication_stage",
        "post_publication_stage",
        "raw_stage_candidate",
        "raw_stage_identity",
        "raw_stage_catalog",
        "raw_stage_source",
        "raw_stage_outcome",
        "missing_stage_descriptor",
    ],
)
def test_promotion_decision_semantics_require_exact_resolved_predecision_stage(
    promotion_decision_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_decision_inputs
    record = inputs["decision_record"]
    summary = record["evidence"]["pre_decision_stage_evidence"]
    reference = summary["record_reference"]
    stage = inputs["stage_record"]
    if violation == "missing_raw_stage":
        del inputs["records"][reference["path"]]
    elif violation == "raw_stage_substitution":
        inputs["records"][reference["path"]] += b" "
    elif violation == "stage_reference_origin":
        reference["run_id"] = "999"
    elif violation == "stage_summary_hash":
        summary["candidate_identity_record_sha256"] = "a" * 64
    elif violation == "stage_summary_source":
        summary["source_commit"] = "a" * 40
    elif violation == "stage_summary_outcome":
        summary["outcome"] = "FAIL"
    else:
        if violation in ("pre_publication_stage", "post_publication_stage"):
            stage["stage"] = (
                "pre-publication" if violation == "pre_publication_stage" else "post-publication"
            )
            stage["gate_evidence"].clear()
        elif violation == "raw_stage_candidate":
            stage["candidate"]["build"]["artifact"]["id"] = "999"
        elif violation == "raw_stage_identity":
            stage["candidate_identity_record"]["artifact_id"] = "999"
        elif violation == "raw_stage_catalog":
            stage["release_gate_catalog"]["sha256"] = "a" * 64
        elif violation == "raw_stage_source":
            stage["source_commit"] = "a" * 40
        elif violation == "raw_stage_outcome":
            stage["outcome"] = "FAIL"
        else:
            stage["gate_evidence"].pop()
        _replace_simulated_decision_stage(inputs)

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_decision(record, **_stage_binding(inputs))


@pytest.mark.parametrize(
    "violation",
    [
        "false_seven_lens_coverage",
        "nonindependent_pr_review",
        "incomplete_consumer",
        "false_pr_check_count",
        "incomplete_lens_checks",
    ],
)
def test_promotion_decision_semantics_recompute_nested_raw_evidence(
    promotion_decision_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_decision_inputs
    if violation in ("false_pr_check_count", "incomplete_lens_checks"):
        assert (
            artifact_contract.seal_promotion_decision(
                inputs["decision_record"], **_stage_binding(inputs)
            )["decision"]
            == "APPROVE"
        )
    if violation == "false_seven_lens_coverage":
        aggregate = inputs["aggregate"]
        aggregate["lens_results"].pop()
        _replace_simulated_gate_record(inputs, 1, aggregate)
    elif violation == "nonindependent_pr_review":
        review = inputs["pr_review"]
        review["reviewer_provenance"]["reviewer_principal_id"] = "simulated-author"
        _replace_simulated_gate_record(inputs, 2, review)
    elif violation == "false_pr_check_count":
        review = inputs["pr_review"]
        review["denominator"] = {"required_evidence_count": 2, "completed_evidence_count": 2}
        _replace_simulated_gate_record(inputs, 2, review)
    elif violation == "incomplete_lens_checks":
        aggregate = inputs["aggregate"]
        aggregate["lens_results"][-1]["checks"][0]["status"] = "INCOMPLETE"
        _replace_simulated_gate_record(inputs, 1, aggregate)
    else:
        consumer = inputs["consumer"]
        consumer["scenario_matrix"]["results"].pop()
        _replace_simulated_gate_record(inputs, 0, consumer)
    _replace_simulated_decision_stage(inputs)

    if violation in ("false_pr_check_count", "incomplete_lens_checks"):
        references = [
            entry["record_reference"] for entry in inputs["stage_record"]["gate_evidence"]
        ]
        references.append(
            inputs["decision_record"]["evidence"]["pre_decision_stage_evidence"]["record_reference"]
        )
        for reference in references:
            assert reference["sha256"] == _sha256_bytes(inputs["records"][reference["path"]])

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_decision(
            inputs["decision_record"], **_stage_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation",
    [
        "unknown_decision",
        "historical_run",
        "post_publication_proof",
        "attempt_in_template",
        "invalid_author",
        "invalid_dispatcher",
        "invalid_decision_time",
        "decision_before_stage",
    ],
)
def test_promotion_decision_semantics_refuse_unclosed_or_invalid_authorization(
    promotion_decision_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_decision_inputs
    record = inputs["decision_record"]
    if violation == "unknown_decision":
        record["decision"] = "DEFER"
    elif violation == "historical_run":
        record["workflow_run"] = {"run_id": "999", "run_attempt": 1}
    elif violation == "post_publication_proof":
        record["evidence"]["post_publication_stage_evidence"] = deepcopy(
            record["evidence"]["pre_decision_stage_evidence"]
        )
    elif violation == "attempt_in_template":
        record["promotion_attempt"]["run_attempt"] = 1
    elif violation == "invalid_author":
        record["promotion_attempt"]["decision_author"]["id"] = 101
    elif violation == "invalid_dispatcher":
        record["promotion_attempt"]["authorized_dispatcher"]["login"] = "not a login"
    elif violation == "decision_before_stage":
        record["decided_at"] = "2029-12-31T23:59:59Z"
    else:
        record["decided_at"] = "not a timestamp"

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_decision(record, **_stage_binding(inputs))


_EVIDENCE_BUNDLE_MEMBERS = (
    "consumer-proof.json",
    "s2-s3-review.json",
    "independent-pr-review.json",
)


@pytest.fixture
def evidence_bundle_records() -> dict[str, bytes]:
    # Transport-only objects, not semantically admitted review or approval receipts.
    return {
        "consumer-proof.json": (
            b'{\r\n  "proof_stage": "retained_artifact", "outcome": "FAIL"\r\n}\r\n'
        ),
        "s2-s3-review.json": (
            b'{"coverage_summary":{"completed_lens_count":0},"outcome":"INCOMPLETE"}\n'
        ),
        "independent-pr-review.json": (
            '{ "findings": {"records": [], "unresolved_high_severity_count": 0}, '
            '"label": "тест" }\n'
        ).encode("utf-8"),
    }


def _evidence_bundle_zip(
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    comment: bytes = b"",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, raw in members:
            archive.writestr(name, raw)
        archive.comment = comment
    return buffer.getvalue()


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_decode_evidence_bundle_returns_only_original_named_bytes(
    evidence_bundle_records: dict[str, bytes], compression: int
) -> None:
    records = evidence_bundle_records
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    encoded = base64.b64encode(
        _evidence_bundle_zip(list(reversed(list(records.items()))), compression=compression)
    ).decode("ascii")

    decoded = artifact_contract.decode_evidence_bundle(encoded, MappingProxyType(expected_sha256))

    expected_sha256.clear()
    assert isinstance(decoded, Mapping)
    assert decoded == records
    assert set(decoded) == set(_EVIDENCE_BUNDLE_MEMBERS)
    assert all(type(raw) is bytes for raw in decoded.values())
    with pytest.raises(TypeError):
        decoded["consumer-proof.json"] = b"{}"


def test_decode_evidence_bundle_keeps_distinct_names_with_equal_raw_hashes() -> None:
    raw = b'{"findings":{"records":[]}}\n'
    records = dict.fromkeys(_EVIDENCE_BUNDLE_MEMBERS, raw)
    encoded = base64.b64encode(_evidence_bundle_zip(list(records.items()))).decode("ascii")

    decoded = artifact_contract.decode_evidence_bundle(
        encoded, dict.fromkeys(_EVIDENCE_BUNDLE_MEMBERS, _sha256_bytes(raw))
    )

    assert decoded == records
    assert len(decoded) == 3


@pytest.fixture
def remote_observation_inputs(semantic_review_inputs: dict[str, Any]) -> dict[str, Any]:
    """Simulated recorded facts and independent runtime context, never live readback."""
    inputs = semantic_review_inputs
    candidate = inputs["candidate_identity"]["candidate"]
    inputs["current_attempt_id"] = "simulated-attempt-001"
    inputs["current_workflow_run"] = {
        "repository": REPOSITORY,
        "workflow_path": ".github/workflows/stateless-preview.yml",
        "mode": "promote",
        "event": "workflow_dispatch",
        "run_id": "801",
        "run_attempt": 2,
        "ref": SOURCE_REF,
        "commit": candidate["source"]["commit"],
    }
    assets = {}
    for kind, descriptor, asset_id in (
        ("archive", candidate["preview_manifest"]["contents"]["archive"], "902"),
        ("manifest", candidate["preview_manifest"]["file"], "903"),
    ):
        assets[kind] = {
            "status": "PRESENT",
            "metadata": {
                "id": asset_id,
                "name": descriptor["name"],
                "size_bytes": descriptor["size_bytes"],
            },
            "downloaded_identity": deepcopy(descriptor),
            "observed_result": "SUCCESS",
            "identity_status": "MATCHES_CANDIDATE",
        }
    inputs["observation"] = {
        "observation_schema_version": "1.0",
        "observation_id": "simulated-observation-001",
        "promotion_attempt_id": inputs["current_attempt_id"],
        "candidate_identity_record": deepcopy(inputs["candidate_identity_reference"]),
        "candidate": deepcopy(candidate),
        "retained_artifact_availability": {
            "status": "AVAILABLE_AND_VERIFIED",
            "observed_result": "SUCCESS",
        },
        "tag_observation": {
            "status": "PRESENT",
            "tag": candidate["destination"]["tag"],
            "tag_type": "ANNOTATED",
            "object_id": "d" * 40,
            "peeled_commit": candidate["source"]["commit"],
        },
        "release_observation": {
            "status": "PRESENT",
            "release_id": "901",
            "tag": candidate["destination"]["tag"],
            "is_draft": True,
            "is_prerelease": True,
            "destination_metadata_status": "MATCHING",
        },
        "asset_observations": {**assets, "unexpected_asset_ids": []},
        "readback_status": {
            "expected_result": "EXPECT_SUCCESS",
            "observed_result": "SUCCESS",
            "reason_code": "READBACK_COMPLETE",
        },
        "observed_at": "2029-12-31T23:59:58Z",
    }
    inputs["classified_at"] = "2029-12-31T23:59:59Z"
    inputs["observation_reference"] = {
        "repository": REPOSITORY,
        "run_id": "801",
        "artifact_id": "802",
        "path": "simulated-remote-observation.json",
        "sha256": "0" * 64,
    }
    inputs["remote_records"] = {}

    def record_resolver(reference: Mapping[str, Any]) -> bytes:
        expected = inputs["observation_reference"]
        if any(
            reference[key] != expected[key]
            for key in ("repository", "run_id", "artifact_id", "path")
        ):
            raise ValueError("simulated observation locator is unavailable")
        return inputs["remote_records"][reference["path"]]

    inputs["remote_record_resolver"] = record_resolver
    _replace_simulated_remote_observation(inputs)
    return inputs


def _replace_simulated_remote_observation(inputs: dict[str, Any], raw: bytes | None = None) -> None:
    raw = _canonical_json_bytes(inputs["observation"]) if raw is None else raw
    reference = inputs["observation_reference"]
    inputs["remote_records"][reference["path"]] = raw
    reference["sha256"] = _sha256_bytes(raw)


def _remote_observation_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {**_review_binding(inputs), "current_attempt_id": inputs["current_attempt_id"]}


def _remote_classification_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_remote_observation_binding(inputs),
        "classification_id": "simulated-classification-001",
        "classified_at": inputs["classified_at"],
        "current_workflow_run": inputs["current_workflow_run"],
        "record_resolver": inputs["remote_record_resolver"],
    }


@pytest.mark.parametrize(
    ("tag_present", "draft", "present_assets", "state", "action", "reason"),
    [
        (False, None, (), "unstarted", "CREATE_ANNOTATED_TAG_AND_DRAFT_RELEASE", "TAG_ABSENT"),
        (True, None, (), "tag_only", "CREATE_DRAFT_PRERELEASE", "TAG_MATCHING_ANNOTATED"),
        (True, True, (), "draft_empty", "UPLOAD_MISSING_APPROVED_ASSETS", "EXPECTED_ASSETS_ABSENT"),
        (
            True,
            True,
            ("archive",),
            "draft_partial",
            "UPLOAD_MISSING_APPROVED_ASSETS",
            "EXPECTED_ASSETS_PARTIAL_MATCH",
        ),
        (
            True,
            True,
            ("manifest",),
            "draft_partial",
            "UPLOAD_MISSING_APPROVED_ASSETS",
            "EXPECTED_ASSETS_PARTIAL_MATCH",
        ),
        (
            True,
            True,
            ("archive", "manifest"),
            "draft_complete",
            "VERIFY_REMOTE_ASSETS_AND_PUBLISH_PRERELEASE",
            "EXPECTED_ASSETS_COMPLETE_MATCH",
        ),
        (
            True,
            False,
            ("archive", "manifest"),
            "published_complete",
            "RUN_REMOTE_CONSUMER_PROOF",
            "RELEASE_MATCHING_PRERELEASE",
        ),
    ],
    ids=[
        "unstarted",
        "tag-only",
        "draft-empty",
        "draft-archive-only",
        "draft-manifest-only",
        "draft-complete",
        "published-complete",
    ],
)
def test_remote_classification_semantics_derive_states_from_facts(
    remote_observation_inputs: dict[str, Any],
    tag_present: bool,
    draft: bool | None,
    present_assets: tuple[str, ...],
    state: str,
    action: str,
    reason: str,
) -> None:
    inputs = remote_observation_inputs
    observation = inputs["observation"]
    if not tag_present:
        observation["tag_observation"] = {"status": "ABSENT"}
    if draft is None:
        observation["release_observation"] = {"status": "ABSENT"}
    else:
        observation["release_observation"]["is_draft"] = draft
    for kind in ("archive", "manifest"):
        if kind not in present_assets:
            observation["asset_observations"][kind] = {"status": "ABSENT"}
    _replace_simulated_remote_observation(inputs)
    original = deepcopy(observation)
    _validate_review_schema(observation, "remote-observation.schema.json")

    sealed = artifact_contract.seal_remote_observation(
        observation, **_remote_observation_binding(inputs)
    )
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )

    _validate_review_schema(sealed, "remote-observation.schema.json")
    _validate_review_schema(classified, "remote-classification.schema.json")
    assert sealed == original == observation
    assert classified["state"] == state
    assert classified["allowed_action"] == action
    assert classified["reason_codes"] == [reason]
    assert classified["admission"] == "ADMITTED"
    assert classified["classification_result"] == {
        "expected": "EXPECT_SUCCESS",
        "observed": "SUCCESS",
        "status": "PASS",
    }
    assert classified["promotion_attempt_id"] == inputs["current_attempt_id"]
    assert classified["candidate"] == inputs["candidate_identity"]["candidate"]
    assert classified["candidate_identity_record"] == observation["candidate_identity_record"]
    assert classified["remote_observed_state_reference"] == inputs["observation_reference"]
    assert classified["classification_id"] == "simulated-classification-001"
    assert classified["classified_at"] == inputs["classified_at"]


@pytest.mark.parametrize(
    ("violation", "reason"),
    [
        ("retained_expired", "RETAINED_ARTIFACT_EXPIRED"),
        ("retained_unavailable", "RETAINED_ARTIFACT_UNAVAILABLE"),
        ("retained_mismatch", "RETAINED_ARTIFACT_IDENTITY_MISMATCH"),
        ("retained_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("retained_false_success", "REMOTE_READBACK_INCOMPLETE"),
        ("readback_unavailable", "REMOTE_READBACK_UNREADABLE"),
        ("readback_unauthorized", "REMOTE_READBACK_UNREADABLE"),
        ("readback_incomplete", "REMOTE_READBACK_INCOMPLETE"),
        ("readback_rejected", "REMOTE_READBACK_INCOMPLETE"),
        ("readback_failed_complete", "REMOTE_READBACK_INCOMPLETE"),
        ("tag_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("release_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("archive_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("manifest_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("archive_present_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("manifest_present_unreadable", "REMOTE_READBACK_UNREADABLE"),
        ("lightweight_tag", "TAG_NON_ANNOTATED"),
        ("tag_name", "TAG_TARGET_MISMATCH"),
        ("tag_target", "TAG_TARGET_MISMATCH"),
        ("release_tag", "RELEASE_METADATA_MISMATCH"),
        ("release_metadata", "RELEASE_METADATA_MISMATCH"),
        ("not_prerelease", "RELEASE_STATE_MISMATCH"),
        ("release_without_tag", "RELEASE_STATE_MISMATCH"),
        ("assets_without_release", "REMOTE_READBACK_INCOMPLETE"),
        ("published_empty", "RELEASE_STATE_MISMATCH"),
        ("published_partial", "RELEASE_STATE_MISMATCH"),
        ("unexpected_asset", "UNEXPECTED_ASSET_PRESENT"),
        ("duplicate_asset_id", "REMOTE_READBACK_INCOMPLETE"),
        ("archive_negative_identity", "ASSET_IDENTITY_MISMATCH"),
        ("manifest_negative_identity", "ASSET_IDENTITY_MISMATCH"),
        ("archive_failed_download", "ASSET_IDENTITY_MISMATCH"),
        ("manifest_failed_download", "ASSET_IDENTITY_MISMATCH"),
    ],
)
def test_remote_classification_semantics_preserve_negative_facts_as_collision(
    remote_observation_inputs: dict[str, Any],
    violation: str,
    reason: str,
) -> None:
    inputs = remote_observation_inputs
    observation = inputs["observation"]
    assets = observation["asset_observations"]
    if violation.startswith("retained_"):
        retained = observation["retained_artifact_availability"]
        if violation == "retained_false_success":
            retained["observed_result"] = "FAILURE"
        else:
            retained["status"] = {
                "retained_expired": "EXPIRED",
                "retained_unavailable": "UNAVAILABLE",
                "retained_mismatch": "IDENTITY_MISMATCH",
                "retained_unreadable": "UNREADABLE",
            }[violation]
            retained["observed_result"] = "REFUSAL"
    elif violation.startswith("readback_"):
        observation["readback_status"].update(
            {
                "observed_result": "FAILURE",
                "reason_code": {
                    "readback_unavailable": "READBACK_UNAVAILABLE",
                    "readback_unauthorized": "READBACK_UNAUTHORIZED",
                    "readback_incomplete": "READBACK_INCOMPLETE",
                    "readback_rejected": "READBACK_EVIDENCE_REJECTED",
                    "readback_failed_complete": "READBACK_COMPLETE",
                }[violation],
            }
        )
    elif violation in ("tag_unreadable", "release_unreadable"):
        kind = violation.split("_")[0]
        observation[kind + "_observation"] = {
            "status": "UNREADABLE",
            "reason_code": kind.upper() + "_READBACK_UNAVAILABLE",
        }
    elif violation.endswith("unreadable"):
        kind = violation.split("_")[0]
        asset = assets[kind]
        assets[kind] = (
            {
                "status": "PRESENT_UNREADABLE",
                "metadata": asset["metadata"],
                "reason_code": kind.upper() + "_DOWNLOAD_UNAVAILABLE",
            }
            if "present" in violation
            else {"status": "UNREADABLE", "reason_code": kind.upper() + "_READBACK_UNAVAILABLE"}
        )
    elif violation == "lightweight_tag":
        observation["tag_observation"]["tag_type"] = "LIGHTWEIGHT"
    elif violation in ("tag_name", "tag_target"):
        observation["tag_observation"]["tag" if violation == "tag_name" else "peeled_commit"] = (
            "stateless-preview-v9.9.9-preview.9" if violation == "tag_name" else "a" * 40
        )
    elif violation == "release_tag":
        observation["release_observation"]["tag"] = "stateless-preview-v9.9.9-preview.9"
    elif violation == "release_metadata":
        observation["release_observation"]["destination_metadata_status"] = "MISMATCHED"
    elif violation == "not_prerelease":
        observation["release_observation"]["is_prerelease"] = False
    elif violation == "release_without_tag":
        observation["tag_observation"] = {"status": "ABSENT"}
    elif violation == "assets_without_release":
        observation["release_observation"] = {"status": "ABSENT"}
    elif violation.startswith("published_"):
        observation["release_observation"]["is_draft"] = False
        assets["archive"] = {"status": "ABSENT"}
        if violation == "published_empty":
            assets["manifest"] = {"status": "ABSENT"}
    elif violation == "unexpected_asset":
        assets["unexpected_asset_ids"] = ["simulated-unexpected-asset"]
    elif violation == "duplicate_asset_id":
        assets["manifest"]["metadata"]["id"] = assets["archive"]["metadata"]["id"]
    else:
        kind = violation.split("_")[0]
        if violation.endswith("negative_identity"):
            assets[kind]["identity_status"] = "DOWNLOADED_SHA256_MISMATCH"
        else:
            assets[kind]["observed_result"] = "FAILURE"
    _replace_simulated_remote_observation(inputs)
    original = deepcopy(observation)
    _validate_review_schema(observation, "remote-observation.schema.json")

    sealed = artifact_contract.seal_remote_observation(
        observation, **_remote_observation_binding(inputs)
    )
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )

    assert sealed == original == observation
    _validate_review_schema(classified, "remote-classification.schema.json")
    assert classified["state"] == "collision"
    assert classified["admission"] == "REFUSED"
    assert classified["allowed_action"] == "REFUSE"
    assert classified["reason_codes"] == [reason]
    assert classified["classification_result"] == {
        "expected": "EXPECT_SUCCESS",
        "observed": "REFUSAL",
        "status": "REFUSED",
    }


@pytest.mark.parametrize("kind", ["archive", "manifest"])
@pytest.mark.parametrize(
    "field",
    ["metadata_name", "metadata_size", "downloaded_name", "downloaded_size", "downloaded_sha256"],
)
def test_remote_classification_semantics_recompute_each_asset_identity(
    remote_observation_inputs: dict[str, Any],
    kind: str,
    field: str,
) -> None:
    inputs = remote_observation_inputs
    asset = inputs["observation"]["asset_observations"][kind]
    container = asset["metadata" if field.startswith("metadata_") else "downloaded_identity"]
    if field.endswith("name"):
        container["name"] = container["name"].replace(VERSION, "9.9.9-preview.9")
    elif field.endswith("size"):
        container["size_bytes"] += 1
    else:
        container["sha256"] = "a" * 64
    _replace_simulated_remote_observation(inputs)
    _validate_review_schema(inputs["observation"], "remote-observation.schema.json")

    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )

    assert asset["identity_status"] == "MATCHES_CANDIDATE"
    assert classified["state"] == "collision"
    assert classified["allowed_action"] == "REFUSE"
    assert classified["reason_codes"] == ["ASSET_IDENTITY_MISMATCH"]


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        ("expired", "unreadable", "RETAINED_ARTIFACT_EXPIRED"),
        ("unavailable", "unreadable", "RETAINED_ARTIFACT_UNAVAILABLE"),
        ("identity_mismatch", "unreadable", "RETAINED_ARTIFACT_IDENTITY_MISMATCH"),
        ("unreadable", "lightweight", "REMOTE_READBACK_UNREADABLE"),
        ("incomplete", "lightweight", "REMOTE_READBACK_INCOMPLETE"),
        ("lightweight", "asset", "TAG_NON_ANNOTATED"),
        ("tag", "release", "TAG_TARGET_MISMATCH"),
        ("release", "unexpected", "RELEASE_METADATA_MISMATCH"),
        ("release_state", "unexpected", "RELEASE_STATE_MISMATCH"),
        ("unexpected", "asset", "UNEXPECTED_ASSET_PRESENT"),
    ],
)
def test_remote_classification_semantics_use_first_applicable_refusal(
    remote_observation_inputs: dict[str, Any],
    first: str,
    second: str,
    reason: str,
) -> None:
    inputs = remote_observation_inputs
    observation = inputs["observation"]
    for fault in (first, second):
        if fault in ("expired", "unavailable", "identity_mismatch"):
            observation["retained_artifact_availability"]["status"] = fault.upper()
        elif fault == "unreadable":
            observation["asset_observations"]["manifest"] = {
                "status": "UNREADABLE",
                "reason_code": "MANIFEST_READBACK_UNAUTHORIZED",
            }
        elif fault == "incomplete":
            observation["readback_status"]["reason_code"] = "READBACK_INCOMPLETE"
        elif fault == "lightweight":
            observation["tag_observation"]["tag_type"] = "LIGHTWEIGHT"
        elif fault == "tag":
            observation["tag_observation"]["peeled_commit"] = "a" * 40
        elif fault == "release":
            observation["release_observation"]["destination_metadata_status"] = "MISMATCHED"
        elif fault == "release_state":
            observation["release_observation"]["is_prerelease"] = False
        elif fault == "unexpected":
            observation["asset_observations"]["unexpected_asset_ids"] = ["simulated-extra"]
        else:
            observation["asset_observations"]["archive"]["downloaded_identity"]["sha256"] = "a" * 64
    _replace_simulated_remote_observation(inputs)
    _validate_review_schema(observation, "remote-observation.schema.json")

    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )

    assert classified["state"] == "collision"
    assert classified["reason_codes"] == [reason]


@pytest.mark.parametrize(
    "violation",
    ["empty", "nonalphabet", "whitespace", "padding", "truncated", "not_zip", "truncated_zip"],
)
def test_decode_evidence_bundle_refuses_invalid_base64_or_zip(
    evidence_bundle_records: dict[str, bytes], violation: str
) -> None:
    records = evidence_bundle_records
    raw_zip = _evidence_bundle_zip(list(records.items()))
    encoded = base64.b64encode(raw_zip).decode("ascii")
    if violation == "empty":
        encoded = ""
    elif violation == "nonalphabet":
        encoded = "!" + encoded[1:]
    elif violation == "whitespace":
        encoded += "\n"
    elif violation == "padding":
        encoded += "===="
    elif violation == "truncated":
        encoded = encoded[:-1]
    elif violation == "not_zip":
        encoded = base64.b64encode(b"not a ZIP archive").decode("ascii")
    else:
        encoded = base64.b64encode(raw_zip[:-22]).decode("ascii")

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            encoded, {name: _sha256_bytes(raw) for name, raw in records.items()}
        )


@pytest.mark.parametrize("zip_size", [45_000, 45_001])
def test_decode_evidence_bundle_bounds_encoded_and_decoded_wire_bytes(
    evidence_bundle_records: dict[str, bytes], zip_size: int
) -> None:
    records = evidence_bundle_records
    members = list(records.items())
    raw_zip = _evidence_bundle_zip(members)
    raw_zip = _evidence_bundle_zip(members, comment=b"x" * (zip_size - len(raw_zip)))
    encoded = base64.b64encode(raw_zip).decode("ascii")
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    assert len(raw_zip) == zip_size

    if zip_size == 45_000:
        assert len(encoded) == 60_000
        assert artifact_contract.decode_evidence_bundle(encoded, expected_sha256) == records
    else:
        assert len(encoded) > 60_000
        with pytest.raises(ValueError):
            artifact_contract.decode_evidence_bundle(encoded, expected_sha256)


@pytest.mark.parametrize(
    ("sizes", "accepted"),
    [
        ((128 * 1024, 128 * 1024 - 2, 2), True),
        ((128 * 1024 + 1, 2, 2), False),
        ((128 * 1024, 128 * 1024 - 1, 2), False),
    ],
    ids=["exact-member-and-total-limits", "member-limit-exceeded", "total-limit-exceeded"],
)
def test_decode_evidence_bundle_bounds_actual_decompressed_records(
    sizes: tuple[int, int, int], accepted: bool
) -> None:
    records = {
        name: b"{}" + b" " * (size - 2) for name, size in zip(_EVIDENCE_BUNDLE_MEMBERS, sizes)
    }
    raw_zip = _evidence_bundle_zip(list(records.items()))
    encoded = base64.b64encode(raw_zip).decode("ascii")
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    assert len(raw_zip) <= 45_000 and len(encoded) <= 60_000

    if accepted:
        assert artifact_contract.decode_evidence_bundle(encoded, expected_sha256) == records
    else:
        with pytest.raises(ValueError):
            artifact_contract.decode_evidence_bundle(encoded, expected_sha256)


@pytest.mark.parametrize(
    "unexpected_name",
    [
        "../consumer-proof.json",
        "/consumer-proof.json",
        "folder/consumer-proof.json",
        "folder/../consumer-proof.json",
        "..\\consumer-proof.json",
        "C:\\consumer-proof.json",
        "consumer-proof.json/",
        "CONSUMER-PROOF.JSON",
        "manifest.json",
    ],
)
def test_decode_evidence_bundle_refuses_nonfixed_or_unsafe_member_names(
    evidence_bundle_records: dict[str, bytes], unexpected_name: str
) -> None:
    records = evidence_bundle_records
    members = list(records.items())
    members[0] = (unexpected_name, members[0][1])
    encoded = base64.b64encode(_evidence_bundle_zip(members)).decode("ascii")

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            encoded, {name: _sha256_bytes(raw) for name, raw in records.items()}
        )


@pytest.mark.parametrize("violation", ["missing", "extra", "duplicate", "casefold_collision"])
def test_decode_evidence_bundle_requires_exactly_three_unique_members(
    evidence_bundle_records: dict[str, bytes], violation: str
) -> None:
    records = evidence_bundle_records
    members = list(records.items())
    if violation == "missing":
        members.pop()
    elif violation == "extra":
        members.append(("manifest.json", b"{}"))
    elif violation == "duplicate":
        members.append(members[0])
    else:
        members.append((members[0][0].upper(), members[0][1]))
    if violation == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            raw_zip = _evidence_bundle_zip(members)
    else:
        raw_zip = _evidence_bundle_zip(members)

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


@pytest.mark.parametrize("kind", ["symlink", "directory", "reparse"])
def test_decode_evidence_bundle_refuses_unsafe_zip_entry_metadata(
    evidence_bundle_records: dict[str, bytes], kind: str
) -> None:
    records = evidence_bundle_records
    member = zipfile.ZipInfo(_EVIDENCE_BUNDLE_MEMBERS[0])
    member.create_system = 3
    if kind == "symlink":
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
    elif kind == "directory":
        member.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
    else:
        member.create_system = 0
        member.external_attr = 0x400 | 0x20
    members = list(records.items())
    members[0] = (member, members[0][1])

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(_evidence_bundle_zip(members)).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


def test_decode_evidence_bundle_refuses_encrypted_zip_entries(
    evidence_bundle_records: dict[str, bytes],
) -> None:
    records = evidence_bundle_records
    raw_zip = bytearray(_evidence_bundle_zip(list(records.items())))
    for offset in (6, raw_zip.index(b"PK\x01\x02") + 8):
        flags = int.from_bytes(raw_zip[offset : offset + 2], "little") | 1
        raw_zip[offset : offset + 2] = flags.to_bytes(2, "little")

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


def test_decode_evidence_bundle_refuses_corrupt_crc_even_with_matching_raw_hash(
    evidence_bundle_records: dict[str, bytes],
) -> None:
    records = evidence_bundle_records
    name = _EVIDENCE_BUNDLE_MEMBERS[0]
    original = records[name]
    raw_zip = bytearray(_evidence_bundle_zip(list(records.items()), compression=zipfile.ZIP_STORED))
    offset = raw_zip.index(original) + len(original) - 1
    raw_zip[offset] = ord(" ")
    records[name] = original[:-1] + b" "

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


@pytest.mark.parametrize(
    "violation",
    [
        "state",
        "allowed_action",
        "admission",
        "approved",
        "version",
        "unsafe_id",
        "missing_attempt",
        "invalid_status",
        "invalid_retained_status",
        "invalid_readback_reason",
        "invalid_expected_result",
        "invalid_observed_result",
        "invalid_identity_status",
        "bool_size",
        "zero_size",
        "bool_draft",
        "invalid_object_id",
        "extra_nested_field",
        "missing_download_identity",
        "duplicate_unexpected_ids",
        "too_many_unexpected_ids",
    ],
)
def test_remote_observation_semantics_refuse_unclosed_or_invalid_records(
    remote_observation_inputs: dict[str, Any],
    violation: str,
) -> None:
    inputs = remote_observation_inputs
    observation = inputs["observation"]
    if violation in ("state", "allowed_action", "admission", "approved"):
        observation[violation] = {
            "state": "draft_complete",
            "allowed_action": "RUN_REMOTE_CONSUMER_PROOF",
            "admission": "ADMITTED",
            "approved": True,
        }[violation]
    elif violation == "version":
        observation["observation_schema_version"] = "2.0"
    elif violation == "unsafe_id":
        observation["observation_id"] = "../simulated-observation"
    elif violation == "missing_attempt":
        del observation["promotion_attempt_id"]
    elif violation == "invalid_status":
        observation["tag_observation"]["status"] = "MISSING"
    elif violation == "invalid_retained_status":
        observation["retained_artifact_availability"]["status"] = "AVAILABLE"
    elif violation == "invalid_readback_reason":
        observation["readback_status"]["reason_code"] = "CLEAN"
    elif violation == "invalid_expected_result":
        observation["readback_status"]["expected_result"] = "EXPECT_FAILURE"
    elif violation == "invalid_observed_result":
        observation["readback_status"]["observed_result"] = "CLEAN"
    elif violation == "invalid_identity_status":
        observation["asset_observations"]["archive"]["identity_status"] = "VERIFIED"
    elif violation in ("bool_size", "zero_size"):
        observation["asset_observations"]["archive"]["metadata"]["size_bytes"] = (
            True if violation == "bool_size" else 0
        )
    elif violation == "bool_draft":
        observation["release_observation"]["is_draft"] = 1
    elif violation == "invalid_object_id":
        observation["tag_observation"]["object_id"] = "not-a-commit"
    elif violation == "extra_nested_field":
        observation["tag_observation"]["diagnostic"] = "simulated raw diagnostic"
    elif violation == "missing_download_identity":
        del observation["asset_observations"]["manifest"]["downloaded_identity"]
    elif violation in ("duplicate_unexpected_ids", "too_many_unexpected_ids"):
        observation["asset_observations"]["unexpected_asset_ids"] = (
            ["simulated-extra", "simulated-extra"]
            if violation == "duplicate_unexpected_ids"
            else [f"simulated-extra-{index}" for index in range(65)]
        )
    _replace_simulated_remote_observation(inputs)
    with pytest.raises(ValidationError):
        _validate_review_schema(observation, "remote-observation.schema.json")
    with pytest.raises(ValueError):
        artifact_contract.seal_remote_observation(
            observation, **_remote_observation_binding(inputs)
        )
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation", ["missing", "extra", "empty", "not_hex", "uppercase", "aliased_hash"]
)
def test_decode_evidence_bundle_refuses_unclosed_or_invalid_expected_hash_mapping(
    evidence_bundle_records: dict[str, bytes], violation: str
) -> None:
    records = evidence_bundle_records
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    name = _EVIDENCE_BUNDLE_MEMBERS[0]
    if violation == "missing":
        expected_sha256.pop(name)
    elif violation == "extra":
        expected_sha256["manifest.json"] = _sha256_bytes(b"{}")
    elif violation == "empty":
        expected_sha256[name] = ""
    elif violation == "not_hex":
        expected_sha256[name] = "g" * 64
    elif violation == "uppercase":
        expected_sha256[name] = expected_sha256[name].upper()
    else:
        expected_sha256 = dict.fromkeys(_EVIDENCE_BUNDLE_MEMBERS, expected_sha256[name])

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(_evidence_bundle_zip(list(records.items()))).decode("ascii"),
            expected_sha256,
        )


@pytest.mark.parametrize("changed_member", _EVIDENCE_BUNDLE_MEMBERS)
def test_decode_evidence_bundle_checks_each_original_raw_hash(
    evidence_bundle_records: dict[str, bytes], changed_member: str
) -> None:
    records = evidence_bundle_records
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    records[changed_member] += b" "

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(_evidence_bundle_zip(list(records.items()))).decode("ascii"),
            expected_sha256,
        )


@pytest.mark.parametrize(
    "raw_record",
    [
        b"",
        b'{"label":"\xff"}',
        b"\xef\xbb\xbf{}",
        "{}".encode("utf-16"),
        b'{"value":}',
        b"{} {}",
        b"[]",
        b"null",
        b'"record"',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b'{"value":-1e999}',
        b'{"outcome":"FAIL","outcome":"PASS"}',
        b'{"nested":{"value":1,"value":2}}',
        b'{"a":1,"\\u0061":2}',
    ],
    ids=[
        "empty",
        "invalid-utf8",
        "utf8-bom",
        "utf16",
        "malformed",
        "trailing-record",
        "array",
        "null",
        "string",
        "nan",
        "infinity",
        "negative-infinity",
        "overflow",
        "negative-overflow",
        "duplicate-key",
        "nested-duplicate",
        "escaped-duplicate",
    ],
)
def test_decode_evidence_bundle_refuses_nonobject_or_nonstrict_json_despite_matching_hash(
    evidence_bundle_records: dict[str, bytes], raw_record: bytes
) -> None:
    records = evidence_bundle_records
    records[_EVIDENCE_BUNDLE_MEMBERS[-1]] = raw_record

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(_evidence_bundle_zip(list(records.items()))).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_decode_evidence_bundle_refuses_other_zip_codecs(
    evidence_bundle_records: dict[str, bytes], compression: int
) -> None:
    records = evidence_bundle_records
    raw_zip = _evidence_bundle_zip(list(records.items()), compression=compression)

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


@pytest.mark.parametrize("field", ["compression", "encryption", "crc"])
def test_decode_evidence_bundle_refuses_local_header_disagreement(
    evidence_bundle_records: dict[str, bytes], field: str
) -> None:
    records = evidence_bundle_records
    raw_zip = bytearray(_evidence_bundle_zip(list(records.items())))
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        entry = archive.getinfo(_EVIDENCE_BUNDLE_MEMBERS[0])
    assert not entry.flag_bits & 0x08
    field_offset, width = {
        "compression": (8, 2),
        "encryption": (6, 2),
        "crc": (14, 4),
    }[field]
    offset = entry.header_offset + field_offset
    value = int.from_bytes(raw_zip[offset : offset + width], "little")
    value ^= zipfile.ZIP_DEFLATED if field == "compression" else 1
    raw_zip[offset : offset + width] = value.to_bytes(width, "little")

    with pytest.raises(ValueError):
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )


@pytest.fixture
def evidence_bundle_data_descriptor_zip(
    evidence_bundle_records: dict[str, bytes],
) -> bytes:
    class NonSeekableBuffer(io.BytesIO):
        def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
            raise io.UnsupportedOperation("non-seekable ZIP output")

    records = evidence_bundle_records
    buffer = NonSeekableBuffer()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in records.items():
            archive.writestr(name, raw)
    return buffer.getvalue()


def test_decode_evidence_bundle_accepts_zero_local_sizes_with_data_descriptors(
    evidence_bundle_records: dict[str, bytes],
    evidence_bundle_data_descriptor_zip: bytes,
) -> None:
    records = evidence_bundle_records
    raw_zip = evidence_bundle_data_descriptor_zip
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        for entry in archive.infolist():
            assert entry.flag_bits & 0x08
            assert raw_zip[entry.header_offset + 14 : entry.header_offset + 26] == b"\0" * 12

    assert (
        artifact_contract.decode_evidence_bundle(
            base64.b64encode(raw_zip).decode("ascii"),
            {name: _sha256_bytes(raw) for name, raw in records.items()},
        )
        == records
    )


@pytest.mark.parametrize(
    "violation", ["crc", "compressed_size", "raw_size", "truncated", "missing", "unsigned"]
)
def test_decode_evidence_bundle_checks_data_descriptor_metadata(
    evidence_bundle_records: dict[str, bytes],
    evidence_bundle_data_descriptor_zip: bytes,
    violation: str,
) -> None:
    records = evidence_bundle_records
    raw_zip = bytearray(evidence_bundle_data_descriptor_zip)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        entry = archive.getinfo(_EVIDENCE_BUNDLE_MEMBERS[-1])
    filename_length = int.from_bytes(
        raw_zip[entry.header_offset + 26 : entry.header_offset + 28], "little"
    )
    extra_length = int.from_bytes(
        raw_zip[entry.header_offset + 28 : entry.header_offset + 30], "little"
    )
    descriptor_offset = (
        entry.header_offset + 30 + filename_length + extra_length + entry.compress_size
    )
    assert raw_zip[descriptor_offset : descriptor_offset + 4] == b"PK\x07\x08"
    assert raw_zip[-22:-18] == b"PK\x05\x06"
    central_offset = int.from_bytes(raw_zip[-6:-2], "little")
    assert descriptor_offset + 16 == central_offset

    if violation in ("crc", "compressed_size", "raw_size"):
        offset = descriptor_offset + {"crc": 4, "compressed_size": 8, "raw_size": 12}[violation]
        value = int.from_bytes(raw_zip[offset : offset + 4], "little") ^ 1
        raw_zip[offset : offset + 4] = value.to_bytes(4, "little")
    else:
        removed = 16 if violation == "missing" else 4
        offset = descriptor_offset + (12 if violation == "truncated" else 0)
        del raw_zip[offset : offset + removed]
        raw_zip[-6:-2] = (central_offset - removed).to_bytes(4, "little")

    encoded = base64.b64encode(raw_zip).decode("ascii")
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    if violation == "unsigned":
        assert artifact_contract.decode_evidence_bundle(encoded, expected_sha256) == records
    else:
        with pytest.raises(ValueError):
            artifact_contract.decode_evidence_bundle(encoded, expected_sha256)


@pytest.mark.parametrize(
    ("compression", "added_flags", "accepted"),
    [
        (zipfile.ZIP_DEFLATED, 0x2000, False),
        (zipfile.ZIP_DEFLATED, 0x4000, False),
        (zipfile.ZIP_DEFLATED, 0x0800, True),
        (zipfile.ZIP_DEFLATED, 0x0006, True),
        (zipfile.ZIP_STORED, 0x0006, False),
    ],
    ids=[
        "masked-header-encryption",
        "reserved-flag",
        "utf8-with-descriptor",
        "deflate-options-with-descriptor",
        "stored-rejects-deflate-options",
    ],
)
def test_decode_evidence_bundle_accepts_only_supported_general_flags(
    evidence_bundle_records: dict[str, bytes],
    evidence_bundle_data_descriptor_zip: bytes,
    compression: int,
    added_flags: int,
    accepted: bool,
) -> None:
    records = evidence_bundle_records
    raw_zip = bytearray(
        evidence_bundle_data_descriptor_zip
        if compression == zipfile.ZIP_DEFLATED
        else _evidence_bundle_zip(list(records.items()), compression=compression)
    )
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        entry = archive.infolist()[0]
        central_offset = archive.start_dir
    assert raw_zip[central_offset : central_offset + 4] == b"PK\x01\x02"
    for offset in (entry.header_offset + 6, central_offset + 8):
        flags = int.from_bytes(raw_zip[offset : offset + 2], "little") | added_flags
        raw_zip[offset : offset + 2] = flags.to_bytes(2, "little")

    encoded = base64.b64encode(raw_zip).decode("ascii")
    expected_sha256 = {name: _sha256_bytes(raw) for name, raw in records.items()}
    if accepted:
        assert artifact_contract.decode_evidence_bundle(encoded, expected_sha256) == records
    else:
        with pytest.raises(ValueError):
            artifact_contract.decode_evidence_bundle(encoded, expected_sha256)


@pytest.mark.parametrize(
    "observed_at",
    ["2029-12-31T23:59:58", "2029-02-30T23:59:58Z"],
    ids=["naive_time", "invalid_time"],
)
def test_remote_observation_semantics_refuse_invalid_observation_timestamps(
    remote_observation_inputs: dict[str, Any],
    observed_at: str,
) -> None:
    inputs = remote_observation_inputs
    inputs["observation"]["observed_at"] = observed_at
    _replace_simulated_remote_observation(inputs)
    with pytest.raises(ValueError):
        artifact_contract.seal_remote_observation(
            inputs["observation"], **_remote_observation_binding(inputs)
        )
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize("field", ["repository", "run_id", "artifact_id", "path", "sha256"])
def test_remote_observation_semantics_bind_every_candidate_reference_field(
    remote_observation_inputs: dict[str, Any],
    field: str,
) -> None:
    inputs = remote_observation_inputs
    inputs["observation"]["candidate_identity_record"][field] = {
        "repository": "other/repository",
        "run_id": "999",
        "artifact_id": "999",
        "path": "other-candidate.json",
        "sha256": "a" * 64,
    }[field]
    _replace_simulated_remote_observation(inputs)
    with pytest.raises(ValueError):
        artifact_contract.seal_remote_observation(
            inputs["observation"], **_remote_observation_binding(inputs)
        )
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation",
    [
        "observation_candidate",
        "candidate_mapping",
        "candidate_raw_hash",
        "duplicate_candidate_key",
        "other_attempt",
    ],
)
def test_remote_observation_semantics_revalidate_raw_candidate_and_attempt(
    remote_observation_inputs: dict[str, Any],
    violation: str,
) -> None:
    inputs = remote_observation_inputs
    if violation == "observation_candidate":
        inputs["observation"]["candidate"]["preview_manifest"]["file"]["sha256"] = "a" * 64
    elif violation == "candidate_mapping":
        inputs["candidate_identity"]["candidate"]["preview_manifest"]["file"]["sha256"] = "a" * 64
    elif violation == "candidate_raw_hash":
        inputs["candidate_identity_bytes"] += b" "
    elif violation == "duplicate_candidate_key":
        raw = b'{"schema_version":"1.0",' + inputs["candidate_identity_bytes"][1:]
        inputs["candidate_identity_bytes"] = raw
        inputs["consumer_identity_reference"]["sha256"] = _sha256_bytes(raw)
        inputs["observation"]["candidate_identity_record"]["sha256"] = _sha256_bytes(raw)
    else:
        inputs["current_attempt_id"] = "simulated-other-attempt"
    _replace_simulated_remote_observation(inputs)
    with pytest.raises(ValueError):
        artifact_contract.seal_remote_observation(
            inputs["observation"], **_remote_observation_binding(inputs)
        )
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize("field", ["repository", "run_id", "artifact_id", "path", "sha256"])
def test_remote_classification_semantics_resolve_all_observation_reference_fields(
    remote_observation_inputs: dict[str, Any],
    field: str,
) -> None:
    inputs = remote_observation_inputs
    reference = deepcopy(inputs["observation_reference"])
    reference[field] = {
        "repository": "other/repository",
        "run_id": "999",
        "artifact_id": "999",
        "path": "other-observation.json",
        "sha256": "a" * 64,
    }[field]
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            reference, **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation",
    ["unavailable", "nonbytes", "raw_hash", "duplicate_key", "nonfinite_number", "array_document"],
)
def test_remote_classification_semantics_refuse_untrusted_resolved_bytes(
    remote_observation_inputs: dict[str, Any],
    violation: str,
) -> None:
    inputs = remote_observation_inputs
    path = inputs["observation_reference"]["path"]
    if violation == "unavailable":
        del inputs["remote_records"][path]
    elif violation == "nonbytes":
        inputs["remote_records"][path] = inputs["remote_records"][path].decode("utf-8")
    elif violation == "raw_hash":
        inputs["remote_records"][path] += b" "
    elif violation == "duplicate_key":
        _replace_simulated_remote_observation(
            inputs, b'{"observation_schema_version":"1.0",' + inputs["remote_records"][path][1:]
        )
    elif violation == "nonfinite_number":
        observation = inputs["observation"]
        observation["asset_observations"]["archive"]["metadata"]["size_bytes"] = float("nan")
        _replace_simulated_remote_observation(
            inputs, json.dumps(observation, allow_nan=True).encode("utf-8")
        )
    else:
        _replace_simulated_remote_observation(inputs, b"[]")
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


def test_remote_classification_semantics_accept_rehashed_equivalent_raw_json(
    remote_observation_inputs: dict[str, Any],
) -> None:
    inputs = remote_observation_inputs
    raw = json.dumps(inputs["observation"], indent=2).encode("utf-8")
    assert _sha256_bytes(raw) != inputs["observation_reference"]["sha256"]
    _replace_simulated_remote_observation(inputs, raw)
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )
    assert classified["state"] == "draft_complete"
    assert classified["remote_observed_state_reference"]["sha256"] == _sha256_bytes(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "other/repository"),
        ("workflow_path", ".github/workflows/publish.yml"),
        ("mode", "build"),
        ("event", "push"),
        ("run_id", "999"),
        ("run_attempt", True),
        ("run_attempt", 0),
        ("ref", "refs/heads/other"),
        ("commit", "a" * 40),
        ("approved", True),
    ],
)
def test_remote_classification_semantics_require_independent_current_workflow_context(
    remote_observation_inputs: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    inputs = remote_observation_inputs
    inputs["current_workflow_run"][field] = value
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize(
    "expires_at", ["2029-12-31T23:59:58Z", "2029-12-31T23:59:58.5Z", "2029-12-31T23:59:59Z"]
)
def test_remote_classification_semantics_apply_payload_expiry_at_both_observed_instants(
    remote_observation_inputs: dict[str, Any],
    expires_at: str,
) -> None:
    inputs = remote_observation_inputs
    candidate = inputs["candidate_identity"]["candidate"]
    candidate["build"]["artifact"]["retention"]["expires_at"] = expires_at
    inputs["candidate_identity_bytes"] = _canonical_json_bytes(inputs["candidate_identity"])
    digest = _sha256_bytes(inputs["candidate_identity_bytes"])
    inputs["consumer_identity_reference"]["sha256"] = digest
    inputs["observation"]["candidate_identity_record"]["sha256"] = digest
    inputs["observation"]["candidate"] = deepcopy(candidate)
    _replace_simulated_remote_observation(inputs)

    sealed = artifact_contract.seal_remote_observation(
        inputs["observation"], **_remote_observation_binding(inputs)
    )
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )

    assert sealed == inputs["observation"]
    assert sealed["retained_artifact_availability"]["status"] == "AVAILABLE_AND_VERIFIED"
    assert classified["state"] == "collision"
    assert classified["reason_codes"] == ["RETAINED_ARTIFACT_EXPIRED"]


@pytest.mark.parametrize(
    "classified_at", ["2029-12-31T23:59:57Z", "2029-12-31T23:59:59", "2029-02-30T23:59:59Z"]
)
def test_remote_classification_semantics_refuse_invalid_or_reversed_chronology(
    remote_observation_inputs: dict[str, Any],
    classified_at: str,
) -> None:
    inputs = remote_observation_inputs
    inputs["classified_at"] = classified_at
    with pytest.raises(ValueError):
        artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )


@pytest.mark.parametrize("classified_at", ["2029-12-31T23:59:58Z", "2030-01-01T00:59:58+01:00"])
def test_remote_classification_semantics_compare_instants_not_timestamp_strings(
    remote_observation_inputs: dict[str, Any],
    classified_at: str,
) -> None:
    inputs = remote_observation_inputs
    inputs["classified_at"] = classified_at
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )
    assert classified["state"] == "draft_complete"


def test_remote_classification_semantics_do_not_consult_wall_clock(
    remote_observation_inputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = remote_observation_inputs

    class NoWallClock(artifact_contract.datetime):
        @classmethod
        def now(cls, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("pure remote admission must not consult the wall clock")

    monkeypatch.setattr(artifact_contract, "datetime", NoWallClock)
    sealed = artifact_contract.seal_remote_observation(
        inputs["observation"], **_remote_observation_binding(inputs)
    )
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )
    assert sealed == inputs["observation"]
    assert classified["state"] == "draft_complete"


def test_remote_observation_semantics_outputs_are_detached_and_deeply_immutable(
    remote_observation_inputs: dict[str, Any],
) -> None:
    inputs = remote_observation_inputs
    original = deepcopy(inputs["observation"])
    reference = deepcopy(inputs["observation_reference"])
    workflow = deepcopy(inputs["current_workflow_run"])
    sealed = artifact_contract.seal_remote_observation(
        inputs["observation"], **_remote_observation_binding(inputs)
    )
    classified = artifact_contract.classify_remote_observation(
        inputs["observation_reference"], **_remote_classification_binding(inputs)
    )
    assert inputs["observation"] == original
    assert inputs["observation_reference"] == reference
    assert inputs["current_workflow_run"] == workflow
    inputs["observation"]["asset_observations"]["archive"]["metadata"]["size_bytes"] += 1
    inputs["observation_reference"]["sha256"] = "a" * 64
    inputs["candidate_identity"]["candidate"]["source"]["commit"] = "a" * 40
    assert sealed == original
    assert classified["remote_observed_state_reference"] == reference
    assert classified["candidate"] == original["candidate"]
    for record in (sealed, classified):
        with pytest.raises(TypeError):
            record["candidate"]["source"]["commit"] = "b" * 40
    with pytest.raises(TypeError):
        sealed["asset_observations"]["archive"]["metadata"]["size_bytes"] = 1
    with pytest.raises(TypeError):
        classified["reason_codes"].append("TAG_ABSENT")


def _replace_simulated_attempt_record(
    inputs: dict[str, Any], reference: dict[str, Any], record: Mapping[str, Any]
) -> None:
    raw = _canonical_json_bytes(record)
    inputs["records"][reference["path"]] = raw
    reference["sha256"] = _sha256_bytes(raw)


@pytest.fixture
def promotion_attempt_inputs(
    promotion_decision_inputs: dict[str, Any], remote_observation_inputs: dict[str, Any]
) -> dict[str, Any]:
    """SIMULATED Attempt 2.0 inputs; never live grants, approval, or effect authority."""
    inputs = promotion_decision_inputs
    # Keep the reused proof chain before the candidate's fixed 2030 retention expiry.
    for record, timestamp in (
        (inputs["consumer"], "recorded_at"),
        (inputs["aggregate"], "sealed_at"),
        (inputs["pr_review"], "recorded_at"),
        (inputs["stage_record"], "sealed_at"),
    ):
        record[timestamp] = "2029-12-31T23:59:50Z"
    for lens in inputs["aggregate"]["lens_results"]:
        lens["recorded_at"] = "2029-12-31T23:59:50Z"
    for index, key in enumerate(("consumer", "aggregate", "pr_review")):
        _replace_simulated_gate_record(inputs, index, inputs[key])
    _replace_simulated_decision_stage(inputs)
    inputs["decision_record"]["decided_at"] = "2029-12-31T23:59:51Z"
    source_commit = inputs["candidate_identity"]["candidate"]["source"]["commit"]
    pre_decision = inputs["decision_record"]["evidence"]["pre_decision_stage_evidence"]

    def retain(path: str, record: Mapping[str, Any]) -> dict[str, Any]:
        reference = {**pre_decision["record_reference"], "path": path}
        _replace_simulated_attempt_record(inputs, reference, record)
        return reference

    decision = artifact_contract.seal_promotion_decision(
        inputs["decision_record"], **_stage_binding(inputs)
    )
    decision_reference = retain("simulated-promotion-decision.json", decision)
    pre_publication_inputs = _simulated_stage_inputs(deepcopy(inputs), "pre-publication")
    pre_publication_inputs["stage_record"]["sealed_at"] = "2029-12-31T23:59:52Z"
    pre_publication = artifact_contract.seal_stage_gate_evidence(
        pre_publication_inputs["stage_record"], **_stage_binding(pre_publication_inputs)
    )
    inputs["pre_publication_record"] = json.loads(_canonical_json_bytes(pre_publication))
    pre_publication_receipt = {
        **deepcopy(pre_decision),
        "record_reference": retain("simulated-pre-publication-stage.json", pre_publication),
        "stage": "pre-publication",
    }

    inputs["current_attempt_id"] = "simulated-attempt-801-2-901"
    inputs["current_workflow_run"] = deepcopy(remote_observation_inputs["current_workflow_run"])
    inputs["observation"]["promotion_attempt_id"] = inputs["current_attempt_id"]
    inputs["observation"]["observed_at"] = "2029-12-31T23:59:54Z"
    inputs["classified_at"] = "2029-12-31T23:59:55Z"
    _replace_simulated_remote_observation(inputs)
    observation_reference = retain("simulated-remote-observation.json", inputs["observation"])
    inputs["classification"] = json.loads(
        _canonical_json_bytes(
            artifact_contract.classify_remote_observation(
                inputs["observation_reference"], **_remote_classification_binding(inputs)
            )
        )
    )
    classification_reference = retain(
        "simulated-remote-classification.json", inputs["classification"]
    )
    dispatcher = inputs["decision_record"]["promotion_attempt"]["authorized_dispatcher"]
    inputs["permission_report"] = {
        "diagnostic": "current-job-permissions",
        "meaning": "RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION",
        "status": "QUALIFIED",
        "availability": "AVAILABLE",
        "reason_code": "REQUIRED_RUNNER_GRANTS_REPORTED",
        "started_at": "2029-12-31T23:59:52Z",
        "finished_at": "2029-12-31T23:59:53Z",
        "binding": {
            "repository": REPOSITORY,
            "run_id": 801,
            "run_attempt": 2,
            "head_sha": source_commit,
            "job_key": "promote",
            "workflow_sha": source_commit,
            "job_name": "Promote canonical-main preview candidate",
            "job_id": 901,
            "setup_step_position": 0,
        },
        "actor_provenance": {
            "GITHUB_ACTOR": dispatcher["login"],
            "GITHUB_TRIGGERING_ACTOR": dispatcher["login"],
        },
        "grants": {
            "actions": "read",
            "contents": "write",
            "metadata": "read",
            "pullrequests": "read",
        },
        "log_sha256": _sha256_bytes(b"SIMULATED native setup permission log"),
        "log_source": "setup_step_logs",
        "requests": [
            {
                "phase": phase,
                "http_status": 200,
                "reason_code": "HTTP_RESPONSE",
                "started_at": "2029-12-31T23:59:52Z",
                "finished_at": "2029-12-31T23:59:53Z",
            }
            for phase in ("current_attempt_jobs", "setup_step_logs", "current_job_after")
        ],
    }
    inputs["current_promotion_context"] = {
        "current_attempt_id": inputs["current_attempt_id"],
        "workflow_run": deepcopy(inputs["current_workflow_run"]),
        "job_id": "901",
        "job_key": "promote",
        "workflow_sha": source_commit,
        "checkout_head": source_commit,
        "origin_main_target": source_commit,
        "current_github_actor": deepcopy(dispatcher),
        "current_github_triggering_actor": deepcopy(dispatcher),
    }
    inputs["attempt_record"] = {
        "schema_version": "2.0",
        "attempt_id": inputs["current_attempt_id"],
        "candidate_identity_record": deepcopy(inputs["candidate_identity_reference"]),
        "candidate_identity_record_sha256": _sha256_bytes(inputs["candidate_identity_bytes"]),
        "candidate": deepcopy(inputs["candidate_identity"]["candidate"]),
        "promotion_decision": decision_reference,
        "promotion_decision_sha256": decision_reference["sha256"],
        "pre_decision_stage_evidence": deepcopy(pre_decision),
        "pre_publication_stage_evidence": pre_publication_receipt,
        "authorization": {
            "decision_authorization": deepcopy(inputs["decision_record"]["promotion_attempt"]),
            "current_github_actor": deepcopy(dispatcher),
            "current_github_triggering_actor": deepcopy(dispatcher),
            "permission_readback": {
                "reference": retain(
                    "simulated-current-job-permissions.json", inputs["permission_report"]
                ),
                "run_attempt": 2,
                "job_key": "promote",
                "job_id": "901",
                "workflow_sha": source_commit,
                "execution_token": {
                    "source": "github.token",
                    "kind": "GITHUB_APP_INSTALLATION_TOKEN",
                    "identity_observation": "UNOBSERVED",
                    "app_id": None,
                    "installation_id": None,
                },
                "permissions": {"actions": "read", "contents": "write"},
            },
        },
        "workflow_run": deepcopy(inputs["current_workflow_run"]),
        "remote_observation": observation_reference,
        "remote_classification": classification_reference,
        "admission_status": "PASS",
        "recorded_at": "2029-12-31T23:59:56Z",
    }
    return inputs


def _attempt_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_stage_binding(inputs),
        "current_promotion_context": inputs["current_promotion_context"],
    }


def test_promotion_attempt_schema_accepts_simulated_2_0_with_eight_field_workflow(
    promotion_attempt_inputs: dict[str, Any],
) -> None:
    inputs = promotion_attempt_inputs
    _validate_review_schema(inputs["attempt_record"], "promotion-attempt.schema.json")
    assert set(inputs["current_promotion_context"]["workflow_run"]) == {
        "repository",
        "workflow_path",
        "mode",
        "event",
        "run_id",
        "run_attempt",
        "ref",
        "commit",
    }


@pytest.mark.parametrize(
    "violation",
    ["legacy", "authenticated_actor", "missing_triggering_actor", "missing_token_id", "extra"],
)
def test_promotion_attempt_schema_refuses_legacy_or_open_identity_shape(
    promotion_attempt_inputs: dict[str, Any], violation: str
) -> None:
    record = promotion_attempt_inputs["attempt_record"]
    authorization = record["authorization"]
    if violation == "legacy":
        record["schema_version"] = "1.0"
    elif violation == "authenticated_actor":
        authorization["permission_readback"]["authenticated_actor"] = deepcopy(
            authorization["current_github_actor"]
        )
    elif violation == "missing_triggering_actor":
        del authorization["current_github_triggering_actor"]
    elif violation == "missing_token_id":
        del authorization["permission_readback"]["execution_token"]["installation_id"]
    else:
        authorization["permission_readback"]["execution_token"]["owner"] = "simulated-dispatcher"
    with pytest.raises(ValidationError):
        _validate_review_schema(record, "promotion-attempt.schema.json")


def test_promotion_attempt_semantics_seal_simulated_approval_for_distinct_dispatcher(
    promotion_attempt_inputs: dict[str, Any],
) -> None:
    inputs = promotion_attempt_inputs
    original = deepcopy(inputs["attempt_record"])
    context = deepcopy(inputs["current_promotion_context"])

    sealed = artifact_contract.seal_promotion_attempt(original, **_attempt_binding(inputs))

    _validate_review_schema(sealed, "promotion-attempt.schema.json")
    assert sealed == inputs["attempt_record"] == original
    assert inputs["current_promotion_context"] == context


def test_promotion_attempt_semantics_output_is_detached_and_deeply_immutable(
    promotion_attempt_inputs: dict[str, Any],
) -> None:
    inputs = promotion_attempt_inputs
    original = deepcopy(inputs["attempt_record"])
    sealed = artifact_contract.seal_promotion_attempt(
        inputs["attempt_record"], **_attempt_binding(inputs)
    )
    inputs["attempt_record"]["authorization"]["current_github_actor"]["id"] = "999"
    inputs["current_promotion_context"]["workflow_run"]["commit"] = "a" * 40
    assert sealed == original
    for branch, field, value in (
        (sealed["workflow_run"], "commit", "a" * 40),
        (sealed["authorization"]["current_github_triggering_actor"], "id", "999"),
        (sealed["authorization"]["permission_readback"]["execution_token"], "app_id", "999"),
    ):
        with pytest.raises(TypeError):
            branch[field] = value


@pytest.mark.parametrize("log_source", ["setup_step_logs", "job_logs"])
def test_promotion_attempt_semantics_accept_rehashed_equivalent_permission_bytes(
    promotion_attempt_inputs: dict[str, Any],
    log_source: str,
) -> None:
    inputs = promotion_attempt_inputs
    report = inputs["permission_report"]
    report["log_source"] = log_source
    if log_source == "job_logs":
        report["requests"][1]["http_status"] = 404
        report["requests"].insert(2, {**report["requests"][2], "phase": "job_logs"})
    reference = inputs["attempt_record"]["authorization"]["permission_readback"]["reference"]
    raw = json.dumps(inputs["permission_report"], indent=4, sort_keys=False).encode("utf-8")
    inputs["records"][reference["path"]] = raw
    reference["sha256"] = _sha256_bytes(raw)

    sealed = artifact_contract.seal_promotion_attempt(
        inputs["attempt_record"], **_attempt_binding(inputs)
    )

    assert sealed["authorization"]["permission_readback"]["reference"] == reference


@pytest.mark.parametrize(
    "document",
    [
        "candidate",
        "catalog",
        "decision",
        "pre-decision",
        "pre-publication",
        "permissions",
        "observation",
        "classification",
    ],
)
def test_promotion_attempt_semantics_reject_changed_raw_bytes(
    promotion_attempt_inputs: dict[str, Any], document: str
) -> None:
    inputs = promotion_attempt_inputs
    record = inputs["attempt_record"]
    if document in ("candidate", "catalog"):
        field = (
            "candidate_identity_bytes" if document == "candidate" else "release_gate_catalog_bytes"
        )
        inputs[field] += b" "
    else:
        reference = {
            "decision": record["promotion_decision"],
            "pre-decision": record["pre_decision_stage_evidence"]["record_reference"],
            "pre-publication": record["pre_publication_stage_evidence"]["record_reference"],
            "permissions": record["authorization"]["permission_readback"]["reference"],
            "observation": record["remote_observation"],
            "classification": record["remote_classification"],
        }[document]
        inputs["records"][reference["path"]] += b" "

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(record, **_attempt_binding(inputs))


@pytest.mark.parametrize(
    ("branch", "field", "value"),
    [
        (None, "current_attempt_id", "simulated-previous-attempt"),
        (None, "job_id", "999"),
        (None, "job_key", "probe_permissions"),
        (None, "workflow_sha", "a" * 40),
        (None, "checkout_head", "a" * 40),
        (None, "origin_main_target", "a" * 40),
        ("workflow_run", "run_id", "999"),
        ("workflow_run", "run_attempt", 1),
        ("workflow_run", "commit", "a" * 40),
        ("workflow_run", "workflow_sha", "a" * 40),
        ("current_github_actor", "id", "999"),
        ("current_github_actor", "login", "simulated-other-dispatcher"),
        ("current_github_triggering_actor", "id", "999"),
        ("current_github_triggering_actor", "login", "simulated-other-rerunner"),
        ("current_github_triggering_actor", "id", 102),
    ],
)
def test_promotion_attempt_semantics_require_independent_current_identity(
    promotion_attempt_inputs: dict[str, Any], branch: str | None, field: str, value: Any
) -> None:
    inputs = promotion_attempt_inputs
    context = inputs["current_promotion_context"]
    target = context if branch is None else context[branch]
    target[field] = value

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(
            inputs["attempt_record"], **_attempt_binding(inputs)
        )


def test_promotion_attempt_semantics_require_explicit_current_context(
    promotion_attempt_inputs: dict[str, Any],
) -> None:
    inputs = promotion_attempt_inputs
    with pytest.raises(TypeError):
        artifact_contract.seal_promotion_attempt(inputs["attempt_record"], **_stage_binding(inputs))


@pytest.mark.parametrize("violation", ["absent", "extra", "missing_triggering_actor"])
def test_promotion_attempt_semantics_require_closed_current_context(
    promotion_attempt_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_attempt_inputs
    if violation == "absent":
        inputs["current_promotion_context"] = None
    elif violation == "extra":
        inputs["current_promotion_context"]["approved"] = True
    else:
        del inputs["current_promotion_context"]["current_github_triggering_actor"]
    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(
            inputs["attempt_record"], **_attempt_binding(inputs)
        )


@pytest.mark.parametrize(
    ("branch", "field", "value"),
    [
        ("binding", "repository", "other/repository"),
        ("binding", "run_id", 999),
        ("binding", "run_attempt", 1),
        ("binding", "job_id", 999),
        ("binding", "job_id", "901"),
        ("binding", "head_sha", "a" * 40),
        ("binding", "workflow_sha", "a" * 40),
        ("binding", "setup_step_position", -1),
        ("grants", "contents", "read"),
        ("grants", "actions", "none"),
        ("actor_provenance", "GITHUB_TRIGGERING_ACTOR", "simulated-other-rerunner"),
        (None, "status", "INSUFFICIENT"),
        (None, "availability", "UNAVAILABLE"),
        (None, "meaning", "WORKFLOW_DECLARED_PERMISSIONS"),
        (None, "reason_code", "REQUIRED_RUNNER_GRANTS_NOT_REPORTED"),
        (None, "log_source", "workflow_yaml"),
        (None, "log_sha256", None),
        (None, "log_sha256", "not-a-hash"),
        (None, "started_at", "not-a-timestamp"),
        (None, "finished_at", "2029-12-31T23:59:51Z"),
        (None, "finished_at", "2029-12-31T23:59:57Z"),
        (None, "declared_permissions", {"actions": "read", "contents": "write"}),
    ],
)
def test_promotion_attempt_semantics_reject_unqualified_permission_report(
    promotion_attempt_inputs: dict[str, Any], branch: str | None, field: str, value: Any
) -> None:
    inputs = promotion_attempt_inputs
    report = inputs["permission_report"]
    target = report if branch is None else report[branch]
    target[field] = value
    reference = inputs["attempt_record"]["authorization"]["permission_readback"]["reference"]
    _replace_simulated_attempt_record(inputs, reference, report)

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(
            inputs["attempt_record"], **_attempt_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation",
    ["probe_report", "unavailable", "missing_log_hash", "missing_binding", "missing_timestamp"],
)
def test_promotion_attempt_semantics_do_not_substitute_probe_or_unavailable_readback(
    promotion_attempt_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_attempt_inputs
    report = inputs["permission_report"]
    if violation == "probe_report":
        report["binding"].update(
            job_key="probe_permissions", job_name="Probe current job permissions"
        )
    elif violation == "unavailable":
        report.update(
            status="UNAVAILABLE",
            availability="UNAVAILABLE",
            reason_code="CURRENT_JOB_LOGS_UNAVAILABLE",
            grants={},
            log_sha256=None,
        )
    elif violation == "missing_log_hash":
        del report["log_sha256"]
    elif violation == "missing_binding":
        del report["binding"]["workflow_sha"]
    else:
        del report["finished_at"]
    reference = inputs["attempt_record"]["authorization"]["permission_readback"]["reference"]
    _replace_simulated_attempt_record(inputs, reference, report)
    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(
            inputs["attempt_record"], **_attempt_binding(inputs)
        )


@pytest.mark.parametrize(
    ("branch", "field", "value"),
    [
        (None, "run_attempt", 1),
        (None, "job_key", "probe_permissions"),
        (None, "job_id", "999"),
        (None, "workflow_sha", "a" * 40),
        ("permissions", "contents", "read"),
        ("execution_token", "source", "PAT"),
        ("execution_token", "kind", "PERSONAL_ACCESS_TOKEN"),
        ("execution_token", "identity_observation", "OBSERVED"),
        ("execution_token", "app_id", "999"),
        ("execution_token", "installation_id", "999"),
        (None, "authenticated_actor", {"id": "102", "login": "simulated-dispatcher"}),
    ],
)
def test_promotion_attempt_semantics_bind_permission_projection_and_token_provenance(
    promotion_attempt_inputs: dict[str, Any], branch: str | None, field: str, value: Any
) -> None:
    inputs = promotion_attempt_inputs
    readback = inputs["attempt_record"]["authorization"]["permission_readback"]
    target = readback if branch is None else readback[branch]
    target[field] = value
    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(
            inputs["attempt_record"], **_attempt_binding(inputs)
        )


@pytest.mark.parametrize(
    "violation",
    [
        "legacy_1_0",
        "candidate_hash",
        "candidate_source",
        "decision_hash",
        "decline",
        "decision_candidate",
        "decision_authorization",
        "recorded_actor",
        "recorded_triggering_actor",
        "recorded_workflow",
        "predecision_other_record",
        "nested_review_incomplete",
        "prepublication_source",
        "prepublication_catalog",
        "missing_prepublication",
        "stale_observation",
        "stale_classification",
        "collision",
        "forged_classification",
        "post_publication_proof",
        "invalid_recorded_at",
        "attempt_before_classification",
    ],
)
def test_promotion_attempt_semantics_revalidate_decision_stages_and_remote_admission(
    promotion_attempt_inputs: dict[str, Any], violation: str
) -> None:
    inputs = promotion_attempt_inputs
    record = inputs["attempt_record"]
    if violation == "legacy_1_0":
        record["schema_version"] = "1.0"
    elif violation == "candidate_hash":
        record["candidate_identity_record_sha256"] = "a" * 64
    elif violation == "candidate_source":
        record["candidate"]["source"]["commit"] = "a" * 40
    elif violation == "decision_hash":
        record["promotion_decision_sha256"] = "a" * 64
    elif violation in ("decline", "decision_candidate", "nested_review_incomplete"):
        if violation == "decline":
            inputs["decision_record"]["decision"] = "DECLINE"
        elif violation == "decision_candidate":
            inputs["decision_record"]["candidate"]["build"]["artifact"]["id"] = "999"
        else:
            inputs["aggregate"]["lens_results"].pop()
            _replace_simulated_gate_record(inputs, 1, inputs["aggregate"])
            _replace_simulated_decision_stage(inputs)
            record["pre_decision_stage_evidence"] = deepcopy(
                inputs["decision_record"]["evidence"]["pre_decision_stage_evidence"]
            )
        _replace_simulated_attempt_record(
            inputs, record["promotion_decision"], inputs["decision_record"]
        )
        record["promotion_decision_sha256"] = record["promotion_decision"]["sha256"]
    elif violation == "decision_authorization":
        record["authorization"]["decision_authorization"]["authorized_dispatcher"]["id"] = "999"
    elif violation in ("recorded_actor", "recorded_triggering_actor"):
        role = (
            "current_github_actor"
            if violation == "recorded_actor"
            else "current_github_triggering_actor"
        )
        record["authorization"][role]["id"] = "999"
    elif violation == "recorded_workflow":
        record["workflow_run"]["run_attempt"] = 1
    elif violation == "predecision_other_record":
        reference = record["pre_decision_stage_evidence"]["record_reference"]
        reference["path"] = "simulated-other-pre-decision-stage.json"
        _replace_simulated_attempt_record(inputs, reference, inputs["stage_record"])
    elif violation in ("prepublication_source", "prepublication_catalog"):
        stage = inputs["pre_publication_record"]
        if violation == "prepublication_source":
            stage["source_commit"] = "a" * 40
            record["pre_publication_stage_evidence"]["source_commit"] = "a" * 40
        else:
            stage["release_gate_catalog"]["sha256"] = "a" * 64
        _replace_simulated_attempt_record(
            inputs, record["pre_publication_stage_evidence"]["record_reference"], stage
        )
    elif violation == "missing_prepublication":
        del inputs["records"][record["pre_publication_stage_evidence"]["record_reference"]["path"]]
    elif violation == "stale_observation":
        inputs["observation"]["promotion_attempt_id"] = "simulated-previous-attempt"
        _replace_simulated_attempt_record(
            inputs, record["remote_observation"], inputs["observation"]
        )
        inputs["classification"]["remote_observed_state_reference"] = deepcopy(
            record["remote_observation"]
        )
        _replace_simulated_attempt_record(
            inputs, record["remote_classification"], inputs["classification"]
        )
    elif violation in ("stale_classification", "forged_classification"):
        if violation == "stale_classification":
            inputs["classification"]["promotion_attempt_id"] = "simulated-previous-attempt"
        else:
            inputs["classification"]["allowed_action"] = "CREATE_DRAFT_PRERELEASE"
        _replace_simulated_attempt_record(
            inputs, record["remote_classification"], inputs["classification"]
        )
    elif violation == "collision":
        inputs["observation"]["asset_observations"]["unexpected_asset_ids"] = ["999"]
        _replace_simulated_remote_observation(inputs)
        _replace_simulated_attempt_record(
            inputs, record["remote_observation"], inputs["observation"]
        )
        classification = artifact_contract.classify_remote_observation(
            inputs["observation_reference"], **_remote_classification_binding(inputs)
        )
        _replace_simulated_attempt_record(inputs, record["remote_classification"], classification)
    elif violation == "post_publication_proof":
        record["post_publication_stage_evidence"] = deepcopy(
            record["pre_publication_stage_evidence"]
        )
    elif violation == "invalid_recorded_at":
        record["recorded_at"] = "not-a-timestamp"
    else:
        record["recorded_at"] = "2029-12-31T23:59:54Z"

    with pytest.raises(ValueError):
        artifact_contract.seal_promotion_attempt(record, **_attempt_binding(inputs))


class _EvidenceWorkflowCli:
    """Real CLI/files/sealers with only the GitHub process/HTTP edge simulated."""

    def __init__(
        self, inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.root = tmp_path / "checkout"
        shutil.copytree(inputs["repository_root"], self.root)
        self.runner = tmp_path / "runner"
        self.runner.mkdir()
        self.event_path = tmp_path / "event.json"
        self.output_path = tmp_path / "github-output"
        self.summary_path = tmp_path / "github-summary"
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.requests: list[str] = []
        self.clock = "2029-01-01T00:00:00Z"
        self.identity = deepcopy(inputs["candidate_identity"])
        candidate = self.identity["candidate"]
        candidate["build"]["artifact"]["name"] = f"stateless-preview-payload-{VERSION}-101-1"
        self.identity_bytes = _canonical_json_bytes(self.identity)
        self.source_commit = candidate["source"]["commit"]
        principal = {"id": 7106373, "login": "thebtf"}
        repository = {"id": 4001, "full_name": REPOSITORY}
        self.runs = {
            run_id: {
                "id": int(run_id),
                "run_attempt": attempt,
                "event": "workflow_dispatch",
                "path": ".github/workflows/stateless-preview.yml",
                "head_branch": "main",
                "head_sha": self.source_commit,
                "status": status,
                "conclusion": conclusion,
                "actor": dict(principal),
                "triggering_actor": dict(principal),
                "repository": dict(repository),
                "head_repository": dict(repository),
                "created_at": created_at,
                "updated_at": created_at,
            }
            for run_id, attempt, status, conclusion, created_at in (
                ("101", 1, "completed", "success", "2028-01-01T00:00:00Z"),
                ("201", 2, "in_progress", None, self.clock),
            )
        }
        # Share defaults so existing negative mutations also reach selected-attempt metadata.
        self.run_attempts = {
            (run_id, str(run["run_attempt"])): run for run_id, run in self.runs.items()
        }
        self.jobs = {
            "101": {
                "total_count": 1,
                "jobs": [
                    {
                        "id": 1001,
                        "run_id": 101,
                        "run_attempt": 1,
                        "head_sha": self.source_commit,
                        "name": "Build canonical-main preview candidate",
                        "status": "completed",
                        "conclusion": "success",
                        "completed_at": "2028-01-01T00:01:00Z",
                    }
                ],
            }
        }
        retained = inputs["repository_root"].parent / "retained"
        manifest = candidate["preview_manifest"]
        self.add_artifact(
            "501",
            candidate["build"]["artifact"]["name"],
            "101",
            {
                manifest["archive_reference"]["path"]: (
                    retained / manifest["archive_reference"]["path"]
                ).read_bytes(),
                manifest["manifest_reference"]["path"]: (
                    retained / manifest["manifest_reference"]["path"]
                ).read_bytes(),
            },
        )
        identity_reference = self.add_artifact(
            "601",
            f"stateless-preview-candidate-identity-{VERSION}-101-1",
            "101",
            {"candidate-identity.json": self.identity_bytes},
        )
        self.catalog = deepcopy(inputs["catalog"])
        self.catalog["catalog"]["resolved_at"] = "2028-01-01T00:02:00Z"
        catalog_reference = self.add_artifact(
            "701",
            f"stateless-preview-release-gate-catalog-{VERSION}-101-1",
            "101",
            {"release-gate-catalog.json": _canonical_json_bytes(self.catalog)},
        )
        compact_identity = self.compact(identity_reference)
        binding = {
            "candidate_identity_record": compact_identity,
            "candidate_identity_record_sha256": identity_reference["sha256"],
            "candidate": candidate,
        }
        consumer = deepcopy(inputs["consumer"])
        consumer.update(
            {
                "candidate_identity_record": deepcopy(identity_reference),
                "candidate": deepcopy(candidate),
                "recorded_at": "2028-01-02T00:00:00Z",
            }
        )
        consumer["download_origin"]["artifact"] = deepcopy(candidate["build"]["artifact"])
        aggregate = deepcopy(inputs["aggregate"])
        aggregate.update(deepcopy(binding))
        aggregate["sealed_at"] = "2028-01-03T00:00:00Z"
        for lens in aggregate["lens_results"]:
            lens.update(deepcopy(binding))
            lens["recorded_at"] = "2028-01-02T00:01:00Z"
        review = deepcopy(inputs["pr_review"])
        review.update(deepcopy(binding))
        review["recorded_at"] = "2028-01-04T00:00:00Z"
        self.records = {
            name: (json.dumps(record, ensure_ascii=False, indent=1) + "\r\n").encode("utf-8")
            for name, record in (
                ("consumer-proof.json", consumer),
                ("s2-s3-review.json", aggregate),
                ("independent-pr-review.json", review),
            )
        }
        self.request = {
            "candidate_identity_reference": identity_reference,
            "release_gate_catalog_reference": catalog_reference,
            "bundle_base64": "",
            "record_sha256": {},
            "decision": "DECLINE",
            "authorized_dispatcher": {"id": "17", "login": "release-bot"},
            "retention_days": "7",
        }
        self.inputs: dict[str, Any] = {
            "mode": "evidence",
            "evidence_operation": "retain",
            "evidence_request": "",
        }
        self.sync_request()
        self.event = {
            "inputs": self.inputs,
            "ref": "main",
            "repository": dict(repository),
            "sender": dict(principal),
        }
        environment = {
            **_build_environment(self.source_commit),
            "GITHUB_WORKFLOW_SHA": self.source_commit,
            "GITHUB_RUN_ID": "201",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_REPOSITORY_ID": "4001",
            "GITHUB_ACTOR": "thebtf",
            "GITHUB_ACTOR_ID": "7106373",
            "GITHUB_TRIGGERING_ACTOR": "thebtf",
            "GITHUB_JOB": "retain_evidence",
            "GITHUB_TOKEN": "test-only-token",
            "GITHUB_EVENT_PATH": str(self.event_path),
            "GITHUB_OUTPUT": str(self.output_path),
            "GITHUB_STEP_SUMMARY": str(self.summary_path),
            "RUNNER_TEMP": str(self.runner),
        }
        for name in tuple(os.environ):
            if name.startswith(("A1_", "GITHUB_")):
                monkeypatch.delenv(name)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        self.environment = os.environ
        monkeypatch.chdir(self.root)
        original_run = subprocess.run

        def process(command: Any, *args: Any, **kwargs: Any) -> Any:
            if command[0] == "git":
                assert command[1] in ("rev-parse", "cat-file", "show", "merge-base")
                return original_run(command, *args, **kwargs)
            assert command[0] == "gh", f"unexpected build/publish process: {command}"
            assert command[1] == "api", f"unexpected GitHub mutation: {command}"
            assert not any(
                value in command for value in ("POST", "PUT", "PATCH", "DELETE", "-f", "-F")
            )
            endpoint = next(value for value in command[2:] if value.startswith("repos/"))
            return subprocess.CompletedProcess(command, 0, self.response(endpoint), b"")

        def http(request: Any, *args: Any, **kwargs: Any) -> io.BytesIO:
            assert request.get_method() == "GET"
            prefix = "https://api.github.com/"
            assert request.full_url.startswith(prefix)
            return io.BytesIO(self.response(request.full_url.removeprefix(prefix)))

        real_datetime = artifact_contract.datetime
        workflow = self

        class WorkflowClock(real_datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                instant = real_datetime.fromisoformat(workflow.clock.replace("Z", "+00:00"))
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        monkeypatch.setattr(artifact_contract.subprocess, "run", process)
        monkeypatch.setattr(artifact_contract, "urlopen", http)
        monkeypatch.setattr(artifact_contract, "datetime", WorkflowClock)

    @staticmethod
    def compact(reference: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **{key: reference[key] for key in ("repository", "run_id", "path", "sha256")},
            "artifact_id": reference["artifact"]["id"],
        }

    def add_artifact(
        self, artifact_id: str, name: str, run_id: str, members: Mapping[str, bytes]
    ) -> dict[str, Any]:
        wire = _evidence_bundle_zip(list(members.items()))
        run = self.runs[run_id]
        self.artifacts[artifact_id] = {
            "wire": wire,
            "members": dict(members),
            "metadata": {
                "id": int(artifact_id),
                "name": name,
                "expired": False,
                "digest": f"sha256:{_sha256_bytes(wire)}",
                "expires_at": "2030-01-01T00:00:00Z",
                "created_at": run["created_at"],
                "workflow_run": {
                    "id": int(run_id),
                    "repository_id": 4001,
                    "head_repository_id": 4001,
                    "head_branch": "main",
                    "head_sha": self.source_commit,
                },
            },
        }
        path, raw = next(iter(members.items()))
        return {
            "repository": REPOSITORY,
            "run_id": run_id,
            "artifact": {
                "id": artifact_id,
                "name": name,
                "sha256": _sha256_bytes(wire),
                "retention": {"configured_days": 7, "expires_at": "2030-01-01T00:00:00Z"},
            },
            "path": path,
            "sha256": _sha256_bytes(raw),
        }

    def response(self, endpoint: str) -> bytes:
        self.requests.append(endpoint)
        prefix = f"repos/{REPOSITORY}/actions/"
        assert endpoint.startswith(prefix), f"unexpected GitHub target: {endpoint}"
        route = endpoint.removeprefix(prefix).partition("?")[0].split("/")
        if route[0] == "artifacts":
            artifact = self.artifacts[route[1]]
            if route[2:] == ["zip"]:
                return artifact["wire"]
            assert len(route) == 2
            return _canonical_json_bytes(artifact["metadata"])
        assert route[0] == "runs", f"unexpected GitHub effect: {endpoint}"
        if len(route) == 2:
            return _canonical_json_bytes(self.runs[route[1]])
        if len(route) == 4 and route[2] == "attempts":
            return _canonical_json_bytes(self.run_attempts[(route[1], route[3])])
        assert route[-1] == "jobs"
        if "attempts" in route:
            assert route[3] in ("1", "2")
        return _canonical_json_bytes(self.jobs[route[1]])

    def sync_request(self) -> None:
        self.request["bundle_base64"] = base64.b64encode(
            _evidence_bundle_zip(list(self.records.items()))
        ).decode("ascii")
        self.request["record_sha256"] = {
            name: _sha256_bytes(raw) for name, raw in self.records.items()
        }
        self.inputs["evidence_request"] = json.dumps(self.request, ensure_ascii=False)

    def replace_catalog(self) -> None:
        reference = self.request["release_gate_catalog_reference"]
        self.request["release_gate_catalog_reference"] = self.add_artifact(
            reference["artifact"]["id"],
            reference["artifact"]["name"],
            "101",
            {"release-gate-catalog.json": _canonical_json_bytes(self.catalog)},
        )
        self.sync_request()

    def invoke(self, command: str) -> int:
        self.event_path.write_text(json.dumps(self.event), encoding="utf-8")
        self.output_path.write_text("", encoding="utf-8")
        self.summary_path.touch(exist_ok=True)
        return artifact_contract.main([command])

    def outputs(self) -> dict[str, str]:
        return dict(
            line.split("=", 1) for line in self.output_path.read_text(encoding="utf-8").splitlines()
        )

    def upload(
        self, kind: str, admission: Mapping[str, str], members: Mapping[str, bytes]
    ) -> dict[str, Any]:
        artifact_id = {"records": "901", "stage": "902", "decision": "903"}[kind]
        reference = self.add_artifact(
            artifact_id, admission[f"{kind}_artifact_name"], "201", members
        )
        prefix = f"A1_EVIDENCE_{kind.upper()}_ARTIFACT"
        self.environment[f"{prefix}_ID"] = artifact_id
        self.environment[f"{prefix}_DIGEST"] = reference["artifact"]["sha256"]
        return reference

    def assert_not_staged(self) -> None:
        assert self.output_path.read_bytes() == b""
        for name in (*self.records, "pre-decision-stage.json", "promotion-decision.json"):
            assert not list(self.runner.rglob(name)), f"refused evidence was staged: {name}"
        assert not (self.root / "artifacts" / "stateless-preview").exists()


@pytest.fixture
def evidence_workflow_cli(
    semantic_review_inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _EvidenceWorkflowCli:
    return _EvidenceWorkflowCli(semantic_review_inputs, tmp_path, monkeypatch)


@pytest.mark.parametrize("mode", [None, "build"], ids=["legacy-three-inputs", "explicit-build"])
def test_evidence_cli_preserves_legacy_build_dispatch(
    evidence_workflow_cli: _EvidenceWorkflowCli, mode: str | None
) -> None:
    cli = evidence_workflow_cli
    cli.inputs.clear()
    cli.inputs.update(preview_version=VERSION, preview_tag=TAG, retention_days="7")
    if mode is not None:
        cli.inputs.update(mode=mode, evidence_operation="", evidence_request="")

    assert cli.invoke("admit-build") == 0

    assert cli.outputs() == {
        "preview_version": VERSION,
        "preview_tag": TAG,
        "retention_days": "7",
        "payload_artifact_name": f"stateless-preview-payload-{VERSION}-201-2",
        "candidate_identity_artifact_name": f"stateless-preview-candidate-identity-{VERSION}-201-2",
        "release_gate_catalog_artifact_name": f"stateless-preview-release-gate-catalog-{VERSION}-201-2",
    }
    assert not list(cli.runner.iterdir())


@pytest.mark.parametrize(
    "violation",
    [
        "unknown-mode",
        "empty-mode",
        "build-with-evidence",
        "unknown-input",
        "env-event-drift",
        "unknown-operation",
        "missing-operation",
        "retain-with-build",
        "retain-without-request",
        "probe-with-request",
        "probe-with-build",
        "build-command-for-evidence",
        "evidence-command-for-build",
    ],
)
def test_evidence_cli_refuses_unclosed_or_cross_mode_dispatch(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    build = {"preview_version": VERSION, "preview_tag": TAG, "retention_days": "7"}
    command = "admit-evidence"
    if violation in {
        "unknown-mode",
        "empty-mode",
        "build-with-evidence",
        "unknown-input",
        "env-event-drift",
    }:
        cli.inputs.clear()
        cli.inputs.update(build)
        command = "admit-build"
    if violation == "unknown-mode":
        cli.inputs["mode"] = "promote"
    elif violation == "empty-mode":
        cli.inputs["mode"] = ""
    elif violation == "build-with-evidence":
        cli.inputs["evidence_operation"] = "retain"
    elif violation == "unknown-input":
        cli.inputs["source_commit"] = cli.source_commit
    elif violation == "env-event-drift":
        cli.inputs["preview_version"] = "9.9.9-preview.1"
    elif violation == "unknown-operation":
        cli.inputs["evidence_operation"] = "publish"
    elif violation == "missing-operation":
        del cli.inputs["evidence_operation"]
    elif violation == "retain-with-build":
        cli.inputs.update(build)
    elif violation == "retain-without-request":
        del cli.inputs["evidence_request"]
    elif violation == "probe-with-request":
        cli.inputs["evidence_operation"] = "permission-probe"
    elif violation == "probe-with-build":
        cli.inputs.clear()
        cli.inputs.update(mode="evidence", evidence_operation="permission-probe", **build)
    elif violation == "build-command-for-evidence":
        command = "admit-build"
    elif violation == "evidence-command-for-build":
        cli.inputs.clear()
        cli.inputs.update(build)

    assert cli.invoke(command) == 1
    cli.assert_not_staged()


def test_evidence_cli_refuses_unknown_command_without_effects(
    evidence_workflow_cli: _EvidenceWorkflowCli,
) -> None:
    cli = evidence_workflow_cli
    with pytest.raises(SystemExit) as error:
        cli.invoke("promote")
    assert error.value.code == 2
    cli.assert_not_staged()
    assert cli.requests == []


@pytest.mark.parametrize("escaped_size", [65400, 65536], ids=["within-budget", "escaped-overflow"])
def test_evidence_cli_bounds_the_entire_escaped_inputs_payload(
    evidence_workflow_cli: _EvidenceWorkflowCli, escaped_size: int
) -> None:
    cli = evidence_workflow_cli
    initial = len(json.dumps(cli.inputs, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    padding = escaped_size - initial
    assert padding > 0
    cli.inputs["evidence_request"] = (
        "\t" * (padding // 2) + " " * (padding % 2) + cli.inputs["evidence_request"]
    )
    assert (
        len(json.dumps(cli.inputs, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        == escaped_size
    )
    assert len(cli.inputs["evidence_request"]) < 65535

    assert cli.invoke("admit-evidence") == (0 if escaped_size <= 65535 else 1)
    if escaped_size > 65535:
        cli.assert_not_staged()
    else:
        assert set(cli.outputs()) == {
            "records_directory",
            "stage_path",
            "decision_path",
            "records_artifact_name",
            "stage_artifact_name",
            "decision_artifact_name",
            "retention_days",
        }


@pytest.mark.parametrize(
    "violation",
    [
        "missing-decision",
        "empty-decision",
        "unknown-request-field",
        "caller-author",
        "unbounded-retention",
    ],
)
def test_evidence_cli_requires_closed_explicit_decision_request(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    if violation == "missing-decision":
        del cli.request["decision"]
    elif violation == "empty-decision":
        cli.request["decision"] = ""
    elif violation == "unknown-request-field":
        cli.request["download_url"] = "https://untrusted.example/evidence.zip"
    elif violation == "caller-author":
        cli.request["decision_author"] = {"id": "7106373", "login": "thebtf"}
    else:
        cli.request["retention_days"] = "91"
    cli.sync_request()

    assert cli.invoke("admit-evidence") == 1
    cli.assert_not_staged()


@pytest.mark.parametrize(
    "violation",
    [
        "actor-id",
        "actor-login",
        "actor-id-type",
        "event-sender",
        "runner-actor-id",
        "rerun-id",
        "rerun-login",
        "current-attempt",
        "workflow-source",
    ],
)
def test_evidence_cli_binds_authenticated_actor_and_rerun_context(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    run = cli.runs["201"]
    if violation == "actor-id":
        run["actor"]["id"] = 17
    elif violation == "actor-login":
        run["actor"]["login"] = "impostor"
    elif violation == "actor-id-type":
        run["actor"]["id"] = True
    elif violation == "event-sender":
        cli.event["sender"]["id"] = 17
    elif violation == "runner-actor-id":
        cli.environment["GITHUB_ACTOR_ID"] = "17"
    elif violation == "rerun-id":
        run["triggering_actor"]["id"] = 17
    elif violation == "rerun-login":
        run["triggering_actor"]["login"] = "impostor"
        cli.environment["GITHUB_TRIGGERING_ACTOR"] = "impostor"
    elif violation == "current-attempt":
        run["run_attempt"] = 1
    else:
        cli.environment["GITHUB_WORKFLOW_SHA"] = "a" * 40

    assert cli.invoke("admit-evidence") == 1
    cli.assert_not_staged()


@pytest.mark.parametrize(
    "violation",
    [
        "incomplete",
        "failed",
        "repository",
        "fork",
        "attempt",
        "sha",
        "workflow",
        "build-job-failed",
        "build-job-attempt",
        "candidate-artifact-owner",
        "catalog-artifact-owner",
    ],
)
def test_evidence_cli_requires_successful_exact_owned_source_build(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    run = cli.runs["101"]
    if violation == "incomplete":
        run.update(status="in_progress", conclusion=None)
    elif violation == "failed":
        run["conclusion"] = "failure"
    elif violation == "repository":
        run["repository"]["full_name"] = "other/netcoredbg-mcp"
    elif violation == "fork":
        run["head_repository"] = {"id": 4002, "full_name": "fork/netcoredbg-mcp"}
    elif violation == "attempt":
        run["run_attempt"] = 2
    elif violation == "sha":
        run["head_sha"] = "a" * 40
    elif violation == "workflow":
        run["path"] = ".github/workflows/other.yml"
    elif violation == "build-job-failed":
        cli.jobs["101"]["jobs"][0]["conclusion"] = "failure"
    elif violation == "build-job-attempt":
        cli.jobs["101"]["jobs"][0]["run_attempt"] = 2
    else:
        artifact_id = "601" if violation == "candidate-artifact-owner" else "701"
        cli.artifacts[artifact_id]["metadata"]["workflow_run"]["id"] = 999

    assert cli.invoke("admit-evidence") == 1
    cli.assert_not_staged()


def test_evidence_cli_admits_successful_selected_build_attempt_after_rerun(
    evidence_workflow_cli: _EvidenceWorkflowCli,
) -> None:
    cli = evidence_workflow_cli
    originals = dict(cli.records)
    cli.runs["101"] = {
        **cli.runs["101"],
        "run_attempt": 2,
        "status": "in_progress",
        "conclusion": None,
    }

    assert cli.invoke("admit-evidence") == 0

    directory = cli.runner.resolve() / "stateless-preview-evidence-201-2"
    assert cli.outputs() == {
        "records_directory": str(directory / "records"),
        "stage_path": str(directory / "pre-decision-stage.json"),
        "decision_path": str(directory / "promotion-decision.json"),
        "records_artifact_name": "stateless-preview-evidence-records-201-2",
        "stage_artifact_name": "stateless-preview-evidence-stage-201-2",
        "decision_artifact_name": "stateless-preview-evidence-decision-201-2",
        "retention_days": "7",
    }
    assert {path.name: path.read_bytes() for path in (directory / "records").iterdir()} == originals
    assert not (directory / "pre-decision-stage.json").exists()
    assert not (directory / "promotion-decision.json").exists()
    assert set(cli.artifacts) == {"501", "601", "701"}


def test_evidence_cli_refuses_last_lens_predating_source_build_before_e0_staging(
    evidence_workflow_cli: _EvidenceWorkflowCli,
) -> None:
    cli = evidence_workflow_cli
    name = "s2-s3-review.json"
    aggregate = json.loads(cli.records[name])
    assert len(aggregate["lens_results"]) == 7
    aggregate["lens_results"][-1]["recorded_at"] = "2028-01-01T00:00:59Z"
    assert (
        aggregate["lens_results"][-1]["recorded_at"]
        < cli.jobs["101"]["jobs"][0]["completed_at"]
        < aggregate["sealed_at"]
    )
    cli.records[name] = _canonical_json_bytes(aggregate)
    cli.sync_request()

    assert cli.invoke("admit-evidence") == 1
    cli.assert_not_staged()


@pytest.mark.parametrize(
    "violation",
    [
        "catalog-snapshot",
        "catalog-closure",
        "consumer-matrix",
        "missing-lens",
        "self-review",
        "false-review-denominator",
    ],
)
def test_evidence_cli_refuses_false_semantics_before_e0_staging(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    if violation.startswith("catalog-"):
        if violation == "catalog-snapshot":
            cli.catalog["catalog"]["policy_authority_snapshots"][0]["sha256"] = "0" * 64
        else:
            cli.catalog["catalog"]["gate_descriptors"].pop()
        cli.replace_catalog()
    else:
        name = {
            "consumer-matrix": "consumer-proof.json",
            "missing-lens": "s2-s3-review.json",
            "self-review": "independent-pr-review.json",
            "false-review-denominator": "independent-pr-review.json",
        }[violation]
        record = json.loads(cli.records[name])
        if violation == "consumer-matrix":
            record["scenario_matrix"]["results"].pop()
        elif violation == "missing-lens":
            record["lens_results"].pop()
        elif violation == "self-review":
            record["reviewer_provenance"]["reviewer_principal_id"] = record["reviewer_provenance"][
                "reviewed_author_principal_id"
            ]
        else:
            record["checks"] = []
        cli.records[name] = _canonical_json_bytes(record)
        cli.sync_request()

    assert cli.invoke("admit-evidence") == 1
    cli.assert_not_staged()


@pytest.mark.parametrize(
    "command", ["seal-evidence-stage", "seal-evidence-decision", "verify-evidence-retention"]
)
def test_evidence_cli_requires_real_predecessor_upload(
    evidence_workflow_cli: _EvidenceWorkflowCli, command: str
) -> None:
    cli = evidence_workflow_cli
    assert cli.invoke("admit-evidence") == 0
    admission = cli.outputs()

    assert cli.invoke(command) == 1
    assert not Path(admission["stage_path"]).exists()
    assert not Path(admission["decision_path"]).exists()
    assert set(cli.artifacts) == {"501", "601", "701"}


def test_evidence_cli_retains_decline_through_independent_three_artifact_readback(
    evidence_workflow_cli: _EvidenceWorkflowCli,
) -> None:
    cli = evidence_workflow_cli
    originals = dict(cli.records)
    assert cli.invoke("admit-evidence") == 0
    admission = cli.outputs()
    assert set(admission) == {
        "records_directory",
        "stage_path",
        "decision_path",
        "records_artifact_name",
        "stage_artifact_name",
        "decision_artifact_name",
        "retention_days",
    }
    assert admission["retention_days"] == "7"
    assert (
        len({admission[f"{kind}_artifact_name"] for kind in ("records", "stage", "decision")}) == 3
    )
    records_directory = Path(admission["records_directory"])
    stage_path = Path(admission["stage_path"])
    decision_path = Path(admission["decision_path"])
    assert stage_path.name == "pre-decision-stage.json"
    assert decision_path.name == "promotion-decision.json"
    for path in (records_directory, stage_path, decision_path):
        assert path.resolve().is_relative_to(cli.runner.resolve())
    assert {path.name: path.read_bytes() for path in records_directory.iterdir()} == originals
    assert not stage_path.exists() and not decision_path.exists()
    records_reference = cli.upload("records", admission, originals)
    for path in records_directory.iterdir():
        path.write_bytes(b"local bytes are not retained evidence\n")

    cli.clock = "2029-01-01T00:00:01Z"
    assert cli.invoke("seal-evidence-stage") == 0
    stage_bytes = stage_path.read_bytes()
    stage = json.loads(stage_bytes)
    descriptors = [
        value
        for value in cli.catalog["catalog"]["gate_descriptors"]
        if value["stage"] == "pre-decision"
    ]
    expected_names = ["consumer-proof.json", "s2-s3-review.json", "independent-pr-review.json"]
    payload_hash = cli.identity["candidate"]["build"]["artifact"]["sha256"]
    payload_members = cli.artifacts["501"]["members"]
    manifest = cli.identity["candidate"]["preview_manifest"]
    assert payload_hash == _sha256_bytes(
        payload_members[manifest["archive_reference"]["path"]]
        + payload_members[manifest["manifest_reference"]["path"]]
    )
    assert payload_hash != _sha256_bytes(cli.artifacts["501"]["wire"])
    assert stage["stage"] == "pre-decision" and stage["outcome"] == "PASS"
    assert len(stage["gate_evidence"]) == len(descriptors) == 3
    for entry, descriptor, name in zip(stage["gate_evidence"], descriptors, expected_names):
        assert {key: entry[key] for key in descriptor} == descriptor
        assert entry["record_reference"] == {
            "repository": REPOSITORY,
            "run_id": "201",
            "artifact_id": "901",
            "path": name,
            "sha256": _sha256_bytes(originals[name]),
        }
        assert (
            len(
                {
                    entry["record_reference"]["sha256"],
                    records_reference["artifact"]["sha256"],
                    payload_hash,
                }
            )
            == 3
        )
    stage_reference = cli.upload("stage", admission, {"pre-decision-stage.json": stage_bytes})
    stage_path.write_bytes(b"{}\n")

    cli.clock = "2029-01-01T00:00:02Z"
    assert cli.invoke("seal-evidence-decision") == 0
    decision_bytes = decision_path.read_bytes()
    decision = json.loads(decision_bytes)
    assert decision["decision"] == "DECLINE"
    assert decision["candidate"] == cli.identity["candidate"]
    assert decision["promotion_attempt"] == {
        "decision_author": {"id": "7106373", "login": "thebtf"},
        "authorized_dispatcher": cli.request["authorized_dispatcher"],
    }
    assert decision["evidence"]["pre_decision_stage_evidence"]["record_reference"] == cli.compact(
        stage_reference
    )
    assert stage["sealed_at"] <= decision["decided_at"]
    decision_reference = cli.upload(
        "decision", admission, {"promotion-decision.json": decision_bytes}
    )
    decision_path.write_bytes(b"{}\n")
    shutil.rmtree(cli.runner)
    cli.runner.mkdir()
    cli.requests.clear()
    cli.summary_path.write_text("", encoding="utf-8")

    assert cli.invoke("verify-evidence-retention") == 0

    for artifact_id in ("901", "902", "903"):
        assert f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip" in cli.requests
    summary = cli.summary_path.read_text(encoding="utf-8")
    for value in cli.compact(decision_reference).values():
        assert value in summary
    assert "APPROVE" not in summary
    assert cli.runs["201"]["status"] == "in_progress"
    assert cli.artifacts["901"]["members"] == originals
    assert set(cli.artifacts) == {"501", "601", "701", "901", "902", "903"}


@pytest.mark.parametrize(
    "violation",
    [
        "expired",
        "expiry-timestamp",
        "wire-tamper",
        "member-tamper",
        "wrong-run",
        "wrong-sha",
        "wrong-name",
        "wrong-upload-digest",
    ],
)
def test_evidence_cli_refuses_untrusted_uploaded_records(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    assert cli.invoke("admit-evidence") == 0
    admission = cli.outputs()
    cli.upload("records", admission, cli.records)
    artifact = cli.artifacts["901"]
    if violation == "expired":
        artifact["metadata"]["expired"] = True
    elif violation == "expiry-timestamp":
        artifact["metadata"]["expires_at"] = "2028-12-31T23:59:59Z"
    elif violation == "wire-tamper":
        artifact["wire"] += b"tamper"
    elif violation == "member-tamper":
        members = {**cli.records, "consumer-proof.json": b"{}\n"}
        artifact["wire"] = _evidence_bundle_zip(list(members.items()))
        digest = _sha256_bytes(artifact["wire"])
        artifact["metadata"]["digest"] = f"sha256:{digest}"
        cli.environment["A1_EVIDENCE_RECORDS_ARTIFACT_DIGEST"] = digest
    elif violation == "wrong-run":
        artifact["metadata"]["workflow_run"]["id"] = 999
    elif violation == "wrong-sha":
        artifact["metadata"]["workflow_run"]["head_sha"] = "a" * 40
    elif violation == "wrong-name":
        artifact["metadata"]["name"] = "foreign-records-201-1"
    else:
        cli.environment["A1_EVIDENCE_RECORDS_ARTIFACT_DIGEST"] = "0" * 64

    assert cli.invoke("seal-evidence-stage") == 1
    assert not Path(admission["stage_path"]).exists()
    assert not Path(admission["decision_path"]).exists()
    assert set(cli.artifacts) == {"501", "601", "701", "901"}


@pytest.mark.parametrize("changed_field", ["decision", "authorized_dispatcher"])
def test_evidence_cli_readback_refuses_decision_request_substitution(
    evidence_workflow_cli: _EvidenceWorkflowCli, changed_field: str
) -> None:
    cli = evidence_workflow_cli
    assert cli.invoke("admit-evidence") == 0
    admission = cli.outputs()
    cli.upload("records", admission, cli.records)
    assert cli.invoke("seal-evidence-stage") == 0
    cli.upload(
        "stage", admission, {"pre-decision-stage.json": Path(admission["stage_path"]).read_bytes()}
    )
    assert cli.invoke("seal-evidence-decision") == 0
    cli.upload(
        "decision",
        admission,
        {"promotion-decision.json": Path(admission["decision_path"]).read_bytes()},
    )
    cli.request[changed_field] = (
        "APPROVE" if changed_field == "decision" else {"id": "18", "login": "other-bot"}
    )
    cli.sync_request()
    shutil.rmtree(cli.runner)
    cli.runner.mkdir()

    assert cli.invoke("verify-evidence-retention") == 1
    assert set(cli.artifacts) == {"501", "601", "701", "901", "902", "903"}


def test_evidence_cli_permission_probe_admission_has_no_retain_build_or_publish_effects(
    evidence_workflow_cli: _EvidenceWorkflowCli,
) -> None:
    cli = evidence_workflow_cli
    cli.inputs.clear()
    cli.inputs.update(mode="evidence", evidence_operation="permission-probe")
    cli.environment["GITHUB_JOB"] = "probe_permissions"
    for name in ("A1_PREVIEW_VERSION", "A1_PREVIEW_TAG", "A1_RETENTION_DAYS"):
        del cli.environment[name]

    assert cli.invoke("admit-evidence") == 0

    assert not list(cli.runner.iterdir())
    assert not (cli.root / "artifacts").exists()
    assert set(cli.outputs()).isdisjoint(
        {
            "records_directory",
            "stage_path",
            "decision_path",
            "records_artifact_name",
            "stage_artifact_name",
            "decision_artifact_name",
            "payload_artifact_name",
            "candidate_identity_artifact_name",
            "release_gate_catalog_artifact_name",
        }
    )
    assert not any("/artifacts/" in endpoint for endpoint in cli.requests)
    assert set(cli.artifacts) == {"501", "601", "701"}


@pytest.mark.parametrize("command", ["prepare-payload", "seal-build-records"])
def test_evidence_cli_build_effect_commands_refuse_evidence_dispatch(
    evidence_workflow_cli: _EvidenceWorkflowCli, command: str
) -> None:
    cli = evidence_workflow_cli
    _create_publish_output(cli.root)
    if command == "seal-build-records":
        prepare_preview_payload(cli.root, cli.environment)
        cli.environment["A1_PAYLOAD_ARTIFACT_ID"] = "501"
        cli.add_artifact(
            "501",
            f"stateless-preview-payload-{VERSION}-201-2",
            "201",
            cli.artifacts["501"]["members"],
        )
    before = {
        path.relative_to(cli.root): path.read_bytes()
        for path in (cli.root / "artifacts").rglob("*")
        if path.is_file()
    }

    assert cli.invoke(command) == 1

    assert {
        path.relative_to(cli.root): path.read_bytes()
        for path in (cli.root / "artifacts").rglob("*")
        if path.is_file()
    } == before
    assert cli.requests == []


@pytest.mark.parametrize(
    "violation", ["records-wire", "stage-expired", "decision-owner", "stage-member"]
)
def test_evidence_cli_final_readback_rechecks_every_retained_artifact(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    assert cli.invoke("admit-evidence") == 0
    admission = cli.outputs()
    cli.upload("records", admission, cli.records)
    assert cli.invoke("seal-evidence-stage") == 0
    cli.upload(
        "stage", admission, {"pre-decision-stage.json": Path(admission["stage_path"]).read_bytes()}
    )
    assert cli.invoke("seal-evidence-decision") == 0
    cli.upload(
        "decision",
        admission,
        {"promotion-decision.json": Path(admission["decision_path"]).read_bytes()},
    )
    if violation == "records-wire":
        cli.artifacts["901"]["wire"] += b"tampered after sealing"
    elif violation == "stage-expired":
        cli.artifacts["902"]["metadata"]["expired"] = True
    elif violation == "decision-owner":
        cli.artifacts["903"]["metadata"]["workflow_run"]["id"] = 999
    else:
        artifact = cli.artifacts["902"]
        artifact["wire"] = _evidence_bundle_zip([("pre-decision-stage.json", b"{}\n")])
        digest = _sha256_bytes(artifact["wire"])
        artifact["metadata"]["digest"] = f"sha256:{digest}"
        cli.environment["A1_EVIDENCE_STAGE_ARTIFACT_DIGEST"] = digest
    shutil.rmtree(cli.runner)
    cli.runner.mkdir()

    assert cli.invoke("verify-evidence-retention") == 1
    assert set(cli.artifacts) == {"501", "601", "701", "901", "902", "903"}


@pytest.mark.parametrize(
    "command",
    [
        "admit-evidence",
        "seal-evidence-stage",
        "seal-evidence-decision",
        "verify-evidence-retention",
    ],
)
def test_evidence_cli_rejects_caller_output_path_options(
    evidence_workflow_cli: _EvidenceWorkflowCli, command: str
) -> None:
    cli = evidence_workflow_cli
    cli.event_path.write_text(json.dumps(cli.event), encoding="utf-8")
    destination = cli.runner / "caller-selected.json"

    with pytest.raises(SystemExit) as error:
        artifact_contract.main([command, "--output", str(destination)])

    assert error.value.code == 2
    assert not destination.exists()
    assert not list(cli.runner.iterdir())
    assert cli.requests == []


@pytest.mark.parametrize(
    "violation",
    ["missing-event-file", "invalid-event-json", "inputs-not-object", "non-string-input"],
)
def test_evidence_cli_requires_a_real_well_formed_dispatch_event(
    evidence_workflow_cli: _EvidenceWorkflowCli, violation: str
) -> None:
    cli = evidence_workflow_cli
    cli.inputs.clear()
    cli.inputs.update(preview_version=VERSION, preview_tag=TAG, retention_days="7")
    cli.output_path.write_text("", encoding="utf-8")
    if violation == "inputs-not-object":
        cli.event["inputs"] = []
    elif violation == "non-string-input":
        cli.inputs["retention_days"] = 7
    if violation != "missing-event-file":
        raw = "{" if violation == "invalid-event-json" else json.dumps(cli.event)
        cli.event_path.write_text(raw, encoding="utf-8")

    assert artifact_contract.main(["admit-build"]) == 1
    cli.assert_not_staged()


class _PromotionWorkflowCli(_EvidenceWorkflowCli):
    """SIMULATED GitHub/Git edges; dispatch, retained bytes and all sealers stay real.

    Workflow outputs use the existing *_path/*_artifact_name convention. P0 is
    admission, C_N classification, A attempt and O_N observation; uploads supply
    only the four A1_PROMOTE_*_ARTIFACT_{ID,DIGEST} environment pairs from the design.
    No test fixture manufactures a Promotion Attempt or a classification.
    """

    def __init__(
        self, inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.native_run = subprocess.run
        super().__init__(inputs, tmp_path, monkeypatch)
        self.request["decision"] = "APPROVE"
        self.sync_request()
        # Produce the predecessor graph through the already implemented evidence CLI.
        assert super().invoke("admit-evidence") == 0
        evidence = self.outputs()
        super().upload("records", evidence, self.records)
        self.clock = "2029-01-01T00:00:01Z"
        assert super().invoke("seal-evidence-stage") == 0
        super().upload(
            "stage",
            evidence,
            {"pre-decision-stage.json": Path(evidence["stage_path"]).read_bytes()},
        )
        self.clock = "2029-01-01T00:00:02Z"
        assert super().invoke("seal-evidence-decision") == 0
        self.decision_reference = self.compact(
            super().upload(
                "decision",
                evidence,
                {"promotion-decision.json": Path(evidence["decision_path"]).read_bytes()},
            )
        )
        self.decision = json.loads(self.artifacts["903"]["members"]["promotion-decision.json"])
        self.clock = "2029-01-02T00:00:00Z"
        self.seconds = 0
        dispatcher = self.request["authorized_dispatcher"]
        principal = {"id": int(dispatcher["id"]), "login": dispatcher["login"]}
        self.runs["801"] = {
            **deepcopy(self.runs["201"]),
            "id": 801,
            "actor": dict(principal),
            "triggering_actor": dict(principal),
            "created_at": self.clock,
            "updated_at": self.clock,
        }
        self.run_attempts[("801", "2")] = self.runs["801"]
        self.jobs["801"] = {
            "total_count": 1,
            "jobs": [
                {
                    "id": 901,
                    "run_id": 801,
                    "run_attempt": 2,
                    "head_sha": self.source_commit,
                    "name": "Promote canonical-main preview candidate",
                    "status": "in_progress",
                    "conclusion": None,
                    "url": f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/901",
                    "run_url": f"https://api.github.com/repos/{REPOSITORY}/actions/runs/801",
                    "steps": [
                        {
                            "name": "Set up job",
                            "number": 1,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                }
            ],
        }
        self.permission_report = deepcopy(inputs["permission_report"])
        self.permission_report["binding"].update(
            head_sha=self.source_commit, workflow_sha=self.source_commit
        )
        self.permission_report["actor_provenance"] = {
            "GITHUB_ACTOR": dispatcher["login"],
            "GITHUB_TRIGGERING_ACTOR": dispatcher["login"],
        }
        self.inputs.clear()
        self.inputs.update(mode="promote", preview_tag=TAG, promote_request="")
        self.event["sender"] = dict(principal)
        self.sync_promotion_request()
        for name, value in {
            "GITHUB_RUN_ID": "801",
            "GITHUB_ACTOR": dispatcher["login"],
            "GITHUB_ACTOR_ID": dispatcher["id"],
            "GITHUB_TRIGGERING_ACTOR": dispatcher["login"],
            "GITHUB_JOB": "promote",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        self.requests.clear()
        self.trace: list[tuple[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self.tag: dict[str, Any] | None = None
        self.tag_objects: dict[str, dict[str, Any]] = {}
        self.releases: list[dict[str, Any]] = []
        self.assets: dict[int, list[dict[str, Any]]] = {}
        self.asset_bytes: dict[int, bytes] = {}
        self.main_target = self.source_commit
        self.known_tag_oid: str | None = None
        self.tag_input: bytes | None = None
        self.push_race: str | None = None
        self.unknown_operation: str | None = None
        self.unknown_after_apply = False
        self.unreadable_reconciliation = False
        self.locations: dict[str, str] = {}
        self.retained: dict[tuple[str, int], dict[str, Any]] = {}
        self.admission_bytes: dict[str, bytes] = {}
        self.attempt_bytes: bytes | None = None
        monkeypatch.setattr(artifact_contract.subprocess, "run", self.process)
        monkeypatch.setattr(artifact_contract, "urlopen", self.http)
        monkeypatch.setattr(
            artifact_contract, "build_opener", lambda *handlers: SimpleNamespace(open=self.http)
        )

    def sync_promotion_request(self) -> None:
        self.inputs["promote_request"] = json.dumps(self.decision_reference, separators=(",", ":"))

    def invoke(self, command: str, *, step: int | None = None) -> int:
        self.seconds += 1
        self.clock = f"2029-01-02T00:{self.seconds // 60:02d}:{self.seconds % 60:02d}Z"
        self.event_path.write_text(json.dumps(self.event), encoding="utf-8")
        self.output_path.write_text("", encoding="utf-8")
        arguments = [command] if step is None else [command, "--step", str(step)]
        result = artifact_contract.main(arguments)
        self.locations.update(self.outputs())
        return result

    def write_permission_report(self) -> bytes:
        report = self.permission_report
        report["started_at"] = report["finished_at"] = self.clock
        for request in report["requests"]:
            request["started_at"] = request["finished_at"] = self.clock
        raw = (json.dumps(report, indent=1) + "\r\n").encode("utf-8")
        path = Path(self.locations["permission_path"])
        assert path.resolve().is_relative_to(self.runner.resolve())
        path.write_bytes(raw)
        return raw

    def retain(self, kind: str, step: int = 0) -> dict[str, Any]:
        if kind == "admission":
            paths = [
                Path(self.locations[key])
                for key in ("stage_path", "permission_path", "observation_path")
            ]
        elif kind == "observation":
            paths = [Path(self.locations["observation_path"])]
        else:
            paths = [Path(self.locations[f"{kind}_path"])]
        fragment = self.locations.get("pre_publication_verification_path")
        if (
            kind in ("admission", "observation")
            and fragment
            and Path(fragment).name == f"pre-publication-verification-{step}.json"
            and Path(fragment).is_file()
        ):
            paths.append(Path(fragment))
        for path in paths:
            assert path.resolve().is_relative_to(self.runner.resolve()) and not path.is_symlink()
        members = {path.name: path.read_bytes() for path in paths}
        artifact_id = str(1100 + len(self.retained))
        assert all(
            self.locations[f"{kind}_artifact_name"] != reference["artifact"]["name"]
            for reference in self.retained.values()
        ), "immutable workflow uploads must have distinct artifact names"
        reference = self.add_artifact(
            artifact_id, self.locations[f"{kind}_artifact_name"], "801", members
        )
        prefix = f"A1_PROMOTE_{kind.upper()}_ARTIFACT"
        self.environment[f"{prefix}_ID"] = artifact_id
        self.environment[f"{prefix}_DIGEST"] = reference["artifact"]["sha256"]
        self.retained[(kind, step)] = reference
        self.trace.append(("upload", artifact_id))
        if kind == "admission":
            self.admission_bytes = members
        elif kind == "attempt":
            self.attempt_bytes = members["promotion-attempt.json"]
        return reference

    def stage_and_observe(self) -> None:
        assert self.invoke("admit-promote") == 0
        assert self.locations["retention_days"] == "7"
        stage = json.loads(Path(self.locations["stage_path"]).read_bytes())
        _validate_review_schema(stage, "stage-gate-evidence.schema.json")
        assert stage["stage"] == "pre-publication" and stage["outcome"] == "PASS"
        assert stage["candidate"] == self.identity["candidate"]
        permission_raw = self.write_permission_report()
        assert self.invoke("observe-promote") == 0
        assert Path(self.locations["permission_path"]).read_bytes() == permission_raw
        assert self.mutations == []

    def classify_and_retain(self, step: int) -> dict[str, Any]:
        assert self.invoke("classify-promote", step=step) == 0
        record = json.loads(Path(self.locations["classification_path"]).read_bytes())
        _validate_review_schema(record, "remote-classification.schema.json")
        self.retain("classification", step)
        return record

    def admit_attempt(self) -> dict[str, Any]:
        self.stage_and_observe()
        self.retain("admission")
        initial = self.classify_and_retain(0)
        assert self.invoke("seal-promote-attempt") == 0
        attempt = json.loads(Path(self.locations["attempt_path"]).read_bytes())
        _validate_review_schema(attempt, "promotion-attempt.schema.json")
        assert attempt["schema_version"] == "2.0"
        assert attempt["attempt_id"] == "a1-promote-801-2-901"
        assert (
            attempt["authorization"]["current_github_actor"]
            == self.request["authorized_dispatcher"]
        )
        assert attempt["authorization"]["permission_readback"]["execution_token"] == {
            "source": "github.token",
            "kind": "GITHUB_APP_INSTALLATION_TOKEN",
            "identity_observation": "UNOBSERVED",
            "app_id": None,
            "installation_id": None,
        }
        self.retain("attempt")
        assert self.mutations == []
        return initial

    def advance_and_retain(self, step: int) -> dict[str, Any]:
        assert self.invoke("advance-promote", step=step) == 0
        observation = json.loads(Path(self.locations["observation_path"]).read_bytes())
        _validate_review_schema(observation, "remote-observation.schema.json")
        assert observation["promotion_attempt_id"] == "a1-promote-801-2-901"
        self.retain("observation", step)
        return self.classify_and_retain(step)

    def finish(self, step: int) -> dict[str, Any]:
        assert self.invoke("finish-promote", step=step) == 0
        path = Path(self.locations["handoff_path"])
        assert path.resolve().is_relative_to(self.runner.resolve())
        return json.loads(path.read_bytes())

    def remote_asset(self, kind: str, release_id: int = 1901) -> dict[str, Any]:
        descriptor = self.identity["candidate"]["preview_manifest"]
        descriptor = descriptor["contents"]["archive"] if kind == "archive" else descriptor["file"]
        raw = self.artifacts["501"]["members"][descriptor["name"]]
        asset_id = 2900 + len(self.asset_bytes) + 1
        asset = {
            "id": asset_id,
            "name": descriptor["name"],
            "size": len(raw),
            "state": "uploaded",
            "digest": f"sha256:{_sha256_bytes(raw)}",
            "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}",
        }
        self.assets.setdefault(release_id, []).append(asset)
        self.asset_bytes[asset_id] = raw
        return asset

    def seed_remote(self, state: str) -> None:
        if state == "unstarted":
            return
        oid = "d" * 40
        self.set_tag(oid)
        if state == "tag_only":
            return
        self.releases.append(self.release_metadata(1901, draft=state != "published_complete"))
        self.assets[1901] = []
        for kind in ("archive", "manifest"):
            if state in ("draft_complete", "published_complete", f"draft_{kind}_only"):
                self.remote_asset(kind)

    def set_tag(self, oid: str, *, target: str | None = None, kind: str = "tag") -> None:
        self.tag = {"ref": f"refs/tags/{TAG}", "object": {"type": kind, "sha": oid}}
        self.tag_objects[oid] = {
            "sha": oid,
            "tag": TAG,
            "object": {"type": "commit", "sha": target or self.source_commit},
        }

    def release_metadata(self, release_id: int, *, draft: bool = True) -> dict[str, Any]:
        return {
            "id": release_id,
            "tag_name": TAG,
            "target_commitish": self.source_commit,
            "name": TAG,
            "body": "",
            "draft": draft,
            "prerelease": True,
            "upload_url": (
                f"https://uploads.github.com/repos/{REPOSITORY}/releases/"
                f"{release_id}/assets{{?name,label}}"
            ),
            "url": f"https://api.github.com/repos/{REPOSITORY}/releases/{release_id}",
        }

    def replace_retained(self, artifact_id: str, path: str, raw: bytes) -> None:
        artifact = self.artifacts[artifact_id]
        artifact["members"][path] = raw
        artifact["wire"] = _evidence_bundle_zip(list(artifact["members"].items()))
        artifact["metadata"]["digest"] = f"sha256:{_sha256_bytes(artifact['wire'])}"

    def rewrite_decision(self, change: Any) -> None:
        change(self.decision)
        raw = _canonical_json_bytes(self.decision)
        self.replace_retained("903", "promotion-decision.json", raw)
        self.decision_reference["sha256"] = _sha256_bytes(raw)
        self.sync_promotion_request()

    def mutation(self, operation: str, target: str, raw: bytes) -> bool:
        self.mutations.append({"operation": operation, "target": target, "body": raw})
        self.trace.append(("mutation", operation))
        return operation == self.unknown_operation

    @staticmethod
    def not_found(endpoint: str) -> HTTPError:
        return HTTPError(
            f"https://api.github.com/{endpoint}", 404, "SIMULATED", HTTPMessage(), io.BytesIO(b"{}")
        )

    def response(self, endpoint: str) -> bytes:
        if not hasattr(self, "trace") or "/actions/" in endpoint:
            if hasattr(self, "trace"):
                self.trace.append(("read", endpoint))
            try:
                return super().response(endpoint)
            except KeyError as error:
                raise self.not_found(endpoint) from error
        self.requests.append(endpoint)
        self.trace.append(("read", endpoint))
        if self.unreadable_reconciliation and self.mutations:
            raise URLError("SIMULATED unavailable reconciliation")
        prefix = f"repos/{REPOSITORY}/"
        assert endpoint.startswith(prefix), f"unexpected publication target: {endpoint}"
        route, _, query = endpoint.removeprefix(prefix).partition("?")
        if route == "git/ref/heads/main":
            value: Any = {"ref": SOURCE_REF, "object": {"type": "commit", "sha": self.main_target}}
        elif route == f"git/ref/tags/{TAG}":
            if self.tag is None:
                raise self.not_found(endpoint)
            value = self.tag
        elif route.startswith("git/tags/"):
            oid = route.removeprefix("git/tags/")
            if oid not in self.tag_objects:
                raise self.not_found(endpoint)
            value = self.tag_objects[oid]
        elif route == "releases":
            page = int(parse_qs(query).get("page", ["1"])[0])
            value = self.releases[(page - 1) * 100 : page * 100]
        elif route.startswith("releases/tags/"):
            # GitHub's tag endpoint can miss a draft; listing must still discover it.
            raise self.not_found(endpoint)
        elif re.fullmatch(r"releases/\d+/assets", route):
            release_id = int(route.split("/")[1])
            page = int(parse_qs(query).get("page", ["1"])[0])
            value = self.assets.get(release_id, [])[(page - 1) * 100 : page * 100]
        elif re.fullmatch(r"releases/assets/\d+", route):
            asset_id = int(route.rsplit("/", 1)[1])
            self.trace.append(("download", asset_id))
            return self.asset_bytes[asset_id]
        elif re.fullmatch(r"releases/\d+", route):
            matches = [
                release for release in self.releases if release["id"] == int(route.split("/")[1])
            ]
            if len(matches) != 1:
                raise self.not_found(endpoint)
            value = {**matches[0], "assets": self.assets.get(matches[0]["id"], [])[:100]}
        else:
            raise AssertionError(f"unexpected publication read: {endpoint}")
        return _canonical_json_bytes(value)

    def api_write(self, method: str, endpoint: str, raw: bytes) -> bytes:
        prefix = f"repos/{REPOSITORY}/releases"
        if endpoint == prefix and method == "POST":
            body = json.loads(raw)
            assert body == {
                "tag_name": TAG,
                "target_commitish": self.source_commit,
                "name": TAG,
                "body": "",
                "draft": True,
                "prerelease": True,
                "generate_release_notes": False,
                "make_latest": "false",
            }
            operation = "create-draft"
            lost = self.mutation(operation, endpoint, raw)
            if lost and not self.unknown_after_apply:
                raise URLError(f"SIMULATED lost {operation} response")
            release = self.release_metadata(1901)
            self.releases.append(release)
            self.assets[1901] = []
        elif endpoint == f"{prefix}/1901" and method == "PATCH":
            assert json.loads(raw) == {"draft": False, "prerelease": True, "make_latest": "false"}
            operation = "publish"
            lost = self.mutation(operation, endpoint, raw)
            release = next(value for value in self.releases if value["id"] == 1901)
            if not lost or self.unknown_after_apply:
                release["draft"] = False
        else:
            self.mutation(f"forbidden-{method}", endpoint, raw)
            raise AssertionError(f"unexpected publication mutation: {method} {endpoint}")
        if lost:
            raise URLError(f"SIMULATED lost {operation} response")
        return _canonical_json_bytes(release)

    def process(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        if command[0] == "git":
            if command[1] == "config":
                assert command[2:] == ["--null", "--list", "--no-includes"]
                return self.native_run(command, *args, **kwargs)
            if command[1] in ("rev-parse", "cat-file", "show", "merge-base"):
                return self.native_run(command, *args, **kwargs)
            if command[1] == "mktag":
                raw = kwargs["input"]
                raw = raw.encode("utf-8") if isinstance(raw, str) else raw
                self.tag_input = raw
                self.known_tag_oid = hashlib.sha1(
                    b"tag " + str(len(raw)).encode("ascii") + b"\0" + raw
                ).hexdigest()
                wire = (self.known_tag_oid + "\n").encode("ascii")
                return subprocess.CompletedProcess(
                    command, 0, wire.decode() if kwargs.get("text") else wire, b""
                )
            assert command[1] == "push", f"unexpected native Git effect: {command}"
            lost = self.mutation("create-tag", " ".join(command), b"")
            assert self.known_tag_oid is not None
            assert command[2:] == [
                "--porcelain",
                "--no-follow-tags",
                f"--force-with-lease=refs/tags/{TAG}:",
                f"https://github.com/{REPOSITORY}.git",
                f"{self.known_tag_oid}:refs/tags/{TAG}",
            ]
            child = kwargs.get("env", self.environment)
            assert child.get("GIT_TERMINAL_PROMPT") == "0"
            assert int(child.get("GIT_CONFIG_COUNT", "0")) > 0
            config = [
                (child[f"GIT_CONFIG_KEY_{index}"], child[f"GIT_CONFIG_VALUE_{index}"])
                for index in range(int(child["GIT_CONFIG_COUNT"]))
            ]
            assert ("credential.helper", "") in config
            assert any(
                key.lower().endswith("followredirects") and value.lower() == "false"
                for key, value in config
            )
            assert any(
                key.lower().endswith("extraheader") and value.lower().startswith("authorization:")
                for key, value in config
            )
            assert self.environment["GITHUB_TOKEN"] not in " ".join(command)
            if self.push_race is not None:
                oid = self.known_tag_oid if self.push_race == "same-oid" else "e" * 40
                self.set_tag(oid)
                flag = "=" if self.push_race == "same-oid" else "!"
                wire = f"{flag}\trefs/tags/{TAG}:refs/tags/{TAG}\tSIMULATED race\n"
                return subprocess.CompletedProcess(
                    command,
                    0 if flag == "=" else 1,
                    wire if kwargs.get("text") else wire.encode(),
                    "SIMULATED" if kwargs.get("text") else b"SIMULATED",
                )
            assert self.tag is None, "publisher attempted to overwrite an existing tag"
            if not lost or self.unknown_after_apply:
                self.set_tag(self.known_tag_oid)
            if lost:
                raise subprocess.TimeoutExpired(command, 60)
            wire = f"*\t{self.known_tag_oid}:refs/tags/{TAG}\t[new tag]\n"
            return subprocess.CompletedProcess(
                command,
                0,
                wire if kwargs.get("text") else wire.encode(),
                "" if kwargs.get("text") else b"",
            )
        assert command[:2] == ["gh", "api"], f"unexpected build/auth/publication process: {command}"
        child = kwargs.get("env", self.environment)
        assert child.get("GITHUB_TOKEN") == "test-only-token"
        assert not child.get("GH_TOKEN"), "GH_TOKEN must not override the workflow token"
        endpoint = next(value for value in command[2:] if value.startswith("repos/"))
        method = (
            command[command.index("--method") + 1]
            if "--method" in command
            else (command[command.index("-X") + 1] if "-X" in command else "GET")
        )
        include_headers = "--include" in command or "-i" in command
        try:
            if method != "GET":
                assert "--input" in command and not any(value in command for value in ("-f", "-F"))
                source = command[command.index("--input") + 1]
                raw = kwargs.get("input", b"") if source == "-" else Path(source).read_bytes()
                raw = raw.encode("utf-8") if isinstance(raw, str) else raw
                wire = self.api_write(method, endpoint, raw)
            elif "--paginate" in command:
                pages = []
                page = 1
                while True:
                    separator = "&" if "?" in endpoint else "?"
                    result = self.response(f"{endpoint}{separator}page={page}")
                    pages.append(json.loads(result))
                    if len(pages[-1]) < 100:
                        break
                    page += 1
                wire = (
                    _canonical_json_bytes(pages)
                    if "--slurp" in command
                    else b"\n".join(_canonical_json_bytes(value) for value in pages)
                )
            else:
                wire = self.response(endpoint)
        except (HTTPError, URLError) as error:
            wire = b""
            if include_headers and isinstance(error, HTTPError):
                wire = (
                    f"HTTP/2.0 {error.code} SIMULATED\r\nContent-Type: application/json\r\n\r\n".encode()
                    + error.read()
                )
            return subprocess.CompletedProcess(command, 1, wire, b"SIMULATED transport failure")
        if include_headers:
            status = 201 if method == "POST" else 200
            wire = (
                f"HTTP/2.0 {status} SIMULATED\r\nContent-Type: application/json\r\n\r\n".encode()
                + wire
            )
        return subprocess.CompletedProcess(command, 0, wire, b"")

    def http(self, request: Any, *args: Any, **kwargs: Any) -> addinfourl:
        url = request.full_url
        assert request.get_header("Authorization", "").endswith(" test-only-token")
        if url.startswith("https://uploads.github.com/"):
            assert request.get_method() == "POST"
            parsed = urlsplit(url)
            assert parsed.path == f"/repos/{REPOSITORY}/releases/1901/assets"
            query = parse_qs(parsed.query)
            assert set(query) == {"name"} and len(query["name"]) == 1
            name = query["name"][0]
            kind = "archive" if name.endswith(".zip") else "manifest"
            expected = self.identity["candidate"]["preview_manifest"]
            descriptor = expected["contents"]["archive"] if kind == "archive" else expected["file"]
            assert name == descriptor["name"]
            raw = request.data
            lost = self.mutation(f"upload-{kind}", url, raw)
            assert isinstance(raw, bytes)
            assert request.get_header("Content-type") == (
                "application/zip" if kind == "archive" else "application/json"
            )
            assert request.get_header("Content-length") == str(len(raw))
            assert not any(asset["name"] == name for asset in self.assets[1901])
            if lost and not self.unknown_after_apply:
                raise URLError(f"SIMULATED lost upload-{kind} response")
            asset = self.remote_asset(kind)
            self.asset_bytes[asset["id"]] = raw
            asset.update(size=len(raw), digest=f"sha256:{_sha256_bytes(raw)}")
            if lost:
                raise URLError(f"SIMULATED lost upload-{kind} response")
            wire = _canonical_json_bytes(asset)
        else:
            prefix = "https://api.github.com/"
            assert url.startswith(prefix), f"unexpected HTTP target: {url}"
            endpoint = url.removeprefix(prefix)
            wire = (
                self.response(endpoint)
                if request.get_method() == "GET"
                else self.api_write(request.get_method(), endpoint, request.data)
            )
        status = 201 if request.get_method() == "POST" else 200
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        listing_count = len(self.releases) if parsed.path == f"/repos/{REPOSITORY}/releases" else 0
        headers = HTTPMessage()
        headers["Content-Type"] = (
            "application/octet-stream" if "/releases/assets/" in parsed.path else "application/json"
        )
        if request.get_method() == "GET" and listing_count > page * 100:
            headers["Link"] = (
                f"<{parsed.scheme}://{parsed.netloc}{parsed.path}"
                f'?per_page=100&page={page + 1}>; rel="next"'
            )
        return addinfourl(io.BytesIO(wire), headers, url, status)


@pytest.fixture
def promotion_workflow_cli(
    promotion_attempt_inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _PromotionWorkflowCli:
    return _PromotionWorkflowCli(promotion_attempt_inputs, tmp_path, monkeypatch)


def test_promotion_cli_unstarted_to_published_complete(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    initial = cli.admit_attempt()
    assert initial["state"] == "unstarted"
    initial_attempt = cli.attempt_bytes
    assert initial_attempt is not None
    attempt = json.loads(initial_attempt)
    assert attempt["promotion_decision"] == cli.decision_reference
    assert attempt["remote_classification"] == cli.compact(cli.retained[("classification", 0)])
    assert cli.advance_and_retain(1)["state"] == "draft_empty"
    assert cli.advance_and_retain(2)["state"] == "draft_complete"

    prepublication_artifact = cli.artifacts[cli.retained[("observation", 2)]["artifact"]["id"]]
    fragment_raw = prepublication_artifact["members"]["pre-publication-verification-2.json"]
    fragment = json.loads(fragment_raw)
    _validate_review_schema(
        fragment, "remote-verification.schema.json#/$defs/pre_publication_verification"
    )
    assert fragment["phase"] == "PRE_PUBLICATION" and fragment["outcome"] == "PASS"
    manifest = cli.identity["candidate"]["preview_manifest"]
    for kind, expected in (
        ("archive", manifest["contents"]["archive"]),
        ("manifest", manifest["file"]),
    ):
        asset = fragment["release"][f"{kind}_asset"]
        assert {key: asset[key] for key in expected} == expected
        assert _sha256_bytes(cli.asset_bytes[int(asset["id"])]) == expected["sha256"]
    assert fragment["executable"] == manifest["contents"]["executable"]

    assert cli.advance_and_retain(3)["state"] == "published_complete"
    before_finish = deepcopy(cli.mutations)
    cli.requests.clear()
    handoff = cli.finish(3)
    assert cli.mutations == before_finish
    assert handoff["promotion_attempt"] == cli.compact(cli.retained[("attempt", 0)])
    assert handoff["remote_observation"] == cli.compact(cli.retained[("observation", 3)])
    assert handoff["remote_classification"] == cli.compact(cli.retained[("classification", 3)])
    assert handoff["promotion_decision"] == cli.decision_reference
    assert handoff["candidate_identity_record"] == cli.compact(
        cli.request["candidate_identity_reference"]
    )
    assert handoff["release_gate_catalog"] == cli.compact(
        cli.request["release_gate_catalog_reference"]
    )
    assert (
        handoff["pre_decision_stage_evidence"]
        == attempt["pre_decision_stage_evidence"]["record_reference"]
    )
    assert (
        handoff["pre_publication_stage_evidence"]
        == attempt["pre_publication_stage_evidence"]["record_reference"]
    )
    assert (
        handoff["permission_report"] == attempt["authorization"]["permission_readback"]["reference"]
    )
    assert (
        not {
            "remote_consumer_proof",
            "remote_verification",
            "post_publication_stage_evidence",
            "program_b_handoff",
        }
        & handoff.keys()
    )
    assert handoff["pre_publication_verification"]["sha256"] == _sha256_bytes(fragment_raw)
    assert Path(cli.locations["attempt_path"]).read_bytes() == initial_attempt
    assert cli.artifacts[cli.retained[("attempt", 0)]["artifact"]["id"]]["members"] == {
        "promotion-attempt.json": initial_attempt
    }
    assert [entry["operation"] for entry in cli.mutations] == [
        "create-tag",
        "create-draft",
        "upload-archive",
        "upload-manifest",
        "publish",
    ]
    first_write = next(index for index, entry in enumerate(cli.trace) if entry[0] == "mutation")
    for kind in ("admission", "classification", "attempt"):
        artifact_id = cli.retained[(kind, 0)]["artifact"]["id"]
        upload = cli.trace.index(("upload", artifact_id))
        readback = cli.trace.index(
            ("read", f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip")
        )
        assert upload < readback < first_write
    for artifact_id in ("501", "601", "701", "901", "902", "903"):
        assert ("read", f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip") in cli.trace[
            :first_write
        ]
        assert f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip" in cli.requests
    for step, following_action in ((1, "upload-archive"), (2, "publish")):
        following_write = cli.trace.index(("mutation", following_action))
        for kind in ("observation", "classification"):
            artifact_id = cli.retained[(kind, step)]["artifact"]["id"]
            uploaded = cli.trace.index(("upload", artifact_id))
            readback = cli.trace.index(
                ("read", f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip")
            )
            assert uploaded < readback < following_write
    for kind in ("observation", "classification"):
        artifact_id = cli.retained[(kind, 3)]["artifact"]["id"]
        assert f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip" in cli.requests

    tag_write = cli.trace.index(("mutation", "create-tag"))
    draft_write = cli.trace.index(("mutation", "create-draft"))
    assert ("read", f"repos/{REPOSITORY}/git/ref/tags/{TAG}") in cli.trace[
        tag_write + 1 : draft_write
    ]
    assert ("read", f"repos/{REPOSITORY}/git/tags/{cli.known_tag_oid}") in cli.trace[
        tag_write + 1 : draft_write
    ]
    tagger_epoch = int(
        artifact_contract.datetime.fromisoformat(
            attempt["recorded_at"].replace("Z", "+00:00")
        ).timestamp()
    )
    assert (
        cli.tag_input
        == (
            f"object {cli.source_commit}\ntype commit\ntag {TAG}\n"
            "tagger github-actions[bot] "
            f"<41898282+github-actions[bot]@users.noreply.github.com> {tagger_epoch} +0000\n"
            f"\nA1 opt-in preview\ncandidate-identity-sha256 {_sha256_bytes(cli.identity_bytes)}\n"
            f"promotion-decision-sha256 {cli.decision_reference['sha256']}\n"
        ).encode()
    )
    assert cli.tag is not None and cli.tag["object"]["sha"] == cli.known_tag_oid
    assert (
        cli.admission_bytes["current-job-permissions.json"]
        == Path(cli.locations["permission_path"]).read_bytes()
    )
    for name in (
        "remote-consumer-proof.json",
        "remote-verification.json",
        "post-publication-stage.json",
    ):
        assert not list(cli.runner.rglob(name)), "publisher must not invent post-publication proof"


@pytest.mark.parametrize(
    ("state", "expected_initial", "expected_writes", "steps"),
    [
        (
            "tag_only",
            "tag_only",
            ["create-draft", "upload-archive", "upload-manifest", "publish"],
            3,
        ),
        ("draft_empty", "draft_empty", ["upload-archive", "upload-manifest", "publish"], 2),
        ("draft_archive_only", "draft_partial", ["upload-manifest", "publish"], 2),
        ("draft_manifest_only", "draft_partial", ["upload-archive", "publish"], 2),
        ("draft_complete", "draft_complete", ["publish"], 1),
    ],
    ids=["tag-only", "draft-empty", "archive-only", "manifest-only", "draft-complete"],
)
def test_promotion_cli_resumes_only_missing_matching_work(
    promotion_workflow_cli: _PromotionWorkflowCli,
    state: str,
    expected_initial: str,
    expected_writes: list[str],
    steps: int,
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote(state)
    original_assets = deepcopy(cli.asset_bytes)
    classification = cli.admit_attempt()
    assert classification["state"] == expected_initial
    initial_attempt = cli.attempt_bytes
    for step in range(1, steps + 1):
        classification = cli.advance_and_retain(step)
    assert classification["state"] == "published_complete"
    cli.finish(steps)
    assert [entry["operation"] for entry in cli.mutations] == expected_writes
    assert cli.tag_input is None, "an existing matching annotated tag must not be recreated"
    assert all(cli.asset_bytes[key] == raw for key, raw in original_assets.items())
    assert Path(cli.locations["attempt_path"]).read_bytes() == initial_attempt


def test_promotion_cli_published_recovery_does_not_fabricate_prepublication_evidence(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("published_complete")
    assert cli.admit_attempt()["state"] == "published_complete"
    handoff = cli.finish(0)
    assert handoff.get("pre_publication_verification") is None
    assert handoff["remote_classification"] == cli.compact(cli.retained[("classification", 0)])
    assert not list(cli.runner.rglob("pre-publication-verification-*.json"))
    assert cli.mutations == []


@pytest.mark.parametrize(
    "boundary", ["admission-upload", "classification-upload", "attempt-upload", "attempt-readback"]
)
def test_promotion_cli_requires_retained_admission_chain_before_any_mutation(
    promotion_workflow_cli: _PromotionWorkflowCli, boundary: str
) -> None:
    cli = promotion_workflow_cli
    cli.stage_and_observe()
    if boundary == "admission-upload":
        assert cli.invoke("classify-promote", step=0) == 1
    else:
        cli.retain("admission")
        assert cli.invoke("classify-promote", step=0) == 0
        if boundary == "classification-upload":
            assert cli.invoke("seal-promote-attempt") == 1
        else:
            cli.retain("classification")
            assert cli.invoke("seal-promote-attempt") == 0
            if boundary == "attempt-readback":
                reference = cli.retain("attempt")
                cli.artifacts[reference["artifact"]["id"]]["wire"] += b"SIMULATED damaged readback"
            assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == []


@pytest.mark.parametrize(
    "violation",
    [
        "decline",
        "decision-raw",
        "candidate-raw",
        "stage-raw",
        "catalog-raw",
        "nested-proof-raw",
        "payload-raw",
        "artifact-expired",
        "expiry-time",
        "artifact-run",
        "stage-outcome",
        "stage-gate-omitted",
    ],
)
def test_promotion_cli_revalidates_retained_authority_before_staging(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    if violation == "decline":
        cli.rewrite_decision(lambda decision: decision.update(decision="DECLINE"))
    elif violation in ("stage-outcome", "stage-gate-omitted"):
        stage = json.loads(cli.artifacts["902"]["members"]["pre-decision-stage.json"])
        if violation == "stage-outcome":
            stage["outcome"] = "FAIL"
        else:
            stage["gate_evidence"].pop()
        raw = _canonical_json_bytes(stage)
        cli.replace_retained("902", "pre-decision-stage.json", raw)
        cli.rewrite_decision(
            lambda decision: decision["evidence"]["pre_decision_stage_evidence"][
                "record_reference"
            ].update(sha256=_sha256_bytes(raw))
        )
    elif violation.endswith("-raw"):
        artifact_id, path = {
            "decision-raw": ("903", "promotion-decision.json"),
            "candidate-raw": ("601", "candidate-identity.json"),
            "stage-raw": ("902", "pre-decision-stage.json"),
            "catalog-raw": ("701", "release-gate-catalog.json"),
            "nested-proof-raw": ("901", "consumer-proof.json"),
            "payload-raw": (
                "501",
                cli.identity["candidate"]["preview_manifest"]["contents"]["archive"]["name"],
            ),
        }[violation]
        cli.replace_retained(artifact_id, path, cli.artifacts[artifact_id]["members"][path] + b" ")
    elif violation == "artifact-expired":
        cli.artifacts["903"]["metadata"]["expired"] = True
    elif violation == "expiry-time":
        cli.artifacts["501"]["metadata"]["expires_at"] = "2028-12-31T23:59:59Z"
    else:
        cli.artifacts["903"]["metadata"]["workflow_run"]["id"] = 999

    assert cli.invoke("admit-promote") == 1
    assert not list(cli.runner.rglob("promotion-attempt.json"))
    assert not list(cli.runner.rglob("pre-publication-stage.json"))
    assert cli.mutations == []


@pytest.mark.parametrize(
    "violation",
    [
        "source-run",
        "fork",
        "live-main",
        "workflow-sha",
        "run-attempt",
        "completed-run",
        "wrong-job",
        "ambiguous-job",
        "actor",
        "rerun-actor",
        "event-sender",
        "probe-job",
        "missing-token",
    ],
)
def test_promotion_cli_requires_independent_current_promote_context(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    run = cli.runs["801"]
    if violation == "source-run":
        run["head_sha"] = "a" * 40
    elif violation == "fork":
        run["head_repository"] = {"id": 4002, "full_name": "other/fork"}
    elif violation == "live-main":
        cli.main_target = "a" * 40
    elif violation == "workflow-sha":
        cli.environment["GITHUB_WORKFLOW_SHA"] = "a" * 40
    elif violation == "run-attempt":
        cli.environment["GITHUB_RUN_ATTEMPT"] = "3"
    elif violation == "completed-run":
        run.update(status="completed", conclusion="success")
    elif violation == "wrong-job":
        cli.jobs["801"]["jobs"][0]["name"] = "Probe current job permissions"
    elif violation == "ambiguous-job":
        cli.jobs["801"]["jobs"].append(deepcopy(cli.jobs["801"]["jobs"][0]))
        cli.jobs["801"]["total_count"] = 2
    elif violation == "actor":
        cli.environment["GITHUB_ACTOR"] = "thebtf"
    elif violation == "rerun-actor":
        run["triggering_actor"] = {"id": 7106373, "login": "thebtf"}
    elif violation == "event-sender":
        cli.event["sender"]["id"] = 7106373
    elif violation == "probe-job":
        cli.environment["GITHUB_JOB"] = "probe_permissions"
    else:
        cli.environment.pop("GITHUB_TOKEN")
    assert cli.invoke("admit-promote") == 1
    assert cli.mutations == []


@pytest.mark.parametrize(
    "violation",
    [
        "unavailable",
        "unqualified",
        "contents-read",
        "probe",
        "other-run",
        "other-attempt",
        "other-job",
        "other-workflow",
        "other-actor",
        "missing-native-log",
        "stale-report",
    ],
)
def test_promotion_cli_refuses_unqualified_or_substituted_same_job_report(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    assert cli.invoke("admit-promote") == 0
    report = cli.permission_report
    if violation == "unavailable":
        report.update(status="UNAVAILABLE", availability="UNAVAILABLE")
    elif violation == "unqualified":
        report["status"] = "UNQUALIFIED"
    elif violation == "contents-read":
        report["grants"]["contents"] = "read"
    elif violation == "probe":
        report["binding"].update(
            job_key="probe_permissions", job_name="Probe current job permissions"
        )
    elif violation.startswith("other-"):
        key, value = {
            "other-run": ("run_id", 999),
            "other-attempt": ("run_attempt", 1),
            "other-job": ("job_id", 999),
            "other-workflow": ("workflow_sha", "a" * 40),
            "other-actor": ("GITHUB_TRIGGERING_ACTOR", "thebtf"),
        }[violation]
        report["actor_provenance" if violation == "other-actor" else "binding"][key] = value
    elif violation == "missing-native-log":
        report["log_sha256"] = None
    cli.write_permission_report()
    if violation == "stale-report":
        report["started_at"] = report["finished_at"] = "2029-01-01T00:00:00Z"
        Path(cli.locations["permission_path"]).write_bytes(_canonical_json_bytes(report))
    assert cli.invoke("observe-promote") == 1
    assert not list(cli.runner.rglob("promotion-attempt.json"))
    assert cli.mutations == []


def test_promotion_cli_job_id_is_not_sourced_from_the_permission_report(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    job = cli.jobs["801"]["jobs"][0]
    job.update(id=902, url=f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/902")
    assert cli.invoke("admit-promote") == 0
    cli.write_permission_report()
    assert cli.invoke("observe-promote") == 1
    assert any("/actions/runs/801/attempts/2/jobs" in endpoint for endpoint in cli.requests)
    assert cli.mutations == []


@pytest.mark.parametrize(
    "violation",
    ["main-moved", "actor-changed", "job-changed", "decision-tamper", "report-tamper", "expired"],
)
def test_promotion_cli_rechecks_authority_immediately_before_mutation(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    cli.admit_attempt()
    if violation == "main-moved":
        cli.main_target = "a" * 40
    elif violation == "actor-changed":
        cli.runs["801"]["triggering_actor"] = {"id": 999, "login": "other"}
    elif violation == "job-changed":
        job = cli.jobs["801"]["jobs"][0]
        job.update(id=902, url=f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/902")
    elif violation == "decision-tamper":
        cli.replace_retained("903", "promotion-decision.json", b"{}\n")
    else:
        artifact_id = cli.retained[("admission", 0)]["artifact"]["id"]
        if violation == "expired":
            cli.artifacts[artifact_id]["metadata"]["expired"] = True
        else:
            cli.replace_retained(artifact_id, "current-job-permissions.json", b"{}\n")
    assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == []


def test_promotion_cli_discovers_matching_draft_on_later_release_page(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("draft_empty")
    cli.releases[:0] = [
        {**cli.release_metadata(4000 + index), "tag_name": f"unrelated-{index}"}
        for index in range(100)
    ]
    assert cli.admit_attempt()["state"] == "draft_empty"
    assert any("releases?" in endpoint and "page=2" in endpoint for endpoint in cli.requests)
    assert cli.advance_and_retain(1)["state"] == "draft_complete"
    assert cli.advance_and_retain(2)["state"] == "published_complete"
    assert "create-draft" not in [entry["operation"] for entry in cli.mutations]


@pytest.mark.parametrize(
    "violation",
    [
        "duplicate-draft",
        "lightweight-tag",
        "wrong-tag-target",
        "duplicate-name",
        "duplicate-id",
        "extra",
        "starter",
        "renamed",
        "tampered-bytes",
    ],
)
def test_promotion_cli_observes_collisions_without_repairing_remote_objects(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("draft_complete")
    if violation == "duplicate-draft":
        cli.releases.append(cli.release_metadata(1902))
    elif violation == "lightweight-tag":
        cli.set_tag(cli.source_commit, kind="commit")
    elif violation == "wrong-tag-target":
        cli.set_tag("d" * 40, target="a" * 40)
    else:
        archive = cli.assets[1901][0]
        if violation == "duplicate-name":
            duplicate = {**archive, "id": 3901}
            cli.assets[1901].append(duplicate)
            cli.asset_bytes[3901] = cli.asset_bytes[archive["id"]]
        elif violation == "duplicate-id":
            cli.assets[1901][1]["id"] = archive["id"]
        elif violation == "extra":
            cli.assets[1901].append({**archive, "id": 3901, "name": "unexpected.zip"})
            cli.asset_bytes[3901] = b"SIMULATED unexpected asset"
        elif violation == "starter":
            archive["state"] = "starter"
        elif violation == "renamed":
            archive["name"] = "unapproved-name.zip"
        else:
            raw = cli.asset_bytes[archive["id"]]
            cli.asset_bytes[archive["id"]] = bytes([raw[0] ^ 1]) + raw[1:]
    cli.stage_and_observe()
    cli.retain("admission")
    classification = cli.classify_and_retain(0)
    assert classification["state"] == "collision"
    assert classification["admission"] == "REFUSED" and classification["allowed_action"] == "REFUSE"
    assert cli.invoke("seal-promote-attempt") == 1
    assert cli.mutations == []


@pytest.mark.parametrize("race", ["same-oid", "other-oid-same-commit"])
def test_promotion_cli_create_only_tag_lease_refuses_racing_or_up_to_date_result(
    promotion_workflow_cli: _PromotionWorkflowCli, race: str
) -> None:
    cli = promotion_workflow_cli
    cli.admit_attempt()
    cli.push_race = race
    assert cli.invoke("advance-promote", step=1) == 1
    assert [entry["operation"] for entry in cli.mutations] == ["create-tag"]
    assert cli.releases == []
    assert cli.tag is not None


@pytest.mark.parametrize(
    ("operation", "state"),
    [
        ("create-tag", "unstarted"),
        ("create-draft", "tag_only"),
        ("upload-archive", "draft_manifest_only"),
        ("upload-manifest", "draft_archive_only"),
        ("publish", "draft_complete"),
    ],
)
@pytest.mark.parametrize("applied", [False, True], ids=["not-applied", "applied-response-lost"])
def test_promotion_cli_unknown_write_is_reconciled_once_and_never_replayed(
    promotion_workflow_cli: _PromotionWorkflowCli, operation: str, state: str, applied: bool
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote(state)
    cli.admit_attempt()
    cli.unknown_operation = operation
    cli.unknown_after_apply = applied
    result = cli.invoke("advance-promote", step=1)
    assert result == (0 if applied else 1)
    assert sum(entry["operation"] == operation for entry in cli.mutations) == 1
    if applied:
        cli.retain("observation", 1)
        classification = cli.classify_and_retain(1)
        assert (
            classification["state"]
            == {
                "create-tag": "draft_empty",
                "create-draft": "draft_empty",
                "upload-archive": "draft_complete",
                "upload-manifest": "draft_complete",
                "publish": "published_complete",
            }[operation]
        )
        if operation == "create-tag":
            assert cli.tag is not None and cli.tag["object"]["sha"] == cli.known_tag_oid
    else:
        assert [entry["operation"] for entry in cli.mutations] == [operation]
    writes = deepcopy(cli.mutations)
    assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == writes


def test_promotion_cli_unknown_tag_write_with_unreadable_reconciliation_stops(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    cli.admit_attempt()
    cli.unknown_operation = "create-tag"
    cli.unknown_after_apply = True
    cli.unreadable_reconciliation = True
    assert cli.invoke("advance-promote", step=1) == 1
    assert [entry["operation"] for entry in cli.mutations] == ["create-tag"]
    assert cli.releases == []
    assert cli.invoke("finish-promote", step=0) == 1


@pytest.mark.parametrize(
    "violation", ["missing-fragment", "false-fragment", "asset-id-changed", "bytes-changed"]
)
def test_promotion_cli_publish_requires_retained_prepublication_fragment_and_fresh_same_assets(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("draft_complete")
    cli.admit_attempt()
    reference = cli.retained[("admission", 0)]
    artifact_id = reference["artifact"]["id"]
    members = cli.artifacts[artifact_id]["members"]
    fragment_name = "pre-publication-verification-0.json"
    assert fragment_name in members
    if violation in ("missing-fragment", "false-fragment"):
        if violation == "missing-fragment":
            del members[fragment_name]
        else:
            fragment = json.loads(members[fragment_name])
            fragment["archive_matches_candidate"] = False
            members[fragment_name] = _canonical_json_bytes(fragment)
        artifact = cli.artifacts[artifact_id]
        artifact["wire"] = _evidence_bundle_zip(list(members.items()))
        digest = _sha256_bytes(artifact["wire"])
        artifact["metadata"]["digest"] = f"sha256:{digest}"
        cli.environment["A1_PROMOTE_ADMISSION_ARTIFACT_DIGEST"] = digest
    else:
        asset = cli.assets[1901][0]
        if violation == "asset-id-changed":
            cli.asset_bytes[3901] = cli.asset_bytes[asset["id"]]
            asset["id"] = 3901
            asset["url"] = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/3901"
        else:
            raw = cli.asset_bytes[asset["id"]]
            cli.asset_bytes[asset["id"]] = raw[:-1] + bytes([raw[-1] ^ 1])
    assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == []
    assert cli.releases[0]["draft"] is True


def test_promotion_cli_uploads_exact_binary_bytes_and_reads_back_before_next_write(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("draft_empty")
    cli.admit_attempt()
    assert cli.advance_and_retain(1)["state"] == "draft_complete"
    for mutation in cli.mutations:
        kind = mutation["operation"].removeprefix("upload-")
        manifest = cli.identity["candidate"]["preview_manifest"]
        descriptor = manifest["contents"]["archive"] if kind == "archive" else manifest["file"]
        expected = cli.artifacts["501"]["members"][descriptor["name"]]
        assert mutation["body"] == expected
        assert _sha256_bytes(mutation["body"]) == descriptor["sha256"]
        if kind == "archive":
            assert b"\0" in expected
    first_upload = cli.trace.index(("mutation", "upload-archive"))
    second_upload = cli.trace.index(("mutation", "upload-manifest"))
    assert any(entry[0] == "download" for entry in cli.trace[first_upload + 1 : second_upload])
    before_publish = len(cli.trace)
    assert cli.advance_and_retain(2)["state"] == "published_complete"
    publication = cli.trace.index(("mutation", "publish"))
    downloaded = {
        entry[1] for entry in cli.trace[before_publish:publication] if entry[0] == "download"
    }
    assert downloaded == set(cli.asset_bytes)


@pytest.mark.parametrize(
    "boundary", ["skip-action-block", "observation-upload", "classification-upload"]
)
def test_promotion_cli_requires_fresh_retained_observation_and_classification_between_actions(
    promotion_workflow_cli: _PromotionWorkflowCli, boundary: str
) -> None:
    cli = promotion_workflow_cli
    cli.admit_attempt()
    if boundary == "skip-action-block":
        assert cli.invoke("advance-promote", step=2) == 1
    else:
        assert cli.invoke("advance-promote", step=1) == 0
        if boundary == "classification-upload":
            cli.retain("observation", 1)
            assert cli.invoke("classify-promote", step=1) == 0
        mutations = deepcopy(cli.mutations)
        assert cli.invoke("advance-promote", step=2) == 1
        assert cli.mutations == mutations
    assert not any(entry["operation"].startswith("upload-") for entry in cli.mutations)


@pytest.mark.parametrize(
    "violation",
    [
        "unknown-request-field",
        "actor",
        "source-sha",
        "grants",
        "release-id",
        "path",
        "url",
        "token",
        "repository",
        "wrong-tag",
        "mixed-build",
        "mixed-evidence",
        "duplicate-json-key",
        "escaped-overflow",
    ],
)
def test_promotion_cli_public_request_is_only_a_closed_retained_decision_reference(
    promotion_workflow_cli: _PromotionWorkflowCli, violation: str
) -> None:
    cli = promotion_workflow_cli
    reference = deepcopy(cli.decision_reference)
    if violation in (
        "unknown-request-field",
        "actor",
        "source-sha",
        "grants",
        "release-id",
        "url",
        "token",
    ):
        key, value = {
            "unknown-request-field": ("approved", True),
            "actor": ("actor", {"id": "17", "login": "release-bot"}),
            "source-sha": ("source_commit", cli.source_commit),
            "grants": ("permissions", {"contents": "write"}),
            "release-id": ("release_id", "1901"),
            "url": ("url", "https://example.invalid/decision.json"),
            "token": ("GITHUB_TOKEN", "SIMULATED caller token"),
        }[violation]
        reference[key] = value
    elif violation == "path":
        reference["path"] = "../promotion-decision.json"
    elif violation == "repository":
        reference["repository"] = "other/repository"
    elif violation == "wrong-tag":
        cli.inputs["preview_tag"] = "stateless-preview-v9.9.9-preview.9"
    elif violation == "mixed-build":
        cli.inputs["preview_version"] = VERSION
    elif violation == "mixed-evidence":
        cli.inputs["evidence_request"] = "{}"
    cli.inputs["promote_request"] = json.dumps(reference)
    if violation == "duplicate-json-key":
        cli.inputs["promote_request"] = (
            cli.inputs["promote_request"][:-1] + ',"path":"promotion-decision.json"}'
        )
    elif violation == "escaped-overflow":
        cli.inputs["promote_request"] += "\t" * 33_000
        assert json.loads(cli.inputs["promote_request"]) == reference
        assert len(cli.inputs["promote_request"]) < 65_535
        assert len(json.dumps(cli.inputs, ensure_ascii=True, separators=(",", ":"))) > 65_535
    assert cli.invoke("admit-promote") == 1
    assert cli.mutations == []
    if violation == "escaped-overflow":
        assert cli.requests == []


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("admit-promote", ["--output", "caller.json"]),
        ("observe-promote", ["--step", "0"]),
        ("seal-promote-attempt", ["--step", "0"]),
        ("classify-promote", ["--step", "4"]),
        ("advance-promote", ["--step", "0"]),
        ("advance-promote", ["--step", "4"]),
        ("finish-promote", ["--step", "-1"]),
    ],
)
def test_promotion_cli_rejects_caller_paths_and_nonfixed_action_steps(
    promotion_workflow_cli: _PromotionWorkflowCli, command: str, arguments: list[str]
) -> None:
    cli = promotion_workflow_cli
    cli.requests.clear()
    with pytest.raises(SystemExit) as error:
        artifact_contract.main([command, *arguments])
    assert error.value.code == 2
    assert cli.requests == [] and cli.mutations == []
    assert not (cli.root / "caller.json").exists()


def test_promotion_cli_ignores_foreign_gh_token_without_exposing_workflow_token(
    promotion_workflow_cli: _PromotionWorkflowCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = promotion_workflow_cli
    cli.environment["GH_TOKEN"] = "SIMULATED foreign higher-precedence token"
    assert cli.invoke("admit-promote") == 0
    output = capsys.readouterr()
    assert "test-only-token" not in output.out + output.err
    assert "SIMULATED foreign higher-precedence token" not in output.out + output.err
    assert cli.environment["GH_TOKEN"] == "SIMULATED foreign higher-precedence token"
    assert cli.mutations == []


@pytest.mark.parametrize(
    "command",
    [
        "admit-build",
        "prepare-payload",
        "seal-build-records",
        "admit-evidence",
        "seal-evidence-stage",
        "seal-evidence-decision",
    ],
)
def test_promotion_cli_cannot_enter_build_or_evidence_effect_routes(
    promotion_workflow_cli: _PromotionWorkflowCli, command: str
) -> None:
    cli = promotion_workflow_cli
    assert cli.invoke(command) == 1
    assert cli.mutations == []
    assert not (cli.root / "artifacts" / "stateless-preview").exists()


@pytest.mark.parametrize("state", ["unstarted", "draft_empty"])
def test_promotion_cli_changed_remote_state_does_not_select_a_different_action(
    promotion_workflow_cli: _PromotionWorkflowCli, state: str
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote(state)
    cli.admit_attempt()
    if state == "unstarted":
        cli.set_tag("d" * 40)
    else:
        cli.remote_asset("manifest")
    assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == []


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://example.invalid/repos/thebtf/netcoredbg-mcp/releases/1901/assets{?name,label}",
        "http://uploads.github.com/repos/thebtf/netcoredbg-mcp/releases/1901/assets{?name,label}",
        "https://uploads.github.com/repos/other/repository/releases/1901/assets{?name,label}",
        "https://uploads.github.com/repos/thebtf/netcoredbg-mcp/releases/1902/assets{?name,label}",
    ],
    ids=["foreign-host", "http", "foreign-repository", "different-release-id"],
)
def test_promotion_cli_never_sends_payload_or_token_to_an_unbound_upload_target(
    promotion_workflow_cli: _PromotionWorkflowCli, upload_url: str
) -> None:
    cli = promotion_workflow_cli
    cli.seed_remote("draft_empty")
    cli.admit_attempt()
    cli.releases[0]["upload_url"] = upload_url
    assert cli.invoke("advance-promote", step=1) == 1
    assert cli.mutations == []


def test_promotion_cli_strips_child_auth_overrides_and_logging(
    promotion_workflow_cli: _PromotionWorkflowCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = promotion_workflow_cli
    overrides = {
        "GH_TOKEN": "SIMULATED foreign token",
        "GH_ENTERPRISE_TOKEN": "SIMULATED foreign enterprise token",
        "GH_DEBUG": "api",
        "GH_HOST": "example.invalid",
        "GH_CONFIG_DIR": "SIMULATED foreign credential cache",
        "GITHUB_ENTERPRISE_TOKEN": "SIMULATED other token",
        "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: SIMULATED foreign header",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    children = []

    def process(command: Any, *args: Any, **kwargs: Any) -> Any:
        if command[0] == "gh" or command[:2] in (
            ["git", "push"],
            ["git", "config"],
            ["git", "mktag"],
        ):
            children.append((command, dict(kwargs["env"])))
        return cli.process(command, *args, **kwargs)

    monkeypatch.setattr(artifact_contract.subprocess, "run", process)
    cli.admit_attempt()
    assert cli.advance_and_retain(1)["state"] == "draft_empty"
    assert children
    for command, child in children:
        assert child["GITHUB_TOKEN"] == "test-only-token"
        assert not set(child) & {
            "GH_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GH_DEBUG",
            "GH_HOST",
            "GITHUB_ENTERPRISE_TOKEN",
            "GIT_TRACE",
            "GIT_TRACE_CURL",
        }
        assert child["GH_CONFIG_DIR"] != overrides["GH_CONFIG_DIR"]
        assert all(
            value not in " ".join(command) for value in ("test-only-token", overrides["GH_TOKEN"])
        )
    assert all(cli.environment[key] == value for key, value in overrides.items())
    captured = capsys.readouterr()
    assert "test-only-token" not in captured.out + captured.err
    assert overrides["GH_TOKEN"] not in captured.out + captured.err


def test_promotion_upload_redirect_handler_never_forwards_request_credentials() -> None:
    request = artifact_contract.Request(
        "https://uploads.github.com/repos/thebtf/netcoredbg-mcp/releases/1901/assets?name=approved.zip",
        data=b"SIMULATED approved bytes",
        method="POST",
        headers={"Authorization": "Bearer SIMULATED workflow token"},
    )
    for status in (301, 302, 303, 307, 308):
        assert (
            artifact_contract._PromotionNoRedirect().redirect_request(
                request,
                None,
                status,
                "SIMULATED redirect",
                HTTPMessage(),
                "https://example.invalid/steal",
            )
            is None
        )


def test_promotion_cli_diagnostic_observation_cannot_authorize_next_action(
    promotion_workflow_cli: _PromotionWorkflowCli,
) -> None:
    cli = promotion_workflow_cli
    cli.admit_attempt()
    cli.set_tag("d" * 40)
    assert cli.invoke("advance-promote", step=1) == 1
    cli.retain("observation", 1)
    assert cli.invoke("classify-promote", step=1) == 1
    assert cli.mutations == []


@pytest.mark.parametrize("newline", [b"\r\n", b"\n"])
def test_promotion_rest_transport_preserves_binary_header_like_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newline: bytes,
) -> None:
    payload = b"SIMULATED\0archive\r\n\r\nHTTP/2.0 403 not a response\n\nend"
    environment = {
        "GITHUB_TOKEN": "test-only-token",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": "801",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(tmp_path),
    }

    def process(command: Any, *args: Any, **kwargs: Any) -> Any:
        assert command[:2] == ["gh", "api"]
        assert kwargs["env"]["GITHUB_TOKEN"] == "test-only-token"
        header = newline.join(
            (b"HTTP/2.0 200 OK", b"Content-Type: application/octet-stream", b"", b"")
        )
        return subprocess.CompletedProcess(command, 0, header + payload, b"")

    monkeypatch.setattr(artifact_contract.subprocess, "run", process)
    status, raw = artifact_contract._promotion_gh(
        tmp_path,
        environment,
        [
            "api",
            f"repos/{REPOSITORY}/releases/assets/2901",
            "-H",
            "Accept: application/octet-stream",
        ],
        "SIMULATED binary asset",
    )
    assert status == 200 and raw == payload
