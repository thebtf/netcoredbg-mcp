"""Behavioral RED contracts for the A1 opt-in preview artifact runway."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_CONTRACT_PATH = PROJECT_ROOT / "scripts" / "stateless_preview_artifact.py"
RELEASE_GATE_CATALOG_CONTRACT = (
    PROJECT_ROOT / "specs" / "010-a1-preview-artifact" / "contracts" / "release-gate-catalog.md"
)
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "stateless-preview.yml"
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
produce_post_merge_exact_head_receipt = artifact_contract.produce_post_merge_exact_head_receipt
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

    for relative_path in _authority_paths():
        source = PROJECT_ROOT / relative_path
        target = authority_root / relative_path
        assert source.is_file(), f"missing authority source: {relative_path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    _git(authority_root, "add", ".")
    _git(authority_root, "commit", "-m", "snapshot release authorities")
    source_commit = _git(authority_root, "rev-parse", "HEAD")
    _git(authority_root, "remote", "add", "origin", str(origin))
    _git(authority_root, "push", "--set-upstream", "origin", "main")

    snapshots = [
        {
            "path": relative_path,
            "sha256": _sha256_path(authority_root / relative_path),
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
    post_merge_reference = {
        "repository": REPOSITORY,
        "run_id": "101",
        "artifact_id": "501",
        "path": "receipts/post-merge-exact-head.json",
        "sha256": _sha256_bytes(b"post-merge receipt"),
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
                    "post_merge_exact_head_receipt": {
                        "record_reference": post_merge_reference,
                        "stage": "post-merge",
                        "record_type": "sonarqube-exact-head",
                        "scanned_commit": source_commit,
                        "tag_target": source_commit,
                        "outcome": "PASS",
                    },
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
        "candidate-exact-head-sonar": "CANDIDATE_EXACT_HEAD_SONAR",
        "post-merge-exact-head-sonar": "POST_MERGE_MAIN_TARGET_SONAR",
        "remote-downloaded-consumer-proof": "REMOTE_CONSUMER_PROOF",
    }
    rows = [
        ("pre-decision", "retained-downloaded-consumer-proof", "artifact-consumer-proof"),
        ("pre-decision", "s2-s3-seven-lens-evidence", "s2-s3-seven-lens-aggregate"),
        ("pre-decision", "independent-review", "independent-pr-review"),
        ("pre-decision", "candidate-exact-head-sonar", "sonarqube-exact-head"),
        ("pre-publication", "post-merge-exact-head-sonar", "sonarqube-exact-head"),
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
        "failed_post_merge_receipt",
        "unrelated_post_merge_receipt",
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
    elif violation == "failed_post_merge_receipt":
        candidate["source"]["post_merge_exact_head_receipt"]["outcome"] = "FAIL"
    else:
        candidate["source"]["post_merge_exact_head_receipt"]["tag_target"] = "c" * 40

    with pytest.raises(ValueError):
        assemble_candidate_identity(_freeze(candidate_input))


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


def test_assemble_candidate_identity_refuses_a_caller_supplied_gate_catalog(
    tmp_path: Path,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    candidate_input, _, _, _ = _create_candidate_input(tmp_path, source_commit)
    candidate_input["release_gate_catalog"] = {"gate_descriptors": []}

    with pytest.raises(ValueError):
        assemble_candidate_identity(_freeze(candidate_input))


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
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "role": "post-merge",
                "outcome": outcome,
                "project_key": "thebtf_netcoredbg_mcp",
                "analysis_xml_project_key": "thebtf_netcoredbg_mcp",
                "captured_head": captured_head or source_commit,
                "post_scan_head": source_commit,
                "quality_gate": {"status": "OK"},
                "worktree": {"detached": True, "linked": True},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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
        "post_merge_receipt",
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


def test_post_merge_receipt_producer_binds_the_trusted_scan_to_main(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    raw_receipt = _write_post_merge_scan_receipt(authority_root, source_commit)

    produced = produce_post_merge_exact_head_receipt(
        authority_root, _build_environment(source_commit)
    )

    assert {
        key: produced[key]
        for key in (
            "receipt_schema_version",
            "record_type",
            "repository",
            "source_ref",
            "stage",
            "scanned_commit",
            "tag_target",
            "outcome",
            "source_runner",
        )
    } == {
        "receipt_schema_version": "1.0",
        "record_type": "sonarqube-exact-head",
        "repository": REPOSITORY,
        "source_ref": SOURCE_REF,
        "stage": "post-merge",
        "scanned_commit": source_commit,
        "tag_target": source_commit,
        "outcome": "PASS",
        "source_runner": {
            "script": "scripts/run_sonarqube_exact_head.py",
            "role": "post-merge",
            "receipt_schema_version": 2,
            "project_key": "thebtf_netcoredbg_mcp",
            "receipt_sha256": _sha256_path(raw_receipt),
        },
    }
    assert re.fullmatch(r"[^\r\n]+Z", produced["recorded_at"])


@pytest.mark.parametrize(
    ("outcome", "captured_head"),
    [("FAIL", None), ("PASS", "c" * 40)],
)
def test_post_merge_receipt_producer_refuses_untrusted_scan_result(
    tmp_path: Path, outcome: str, captured_head: str | None
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    _write_post_merge_scan_receipt(
        authority_root, source_commit, outcome=outcome, captured_head=captured_head
    )

    with pytest.raises(ValueError):
        produce_post_merge_exact_head_receipt(authority_root, _build_environment(source_commit))


def test_post_merge_receipt_producer_refuses_a_non_post_merge_runner(tmp_path: Path) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    raw_receipt = _write_post_merge_scan_receipt(authority_root, source_commit)
    contents = json.loads(raw_receipt.read_text(encoding="utf-8"))
    contents["role"] = "candidate"
    raw_receipt.write_text(json.dumps(contents, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        produce_post_merge_exact_head_receipt(authority_root, _build_environment(source_commit))


def test_seal_build_records_discovers_uploaded_receipt_and_payload_metadata(
    tmp_path: Path,
) -> None:
    authority_root, source_commit, _ = _create_authority_repository(tmp_path)
    _create_publish_output(authority_root)
    environment = _build_environment(source_commit)
    admission = admit_build(authority_root, environment)
    prepare_preview_payload(authority_root, environment)
    _write_post_merge_scan_receipt(authority_root, source_commit)
    produce_post_merge_exact_head_receipt(authority_root, environment)
    environment.update(
        {
            "A1_PAYLOAD_ARTIFACT_ID": "501",
            "A1_POST_MERGE_RECEIPT_ARTIFACT_ID": "502",
            "GITHUB_TOKEN": "test-token",
        }
    )

    def artifact_reader(_repository: str, artifact_id: str, _token: str) -> Mapping[str, Any]:
        return {
            "id": int(artifact_id),
            "name": admission["artifacts"][
                "payload" if artifact_id == "501" else "post_merge_receipt"
            ],
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
    assert (
        candidate["candidate"]["source"]["post_merge_exact_head_receipt"]["record_reference"][
            "artifact_id"
        ]
        == "502"
    )
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
    _write_post_merge_scan_receipt(authority_root, source_commit)
    produce_post_merge_exact_head_receipt(authority_root, environment)
    environment.update(
        {
            "A1_PAYLOAD_ARTIFACT_ID": "501",
            "A1_POST_MERGE_RECEIPT_ARTIFACT_ID": "502",
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
        "produce-post-merge-receipt",
        "seal-build-records",
    ),
)
def test_build_cli_exposes_only_defined_foundation_operations(command: str) -> None:
    assert parse_args([command]).command == command

    with pytest.raises(SystemExit):
        parse_args([command, "--source", "b" * 40])


def test_stateless_preview_workflow_is_build_only_and_immutable() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    dispatch = workflow.split("workflow_dispatch:", maxsplit=1)[1].split(
        "permissions:", maxsplit=1
    )[0]
    input_names = re.findall(r"^      ([a-z_]+):$", dispatch, flags=re.MULTILINE)

    assert input_names == ["preview_version", "preview_tag", "retention_days"]
    assert "\n  promote:" not in workflow
    assert "permissions: {}" in workflow
    assert "contents: read" in workflow
    assert "actions: read" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "python scripts/run_sonarqube_exact_head.py --role post-merge" in workflow
    assert "path: artifacts/stateless-preview/post-merge-receipt/" in workflow
    assert "A1_PAYLOAD_ARTIFACT_ID: ${{ steps.upload_payload.outputs.artifact-id }}" in workflow
    assert (
        "A1_POST_MERGE_RECEIPT_ARTIFACT_ID: "
        "${{ steps.upload_post_merge_receipt.outputs.artifact-id }}"
    ) in workflow
    for command in (
        "admit-build",
        "prepare-payload",
        "produce-post-merge-receipt",
        "seal-build-records",
    ):
        assert f"python scripts/stateless_preview_artifact.py {command}" in workflow
    assert workflow.count("uses: actions/upload-artifact@v7") == 4
    assert workflow.count("if-no-files-found: error") == 4
    assert workflow.count("overwrite: false") == 4
    assert workflow.count("retention-days: ${{ steps.admit.outputs.retention_days }}") == 4
    assert "--source" not in workflow
    assert "--receipt" not in workflow
    for forbidden in (
        "actions/setup-python",
        "pip install",
        "python -m build",
        "uv ",
        "pypi",
        "publish.yml",
        "twine",
    ):
        assert forbidden not in workflow.lower()


def test_t006_t008_contract_records_only_the_executable_build_foundation() -> None:
    tasks = (PROJECT_ROOT / "specs" / "010-a1-preview-artifact" / "tasks.md").read_text(
        encoding="utf-8"
    )
    workflow_contract = (
        PROJECT_ROOT
        / "specs"
        / "010-a1-preview-artifact"
        / "contracts"
        / "stateless-preview-workflow.md"
    ).read_text(encoding="utf-8")

    assert "- [x] T006" in tasks
    assert "- [x] T008" in tasks
    assert "one executable manual `build` surface" in workflow_contract
    assert "Promotion remains a future consuming surface" in workflow_contract


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
        "receipt_provenance": _consumer_proof_reference(
            artifact_id="602",
            artifact_name="stateless-preview-retained-proof",
            path="retained-artifact-proof.json",
            sha256="e" * 64,
            run_id="602",
        ),
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


def _receipt_provenance_record(source_commit: str) -> dict[str, str]:
    return {
        "record_type": "sonarqube-exact-head",
        "repository": REPOSITORY,
        "source_ref": SOURCE_REF,
        "stage": "post-merge",
        "scanned_commit": source_commit,
        "tag_target": source_commit,
        "outcome": "PASS",
    }


def _receipt_provenance_download_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    receipt: Mapping[str, Any] | None = None,
    member_path: str = "receipts/post-merge-exact-head.json",
    archived_member_path: str | None = None,
    artifact_sha256: str | None = None,
    file_sha256: str | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    source_commit = "a" * 40
    receipt_bytes = _canonical_json_bytes(
        _receipt_provenance_record(source_commit) if receipt is None else receipt
    )
    wire_bytes = _validator_wire_zip(
        tmp_path,
        {archived_member_path or member_path: receipt_bytes},
    )
    reference = _validator_artifact_reference(
        artifact_sha256=(_sha256_bytes(wire_bytes) if artifact_sha256 is None else artifact_sha256),
        path=member_path,
        sha256=_sha256_bytes(receipt_bytes) if file_sha256 is None else file_sha256,
    )
    candidate_source = {
        "repository": REPOSITORY,
        "ref": SOURCE_REF,
        "commit": source_commit,
        "post_merge_exact_head_receipt": {
            "record_reference": {
                "repository": reference.repository,
                "run_id": reference.run_id,
                "artifact_id": reference.artifact_id,
                "path": reference.path,
                "sha256": reference.sha256,
            },
            "stage": "post-merge",
            "record_type": "sonarqube-exact-head",
            "scanned_commit": source_commit,
            "tag_target": source_commit,
            "outcome": "PASS",
        },
    }
    downloader = preview_validator.ArtifactDownloader(tmp_path / "fresh-download")
    monkeypatch.setattr(downloader, "_metadata", lambda _: None)
    monkeypatch.setattr(downloader, "_run_gh", lambda *_: wire_bytes)
    return downloader, reference, candidate_source


def test_downloaded_receipt_provenance_binds_candidate_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader, reference, candidate_source = _receipt_provenance_download_fixture(
        tmp_path, monkeypatch
    )

    validated = preview_validator._download_and_validate_receipt_provenance(
        downloader,
        reference,
        candidate_source,
    )

    assert validated.as_mapping() == reference.as_mapping()


def test_downloaded_receipt_provenance_refuses_nonexistent_artifact_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader, reference, candidate_source = _receipt_provenance_download_fixture(
        tmp_path,
        monkeypatch,
        archived_member_path="receipts/not-the-post-merge-receipt.json",
    )

    with pytest.raises(ValueError, match="file is absent or ambiguous"):
        preview_validator._download_and_validate_receipt_provenance(
            downloader,
            reference,
            candidate_source,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "another/repository"),
        ("run_id", "702"),
        ("artifact_id", "702"),
        ("path", "receipts/different-post-merge-receipt.json"),
        ("sha256", "0" * 64),
    ],
)
def test_downloaded_receipt_provenance_refuses_mismatched_candidate_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    downloader, reference, candidate_source = _receipt_provenance_download_fixture(
        tmp_path, monkeypatch
    )
    candidate_source["post_merge_exact_head_receipt"]["record_reference"][field] = value

    with pytest.raises(ValueError, match="receipt provenance reference"):
        preview_validator._download_and_validate_receipt_provenance(
            downloader,
            reference,
            candidate_source,
        )


@pytest.mark.parametrize(
    ("artifact_sha256", "file_sha256", "message"),
    [
        ("0" * 64, None, "wire bytes"),
        (None, "0" * 64, "file bytes"),
    ],
)
def test_downloaded_receipt_provenance_refuses_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_sha256: str | None,
    file_sha256: str | None,
    message: str,
) -> None:
    downloader, reference, candidate_source = _receipt_provenance_download_fixture(
        tmp_path,
        monkeypatch,
        artifact_sha256=artifact_sha256,
        file_sha256=file_sha256,
    )

    with pytest.raises(ValueError, match=message):
        preview_validator._download_and_validate_receipt_provenance(
            downloader,
            reference,
            candidate_source,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_type", "artifact-consumer-proof"),
        ("stage", "candidate"),
        ("scanned_commit", "b" * 40),
        ("tag_target", "b" * 40),
        ("outcome", "FAIL"),
        ("repository", "another/repository"),
        ("source_ref", "refs/heads/other"),
    ],
)
def test_downloaded_receipt_provenance_refuses_semantic_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    receipt = _receipt_provenance_record("a" * 40)
    receipt[field] = value
    downloader, reference, candidate_source = _receipt_provenance_download_fixture(
        tmp_path,
        monkeypatch,
        receipt=receipt,
    )

    with pytest.raises(ValueError, match="candidate exact-head receipt"):
        preview_validator._download_and_validate_receipt_provenance(
            downloader,
            reference,
            candidate_source,
        )


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


def test_python_rollback_refuses_zero_exit_without_product_works_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_output = b'{"result":"zero exit without rollback marker"}\n'

    monkeypatch.setattr(
        preview_validator.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["consumer-python"],
            returncode=0,
            stdout=raw_output,
            stderr=b"",
        ),
    )

    with pytest.raises(ValueError, match="did not emit PRODUCT_WORKS") as error:
        preview_validator._run_python_rollback("consumer-python", ["consumer.py"])

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
            args=["consumer-python"],
            returncode=0,
            stdout=json.dumps(marker, sort_keys=True).encode("utf-8") + b"\n",
            stderr=b"",
        ),
    )

    assert preview_validator._run_python_rollback("consumer-python", ["consumer.py"]) == {
        "only_preview_selection_removed": True,
        "python_package_reinstalled": False,
        "python_package_replaced": False,
        "console_entrypoint_changed": False,
        "default_selector_changed": False,
        "legacy_journey_outcome": "PRODUCT_WORKS",
    }
