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
