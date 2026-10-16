"""Behavioral RED contracts for the A1 opt-in preview artifact runway."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

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

_spec = importlib.util.spec_from_file_location(
    "stateless_preview_artifact",
    ARTIFACT_CONTRACT_PATH,
)
assert _spec is not None and _spec.loader is not None
artifact_contract = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = artifact_contract
_spec.loader.exec_module(artifact_contract)

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
    return preview_validator.ArtifactFileReference(
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


def test_artifact_downloader_hashes_wire_bytes_before_exclusive_retention(
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
    downloader = preview_validator.ArtifactDownloader(root)
    archive_path = root / "artifacts" / f"{reference.artifact_id}-{reference.artifact_sha256}.zip"
    events: list[str] = []
    original_hash = preview_validator._sha256_bytes
    original_mkdir = Path.mkdir
    original_open = Path.open

    def track_hash(value: bytes) -> str:
        if value == wire_bytes:
            events.append("wire-hash")
        return original_hash(value)

    def track_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == archive_path.parent:
            events.append("archive-directory")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def track_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == archive_path and mode == "xb":
            events.append("exclusive-write")
        return original_open(path, mode, *args, **kwargs)

    def fetch_wire(arguments: Any, name: str) -> bytes:
        assert name == "retained artifact"
        assert arguments == [
            "api",
            f"repos/{reference.repository}/actions/artifacts/{reference.artifact_id}/zip",
        ]
        assert not archive_path.parent.exists()
        return wire_bytes

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", fetch_wire)
    monkeypatch.setattr(preview_validator, "_sha256_bytes", track_hash)
    monkeypatch.setattr(Path, "mkdir", track_mkdir)
    monkeypatch.setattr(Path, "open", track_open)

    retained_path = downloader.download_file(reference)

    assert events == ["wire-hash", "archive-directory", "exclusive-write"]
    assert archive_path.read_bytes() == wire_bytes
    assert retained_path.read_bytes() == member


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
    downloader = preview_validator.ArtifactDownloader(root)

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)

    with pytest.raises(ValueError, match="destination already exists"):
        downloader.download_file(reference)

    assert archive_path.read_bytes() == b"planted archive"
    assert downloader._archives == {}


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
    downloader = preview_validator.ArtifactDownloader(root)
    original_open = Path.open
    race_injected = False

    def race_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal race_injected
        if path == archive_path and mode == "xb" and not race_injected:
            race_injected = True
            with original_open(path, "wb") as planted:
                planted.write(b"raced archive")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)
    monkeypatch.setattr(Path, "open", race_open)

    with pytest.raises(ValueError, match="destination already exists"):
        downloader.download_file(reference)

    assert race_injected
    assert archive_path.read_bytes() == b"raced archive"
    assert downloader._archives == {}


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
    downloader = preview_validator.ArtifactDownloader(root)

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)

    with pytest.raises(ValueError, match="wire bytes"):
        downloader.download_file(reference)

    assert not (root / "artifacts").exists()
    assert downloader._archives == {}


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
    downloader = preview_validator.ArtifactDownloader(root)
    requests: list[Any] = []

    def fetch_wire(arguments: Any, _: str) -> bytes:
        requests.append(arguments)
        return wire_bytes

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", fetch_wire)

    downloader.download_file(archive_reference)
    cached_path = root / "artifacts" / f"{archive_reference.artifact_id}-{wire_sha256}.zip"
    cached_path.write_bytes(
        _validator_wire_zip(
            tmp_path,
            {"payload.zip": b"substituted archive", "payload.manifest.json": manifest_member},
        )
    )

    with pytest.raises(ValueError, match="cached retained artifact"):
        downloader.download_file(manifest_reference)

    assert len(requests) == 1


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
    downloader = preview_validator.ArtifactDownloader(root)

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)

    archive_path, manifest_path = downloader.download_payload(
        archive_reference,
        manifest_reference,
        candidate_payload_digest,
    )

    assert wire_sha256 != candidate_payload_digest
    assert archive_path.read_bytes() == archive_member
    assert manifest_path.read_bytes() == manifest_member


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
    downloader = preview_validator.ArtifactDownloader(root)

    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)

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
