"""Fail-closed identity and retained-byte helpers for the A1 preview artifact."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_CANONICAL_SOURCE_REF = "refs/heads/main"
_TRUSTED_WORKFLOW_PATH = ".github/workflows/stateless-preview.yml"
_TRUSTED_BUILD_MODE = "build"
_TRUSTED_BUILD_EVENT = "workflow_dispatch"
_PREVIEW_EXECUTABLE = "netcoredbg-mcp-stateless-preview.exe"

_POLICY_AUTHORITY_PATHS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/RELEASE-PROTOCOL.md",
    "docs/adr/ADR-004-stateless-preview.md",
    "specs/010-a1-preview-artifact/spec.md",
)
_AUTHORITY_RULES = (
    "AGENTS_POLICY",
    "CONTRIBUTING_POLICY",
    "RELEASE_PROTOCOL",
    "ADR_004_STATELESS_PREVIEW",
    "A1_PREVIEW_ARTIFACT_SPEC",
)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_IDENTIFIER_PATTERN = re.compile(r"^[1-9][0-9]*$")
_GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_ACTOR_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?$")
_SAFE_RELATIVE_PATH_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_PREVIEW_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-preview\.(?:[1-9][0-9]*)$"
)


class _FrozenDict(dict[str, Any]):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("artifact contract outputs are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class _FrozenList(list[Any]):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("artifact contract outputs are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


def _expect_mapping(value: Any, name: str, required_keys: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _refuse(f"{name} must be an object")
    keys = set(value)
    expected = set(required_keys)
    if keys != expected:
        _refuse(f"{name} has unsupported or missing fields")
    return value


def _expect_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        _refuse(f"{name} must be a string")
    return value


def _expect_positive_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _refuse(f"{name} must be a positive integer")
    return value


def _expect_pattern(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    text = _expect_string(value, name)
    if pattern.fullmatch(text) is None:
        _refuse(f"{name} has an invalid value")
    return text


def _expect_sha256(value: Any, name: str) -> str:
    return _expect_pattern(value, name, _SHA256_PATTERN)


def _expect_commit(value: Any, name: str) -> str:
    return _expect_pattern(value, name, _COMMIT_PATTERN)


def _expect_repository(value: Any, name: str) -> str:
    return _expect_pattern(value, name, _GITHUB_REPOSITORY_PATTERN)


def _expect_identifier(value: Any, name: str) -> str:
    return _expect_pattern(value, name, _GITHUB_IDENTIFIER_PATTERN)


def _expect_safe_relative_path(value: Any, name: str) -> str:
    return _expect_pattern(value, name, _SAFE_RELATIVE_PATH_PATTERN)


def _expect_datetime(value: Any, name: str) -> str:
    timestamp = _expect_string(value, name)
    try:
        parsed = datetime.fromisoformat(
            timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        )
    except ValueError:
        _refuse(f"{name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        _refuse(f"{name} must include a timezone")
    return timestamp


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze(item) for item in value)
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_file_descriptor(
    value: Any,
    name: str,
    *,
    expected_name: str | None = None,
) -> Mapping[str, Any]:
    descriptor = _expect_mapping(value, name, ("name", "size_bytes", "sha256"))
    file_name = _expect_string(descriptor["name"], f"{name}.name")
    if not file_name:
        _refuse(f"{name}.name must not be empty")
    if expected_name is not None and file_name != expected_name:
        _refuse(f"{name}.name does not match the candidate")
    _expect_positive_integer(descriptor["size_bytes"], f"{name}.size_bytes")
    _expect_sha256(descriptor["sha256"], f"{name}.sha256")
    return descriptor


def _validate_artifact_file_reference(value: Any, name: str) -> Mapping[str, Any]:
    reference = _expect_mapping(
        value,
        name,
        ("repository", "run_id", "artifact_id", "path", "sha256"),
    )
    _expect_repository(reference["repository"], f"{name}.repository")
    _expect_identifier(reference["run_id"], f"{name}.run_id")
    _expect_identifier(reference["artifact_id"], f"{name}.artifact_id")
    _expect_safe_relative_path(reference["path"], f"{name}.path")
    _expect_sha256(reference["sha256"], f"{name}.sha256")
    return reference


def _validate_actions_artifact(value: Any) -> Mapping[str, Any]:
    artifact = _expect_mapping(
        value, "candidate.build.artifact", ("id", "name", "sha256", "retention")
    )
    _expect_identifier(artifact["id"], "candidate.build.artifact.id")
    artifact_name = _expect_string(artifact["name"], "candidate.build.artifact.name")
    if not artifact_name or len(artifact_name) > 255:
        _refuse("candidate.build.artifact.name is invalid")
    _expect_sha256(artifact["sha256"], "candidate.build.artifact.sha256")
    retention = _expect_mapping(
        artifact["retention"],
        "candidate.build.artifact.retention",
        ("configured_days", "expires_at"),
    )
    _expect_positive_integer(
        retention["configured_days"], "candidate.build.artifact.retention.configured_days"
    )
    _expect_datetime(retention["expires_at"], "candidate.build.artifact.retention.expires_at")
    return artifact


def _validate_preview_manifest(value: Any, expected_commit: str) -> Mapping[str, Any]:
    manifest = _expect_mapping(
        value,
        "candidate.preview_manifest.contents",
        ("schema_version", "version", "tag", "commit", "rid", "archive", "executable"),
    )
    if manifest["schema_version"] != "1.0":
        _refuse("candidate.preview_manifest.contents.schema_version is unsupported")
    version = _expect_pattern(
        manifest["version"], "candidate.preview_manifest.contents.version", _PREVIEW_VERSION_PATTERN
    )
    tag = _expect_string(manifest["tag"], "candidate.preview_manifest.contents.tag")
    if tag != f"stateless-preview-v{version}":
        _refuse("candidate.preview_manifest.contents.tag does not match the version")
    commit = _expect_commit(manifest["commit"], "candidate.preview_manifest.contents.commit")
    if commit != expected_commit:
        _refuse("candidate.preview_manifest.contents.commit does not match the source")
    if manifest["rid"] != "win-x64":
        _refuse("candidate.preview_manifest.contents.rid is unsupported")
    archive_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.zip"
    _validate_file_descriptor(
        manifest["archive"],
        "candidate.preview_manifest.contents.archive",
        expected_name=archive_name,
    )
    _validate_file_descriptor(
        manifest["executable"],
        "candidate.preview_manifest.contents.executable",
        expected_name=_PREVIEW_EXECUTABLE,
    )
    return manifest


def _validate_exact_reference(
    reference: Mapping[str, Any],
    name: str,
    *,
    repository: str,
    run_id: str,
    artifact_id: str,
    path: str,
    sha256: str,
) -> None:
    if (
        reference["repository"] != repository
        or reference["run_id"] != run_id
        or reference["artifact_id"] != artifact_id
        or reference["path"] != path
        or reference["sha256"] != sha256
    ):
        _refuse(f"{name} does not bind the retained candidate")


def _validate_candidate_identity(value: Any) -> dict[str, Any]:
    identity = _thaw(value)
    root = _expect_mapping(identity, "candidate identity", ("schema_version", "candidate"))
    if root["schema_version"] != "1.0":
        _refuse("candidate identity schema_version is unsupported")

    candidate = _expect_mapping(
        root["candidate"],
        "candidate",
        ("source", "build", "preview_manifest", "destination"),
    )
    source = _expect_mapping(
        candidate["source"],
        "candidate.source",
        ("repository", "ref", "commit", "origin_main_target"),
    )
    source_repository = _expect_repository(source["repository"], "candidate.source.repository")
    if source["ref"] != _CANONICAL_SOURCE_REF:
        _refuse("candidate.source.ref is not canonical main")
    source_commit = _expect_commit(source["commit"], "candidate.source.commit")
    origin_main_target = _expect_commit(
        source["origin_main_target"], "candidate.source.origin_main_target"
    )

    build = _expect_mapping(
        candidate["build"],
        "candidate.build",
        (
            "repository",
            "workflow_path",
            "mode",
            "event",
            "run_id",
            "run_attempt",
            "ref",
            "commit",
            "artifact",
        ),
    )
    build_repository = _expect_repository(build["repository"], "candidate.build.repository")
    if build_repository != source_repository:
        _refuse("candidate build has a different repository")
    if (
        build["workflow_path"] != _TRUSTED_WORKFLOW_PATH
        or build["mode"] != _TRUSTED_BUILD_MODE
        or build["event"] != _TRUSTED_BUILD_EVENT
        or build["ref"] != _CANONICAL_SOURCE_REF
    ):
        _refuse("candidate build does not have trusted canonical-main provenance")
    build_run_id = _expect_identifier(build["run_id"], "candidate.build.run_id")
    _expect_positive_integer(build["run_attempt"], "candidate.build.run_attempt")
    build_commit = _expect_commit(build["commit"], "candidate.build.commit")
    artifact = _validate_actions_artifact(build["artifact"])

    preview_manifest = _expect_mapping(
        candidate["preview_manifest"],
        "candidate.preview_manifest",
        ("file", "manifest_reference", "archive_reference", "contents"),
    )
    manifest_contents = _validate_preview_manifest(preview_manifest["contents"], source_commit)
    version = manifest_contents["version"]
    manifest_file_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.manifest.json"
    manifest_file = _validate_file_descriptor(
        preview_manifest["file"],
        "candidate.preview_manifest.file",
        expected_name=manifest_file_name,
    )
    manifest_reference = _validate_artifact_file_reference(
        preview_manifest["manifest_reference"], "candidate.preview_manifest.manifest_reference"
    )
    archive_reference = _validate_artifact_file_reference(
        preview_manifest["archive_reference"], "candidate.preview_manifest.archive_reference"
    )
    _validate_exact_reference(
        manifest_reference,
        "candidate.preview_manifest.manifest_reference",
        repository=source_repository,
        run_id=build_run_id,
        artifact_id=artifact["id"],
        path=manifest_file["name"],
        sha256=manifest_file["sha256"],
    )
    _validate_exact_reference(
        archive_reference,
        "candidate.preview_manifest.archive_reference",
        repository=source_repository,
        run_id=build_run_id,
        artifact_id=artifact["id"],
        path=manifest_contents["archive"]["name"],
        sha256=manifest_contents["archive"]["sha256"],
    )

    destination = _expect_mapping(
        candidate["destination"],
        "candidate.destination",
        ("provider", "repository", "tag", "prerelease"),
    )
    if destination["provider"] != "github":
        _refuse("candidate.destination.provider is unsupported")
    destination_repository = _expect_repository(
        destination["repository"], "candidate.destination.repository"
    )
    if destination_repository != source_repository:
        _refuse("candidate destination has a different repository")
    if destination["tag"] != manifest_contents["tag"] or destination["prerelease"] is not True:
        _refuse("candidate destination does not match the preview manifest")

    if len({source_commit, origin_main_target, build_commit}) != 1:
        _refuse("candidate commits do not bind one canonical main target")

    return identity


def assemble_candidate_identity(candidate_input: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and seal a candidate identity without retaining caller-owned state."""

    return _freeze(_validate_candidate_identity(candidate_input))


def _read_artifact_bytes(path_value: str | PathLike[str], name: str) -> tuple[Path, bytes]:
    try:
        path = Path(path_value)
    except TypeError:
        _refuse(f"{name} path is invalid")
    try:
        if not path.is_file():
            _refuse(f"{name} is unavailable")
        return path, path.read_bytes()
    except OSError:
        _refuse(f"{name} is unreadable")
    raise AssertionError("unreachable")


def _verify_recorded_file(
    path: Path, contents: bytes, descriptor: Mapping[str, Any], name: str
) -> None:
    if path.name != descriptor["name"]:
        _refuse(f"{name} name does not match the candidate")
    if len(contents) != descriptor["size_bytes"] or _sha256_bytes(contents) != descriptor["sha256"]:
        _refuse(f"{name} bytes do not match the candidate")


def _reject_json_constant(value: str) -> None:
    _refuse(f"manifest contains unsupported JSON constant {value}")


def _load_manifest(raw_manifest: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _refuse("manifest contains a duplicate key")
            result[key] = value
        return result

    try:
        decoded = raw_manifest.decode("utf-8")
        manifest = json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("manifest is not valid UTF-8 JSON")
    if not isinstance(manifest, dict):
        _refuse("manifest must contain an object")
    return manifest


def _verify_archive_member(
    archive_bytes: bytes, executable_name: str, executable_bytes: bytes
) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = [entry for entry in archive.infolist() if entry.filename == executable_name]
            if len(members) != 1 or members[0].is_dir():
                _refuse("archive does not contain exactly one executable member")
            member = members[0]
            archive_member_bytes = archive.read(member)
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        _refuse("archive is unreadable")
    if member.file_size != len(executable_bytes) or archive_member_bytes != executable_bytes:
        _refuse("archive executable member does not match the verified executable")


def _verified_retained_artifact_inputs(
    identity: Mapping[str, Any],
    archive_path: str | PathLike[str],
    manifest_path: str | PathLike[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Path, bytes]:
    validated_identity = _validate_candidate_identity(identity)
    candidate = validated_identity["candidate"]
    manifest_binding = candidate["preview_manifest"]
    manifest_contents = manifest_binding["contents"]
    archive_file, archive_bytes = _read_artifact_bytes(archive_path, "archive")
    manifest_file, manifest_bytes = _read_artifact_bytes(manifest_path, "manifest")

    _verify_recorded_file(archive_file, archive_bytes, manifest_contents["archive"], "archive")
    _verify_recorded_file(manifest_file, manifest_bytes, manifest_binding["file"], "manifest")
    observed_manifest = _load_manifest(manifest_bytes)
    _validate_preview_manifest(observed_manifest, candidate["source"]["commit"])
    if observed_manifest != manifest_contents:
        _refuse("manifest contents do not match the candidate")
    payload_hash = hashlib.sha256()
    payload_hash.update(archive_bytes)
    payload_hash.update(manifest_bytes)
    if payload_hash.hexdigest() != candidate["build"]["artifact"]["sha256"]:
        _refuse("retained payload digest does not match the candidate")
    return candidate, manifest_binding, manifest_contents, archive_file, archive_bytes


def _retained_verification_record(
    candidate: Mapping[str, Any],
    manifest_binding: Mapping[str, Any],
    manifest_contents: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _freeze(
        {
            "source_ref": candidate["source"]["ref"],
            "source_commit": candidate["source"]["commit"],
            "archive": _thaw(manifest_contents["archive"]),
            "manifest": _thaw(manifest_binding["file"]),
            "executable": _thaw(manifest_contents["executable"]),
        }
    )


def verify_retained_artifact_inputs(
    identity: Mapping[str, Any],
    archive_path: str | PathLike[str],
    manifest_path: str | PathLike[str],
) -> Mapping[str, Any]:
    """Verify downloaded archive and manifest bytes before archive extraction."""

    candidate, manifest_binding, manifest_contents, _, _ = _verified_retained_artifact_inputs(
        identity, archive_path, manifest_path
    )
    return _retained_verification_record(candidate, manifest_binding, manifest_contents)


def verify_retained_artifact(
    identity: Mapping[str, Any],
    archive_path: str | PathLike[str],
    manifest_path: str | PathLike[str],
    executable_path: str | PathLike[str],
) -> Mapping[str, Any]:
    """Replay every candidate byte equation before an extracted executable is used."""

    candidate, manifest_binding, manifest_contents, _, archive_bytes = (
        _verified_retained_artifact_inputs(identity, archive_path, manifest_path)
    )
    executable_file, executable_bytes = _read_artifact_bytes(executable_path, "executable")
    _verify_recorded_file(
        executable_file, executable_bytes, manifest_contents["executable"], "executable"
    )
    _verify_archive_member(archive_bytes, manifest_contents["executable"]["name"], executable_bytes)
    return _retained_verification_record(candidate, manifest_binding, manifest_contents)


def verify_and_extract_retained_artifact(
    identity: Mapping[str, Any],
    archive_path: str | PathLike[str],
    manifest_path: str | PathLike[str],
    extraction_directory: str | PathLike[str],
) -> Mapping[str, Any]:
    """Verify retained inputs, then extract only the verified executable once."""

    candidate, manifest_binding, manifest_contents, _, archive_bytes = (
        _verified_retained_artifact_inputs(identity, archive_path, manifest_path)
    )
    destination = Path(extraction_directory)
    try:
        if destination.exists() or destination.is_symlink():
            _refuse("verified extraction directory already exists")
        destination.mkdir(parents=True, exist_ok=False)
    except OSError:
        _refuse("verified extraction directory is unavailable")
    executable_name = manifest_contents["executable"]["name"]
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = [entry for entry in archive.infolist() if entry.filename == executable_name]
            if len(members) != 1 or members[0].is_dir():
                _refuse("archive does not contain exactly one executable member")
            executable_bytes = archive.read(members[0])
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        _refuse("archive is unreadable")
    executable_path = destination / executable_name
    try:
        with executable_path.open("xb") as output:
            output.write(executable_bytes)
    except OSError:
        _refuse("verified executable cannot be extracted")
    _verify_recorded_file(
        executable_path, executable_bytes, manifest_contents["executable"], "executable"
    )
    _verify_archive_member(archive_bytes, executable_name, executable_bytes)
    return _freeze(
        {
            **_thaw(_retained_verification_record(candidate, manifest_binding, manifest_contents)),
            "executable_path": executable_path,
        }
    )


def _git_bytes(repository_root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
    except OSError:
        _refuse("git is unavailable for canonical policy resolution")
    if result.returncode != 0:
        _refuse("canonical policy resolution failed")
    return result.stdout


def _git_text(repository_root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(repository_root, *arguments).decode("utf-8").strip()
    except UnicodeDecodeError:
        _refuse("git returned invalid canonical policy metadata")
    raise AssertionError("unreachable")


def _resolve_repository_root(repository_root: str | PathLike[str]) -> Path:
    try:
        root = Path(repository_root).resolve(strict=True)
    except (OSError, TypeError):
        _refuse("policy authority root is unavailable")
    if not root.is_dir():
        _refuse("policy authority root is not a directory")
    reported_root = _git_text(root, "rev-parse", "--show-toplevel")
    try:
        if Path(reported_root).resolve(strict=True) != root:
            _refuse("policy authority root must be the repository root")
    except OSError:
        _refuse("canonical policy repository root is unavailable")
    return root


def _resolve_main_commit(repository_root: Path, source_commit: str) -> None:
    origin_main = _git_text(
        repository_root,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main^{commit}",
    )
    if _COMMIT_PATTERN.fullmatch(origin_main) is None or origin_main != source_commit:
        _refuse("candidate is not the exact canonical main target")


def _read_tracked_authority(repository_root: Path, source_commit: str, relative_path: str) -> bytes:
    object_name = f"{source_commit}:{relative_path}"
    object_type = _git_text(repository_root, "cat-file", "-t", object_name)
    if object_type != "blob":
        _refuse("policy authority is not a tracked file")
    return _git_bytes(repository_root, "cat-file", "blob", object_name)


def _fixed_gate_descriptors() -> list[dict[str, Any]]:
    requirements = {
        "retained-downloaded-consumer-proof": "RETAINED_ARTIFACT_PROOF",
        "s2-s3-seven-lens-evidence": "S2_S3_SEVEN_LENS_AGGREGATE",
        "independent-review": "INDEPENDENT_PR_REVIEW",
        "remote-downloaded-consumer-proof": "REMOTE_CONSUMER_PROOF",
    }
    rows = (
        ("pre-decision", "retained-downloaded-consumer-proof", "artifact-consumer-proof"),
        ("pre-decision", "s2-s3-seven-lens-evidence", "s2-s3-seven-lens-aggregate"),
        ("pre-decision", "independent-review", "independent-pr-review"),
        ("post-publication", "remote-downloaded-consumer-proof", "artifact-consumer-proof"),
    )
    return [
        {
            "stage": stage,
            "gate_id": gate_id,
            "record_type": record_type,
            "authority_rules": list(_AUTHORITY_RULES),
            "evidence_requirements": [
                "CANDIDATE_IDENTITY_MATCH",
                "CANONICAL_MAIN_PROVENANCE",
                "PASSING_OUTCOME",
                requirements[gate_id],
            ],
        }
        for stage, gate_id, record_type in rows
    ]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_release_gate_catalog(
    identity: Mapping[str, Any], repository_root: str | PathLike[str]
) -> Mapping[str, Any]:
    """Resolve the closed A1 gate catalog from the candidate's exact main commit."""

    validated_identity = _validate_candidate_identity(identity)
    source_commit = validated_identity["candidate"]["source"]["commit"]
    root = _resolve_repository_root(repository_root)
    _resolve_main_commit(root, source_commit)

    snapshots = [
        {
            "path": relative_path,
            "sha256": _sha256_bytes(_read_tracked_authority(root, source_commit, relative_path)),
            "source_commit": source_commit,
        }
        for relative_path in _POLICY_AUTHORITY_PATHS
    ]
    return _freeze(
        {
            "catalog_schema_version": "1.0",
            "catalog": {
                "producer": {
                    "helper_path": "scripts/stateless_preview_artifact.py",
                    "operation": "resolve_release_gate_catalog",
                },
                "source_ref": _CANONICAL_SOURCE_REF,
                "source_commit": source_commit,
                "policy_authority_snapshots": snapshots,
                "gate_descriptors": _fixed_gate_descriptors(),
                "resolved_at": _utc_timestamp(),
            },
        }
    )


_PREVIEW_ARTIFACT_ROOT = Path("artifacts") / "stateless-preview"
_MAX_ACTIONS_RETENTION_DAYS = 90
_CANDIDATE_IDENTITY_FILENAME = "candidate-identity.json"
_RELEASE_GATE_CATALOG_FILENAME = "release-gate-catalog.json"


def _environment_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value:
        _refuse(f"{name} is unavailable")
    return value


def _expect_environment_identifier(environment: Mapping[str, str], name: str) -> str:
    return _expect_pattern(_environment_value(environment, name), name, _GITHUB_IDENTIFIER_PATTERN)


def _expect_retention_days(environment: Mapping[str, str]) -> int:
    text = _expect_environment_identifier(environment, "A1_RETENTION_DAYS")
    days = int(text)
    if days > _MAX_ACTIONS_RETENTION_DAYS:
        _refuse("A1_RETENTION_DAYS exceeds the bounded Actions retention limit")
    return days


def _artifact_names(version: str, run_id: str, run_attempt: int) -> dict[str, str]:
    suffix = f"{version}-{run_id}-{run_attempt}"
    return {
        "payload": f"stateless-preview-payload-{suffix}",
        "candidate_identity": f"stateless-preview-candidate-identity-{suffix}",
        "release_gate_catalog": f"stateless-preview-release-gate-catalog-{suffix}",
    }


def _canonical_build_source(
    repository_root: Path, environment: Mapping[str, str]
) -> dict[str, Any]:
    repository = _expect_repository(
        _environment_value(environment, "GITHUB_REPOSITORY"), "GITHUB_REPOSITORY"
    )
    if _environment_value(environment, "GITHUB_EVENT_NAME") != _TRUSTED_BUILD_EVENT:
        _refuse("build admission requires workflow_dispatch")
    if _environment_value(environment, "GITHUB_REF") != _CANONICAL_SOURCE_REF:
        _refuse("build admission requires canonical main")
    workflow_ref = _environment_value(environment, "GITHUB_WORKFLOW_REF")
    expected_workflow_ref = f"{repository}/{_TRUSTED_WORKFLOW_PATH}@{_CANONICAL_SOURCE_REF}"
    if workflow_ref != expected_workflow_ref:
        _refuse("build admission requires the trusted canonical-main workflow")

    head = _git_text(repository_root, "rev-parse", "--verify", "HEAD^{commit}")
    github_sha = _expect_commit(_environment_value(environment, "GITHUB_SHA"), "GITHUB_SHA")
    if _COMMIT_PATTERN.fullmatch(head) is None or head != github_sha:
        _refuse("workflow checkout does not match the GitHub source commit")
    _resolve_main_commit(repository_root, head)
    return {
        "repository": repository,
        "ref": _CANONICAL_SOURCE_REF,
        "commit": head,
    }


def admit_build(
    repository_root: str | PathLike[str], environment: Mapping[str, str]
) -> Mapping[str, Any]:
    """Derive the only build source and bounded preview inputs from Actions context."""

    root = _resolve_repository_root(repository_root)
    source = _canonical_build_source(root, environment)
    version = _expect_pattern(
        _environment_value(environment, "A1_PREVIEW_VERSION"),
        "A1_PREVIEW_VERSION",
        _PREVIEW_VERSION_PATTERN,
    )
    tag = _expect_string(_environment_value(environment, "A1_PREVIEW_TAG"), "A1_PREVIEW_TAG")
    if tag != f"stateless-preview-v{version}":
        _refuse("A1_PREVIEW_TAG does not match A1_PREVIEW_VERSION")
    retention_days = _expect_retention_days(environment)
    run_id = _expect_environment_identifier(environment, "GITHUB_RUN_ID")
    run_attempt = int(_expect_environment_identifier(environment, "GITHUB_RUN_ATTEMPT"))
    return _freeze(
        {
            "source": source,
            "preview": {
                "version": version,
                "tag": tag,
                "retention_days": retention_days,
            },
            "run": {"id": run_id, "attempt": run_attempt},
            "artifacts": _artifact_names(version, run_id, run_attempt),
        }
    )


def _artifact_root(repository_root: Path) -> Path:
    return repository_root / _PREVIEW_ARTIFACT_ROOT


def _write_bytes_once(path: Path, contents: bytes, name: str) -> None:
    if path.exists() or path.is_symlink():
        _refuse(f"{name} already exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    except OSError:
        _refuse(f"{name} cannot be written")


def _read_regular_bytes(path: Path, name: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            _refuse(f"{name} is unavailable")
        return path.read_bytes()
    except OSError:
        _refuse(f"{name} is unreadable")
    raise AssertionError("unreachable")


def _canonical_json_bytes(value: Any, name: str) -> bytes:
    try:
        return (
            json.dumps(
                _thaw(value),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        _refuse(f"{name} cannot be serialized")
    raise AssertionError("unreachable")


def _read_exact_preview_archive_member(archive_bytes: bytes, executable_name: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].is_dir() or members[0].filename != executable_name:
                _refuse("archive does not contain one preview executable")
            return archive.read(members[0])
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        _refuse("archive is unreadable")
    raise AssertionError("unreachable")


def prepare_preview_payload(
    repository_root: str | PathLike[str], environment: Mapping[str, str]
) -> Mapping[str, Any]:
    """Archive one published preview executable and seal its inherited manifest."""

    root = _resolve_repository_root(repository_root)
    admission = admit_build(root, environment)
    source = admission["source"]
    preview = admission["preview"]
    publish_directory = _artifact_root(root) / "publish"
    source_executable = publish_directory / _PREVIEW_EXECUTABLE
    executable_bytes = _read_regular_bytes(source_executable, "published preview executable")
    if not executable_bytes:
        _refuse("published preview executable is empty")

    payload_directory = _artifact_root(root) / "payload"
    if payload_directory.exists() or payload_directory.is_symlink():
        _refuse("preview payload directory already exists")
    try:
        payload_directory.mkdir(parents=True)
    except OSError:
        _refuse("preview payload directory cannot be created")

    version = preview["version"]
    archive_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.zip"
    manifest_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.manifest.json"
    archive_path = payload_directory / archive_name
    entry = zipfile.ZipInfo(_PREVIEW_EXECUTABLE, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_STORED
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(entry, executable_bytes)
    except (OSError, RuntimeError):
        _refuse("preview archive cannot be written")
    archive_bytes = _read_regular_bytes(archive_path, "preview archive")
    archive_executable = _read_exact_preview_archive_member(archive_bytes, _PREVIEW_EXECUTABLE)
    if archive_executable != executable_bytes:
        _refuse("preview archive executable does not match publish output")

    manifest_contents = {
        "schema_version": "1.0",
        "version": version,
        "tag": preview["tag"],
        "commit": source["commit"],
        "rid": "win-x64",
        "archive": {
            "name": archive_name,
            "size_bytes": len(archive_bytes),
            "sha256": _sha256_bytes(archive_bytes),
        },
        "executable": {
            "name": _PREVIEW_EXECUTABLE,
            "size_bytes": len(executable_bytes),
            "sha256": _sha256_bytes(executable_bytes),
        },
    }
    _validate_preview_manifest(manifest_contents, source["commit"])
    manifest_bytes = _canonical_json_bytes(manifest_contents, "preview manifest")
    manifest_path = payload_directory / manifest_name
    _write_bytes_once(manifest_path, manifest_bytes, "preview manifest")
    manifest_file = {
        "name": manifest_name,
        "size_bytes": len(manifest_bytes),
        "sha256": _sha256_bytes(manifest_bytes),
    }
    return _freeze(
        {
            "archive": _thaw(manifest_contents["archive"]),
            "manifest": manifest_file,
            "executable": _thaw(manifest_contents["executable"]),
        }
    )


def _load_json_object(raw: bytes, name: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _refuse(f"{name} contains a duplicate key")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8")
        result = json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: _refuse(
                f"{name} contains unsupported JSON constant {value}"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse(f"{name} is not valid UTF-8 JSON")
    if not isinstance(result, dict):
        _refuse(f"{name} must contain an object")
    return result


def _read_github_artifact(repository: str, artifact_id: str, token: str) -> Mapping[str, Any]:
    if not token:
        _refuse("GITHUB_TOKEN is unavailable")
    request = Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
    except (HTTPError, URLError, OSError):
        _refuse("uploaded artifact metadata is unavailable")
    return _load_json_object(raw, "uploaded artifact metadata")


def _validate_uploaded_artifact(
    value: Mapping[str, Any],
    *,
    artifact_id: str,
    expected_name: str,
    current_run_id: str,
) -> Mapping[str, Any]:
    numeric_id = value.get("id")
    workflow_run = value.get("workflow_run")
    if (
        type(numeric_id) is not int
        or str(numeric_id) != artifact_id
        or value.get("name") != expected_name
        or value.get("expired") is not False
        or not isinstance(workflow_run, Mapping)
        or str(workflow_run.get("id")) != current_run_id
    ):
        _refuse("uploaded artifact is not owned by this build run")
    expires_at = _expect_datetime(value.get("expires_at"), "uploaded artifact expires_at")
    return {"id": artifact_id, "name": expected_name, "expires_at": expires_at}


def seal_build_records(
    repository_root: str | PathLike[str],
    environment: Mapping[str, str],
    *,
    artifact_reader: Callable[[str, str, str], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Seal the candidate identity and fixed catalog from current-run artifact metadata."""

    root = _resolve_repository_root(repository_root)
    admission = admit_build(root, environment)
    source = admission["source"]
    preview = admission["preview"]
    run = admission["run"]
    names = admission["artifacts"]
    token = _environment_value(environment, "GITHUB_TOKEN")
    payload_artifact_id = _expect_environment_identifier(environment, "A1_PAYLOAD_ARTIFACT_ID")
    reader = artifact_reader or _read_github_artifact
    payload_artifact = _validate_uploaded_artifact(
        reader(source["repository"], payload_artifact_id, token),
        artifact_id=payload_artifact_id,
        expected_name=names["payload"],
        current_run_id=run["id"],
    )
    payload_directory = _artifact_root(root) / "payload"
    archive_name = f"netcoredbg-mcp-stateless-preview-win-x64-{preview['version']}.zip"
    manifest_name = f"netcoredbg-mcp-stateless-preview-win-x64-{preview['version']}.manifest.json"
    archive_path = payload_directory / archive_name
    manifest_path = payload_directory / manifest_name
    archive_bytes = _read_regular_bytes(archive_path, "preview archive")
    manifest_bytes = _read_regular_bytes(manifest_path, "preview manifest")
    manifest_contents = _load_manifest(manifest_bytes)
    _validate_preview_manifest(manifest_contents, source["commit"])
    if (
        manifest_contents["version"] != preview["version"]
        or manifest_contents["tag"] != preview["tag"]
    ):
        _refuse("preview manifest does not match admitted preview inputs")
    if manifest_contents["archive"]["name"] != archive_name:
        _refuse("preview manifest archive name is not admitted")
    if (
        len(archive_bytes) != manifest_contents["archive"]["size_bytes"]
        or _sha256_bytes(archive_bytes) != manifest_contents["archive"]["sha256"]
    ):
        _refuse("preview archive does not match its manifest")
    executable_bytes = _read_exact_preview_archive_member(archive_bytes, _PREVIEW_EXECUTABLE)
    if (
        len(executable_bytes) != manifest_contents["executable"]["size_bytes"]
        or _sha256_bytes(executable_bytes) != manifest_contents["executable"]["sha256"]
    ):
        _refuse("preview executable does not match its manifest")
    manifest_file = {
        "name": manifest_name,
        "size_bytes": len(manifest_bytes),
        "sha256": _sha256_bytes(manifest_bytes),
    }

    payload_retention = {
        "configured_days": preview["retention_days"],
        "expires_at": payload_artifact["expires_at"],
    }
    candidate_input = {
        "schema_version": "1.0",
        "candidate": {
            "source": {
                "repository": source["repository"],
                "ref": _CANONICAL_SOURCE_REF,
                "commit": source["commit"],
                "origin_main_target": source["commit"],
            },
            "build": {
                "repository": source["repository"],
                "workflow_path": _TRUSTED_WORKFLOW_PATH,
                "mode": _TRUSTED_BUILD_MODE,
                "event": _TRUSTED_BUILD_EVENT,
                "run_id": run["id"],
                "run_attempt": run["attempt"],
                "ref": _CANONICAL_SOURCE_REF,
                "commit": source["commit"],
                "artifact": {
                    "id": payload_artifact["id"],
                    "name": payload_artifact["name"],
                    "sha256": _sha256_bytes(archive_bytes + manifest_bytes),
                    "retention": payload_retention,
                },
            },
            "preview_manifest": {
                "file": manifest_file,
                "manifest_reference": {
                    "repository": source["repository"],
                    "run_id": run["id"],
                    "artifact_id": payload_artifact["id"],
                    "path": manifest_name,
                    "sha256": manifest_file["sha256"],
                },
                "archive_reference": {
                    "repository": source["repository"],
                    "run_id": run["id"],
                    "artifact_id": payload_artifact["id"],
                    "path": archive_name,
                    "sha256": manifest_contents["archive"]["sha256"],
                },
                "contents": manifest_contents,
            },
            "destination": {
                "provider": "github",
                "repository": source["repository"],
                "tag": preview["tag"],
                "prerelease": True,
            },
        },
    }
    candidate_identity = assemble_candidate_identity(candidate_input)
    release_gate_catalog = resolve_release_gate_catalog(candidate_identity, root)
    records_directory = _artifact_root(root) / "records"
    if records_directory.exists() or records_directory.is_symlink():
        _refuse("sealed record directory already exists")
    _write_bytes_once(
        records_directory / _CANDIDATE_IDENTITY_FILENAME,
        _canonical_json_bytes(candidate_identity, "candidate identity"),
        "candidate identity",
    )
    _write_bytes_once(
        records_directory / _RELEASE_GATE_CATALOG_FILENAME,
        _canonical_json_bytes(release_gate_catalog, "release gate catalog"),
        "release gate catalog",
    )
    return _freeze(
        {
            "candidate_identity": _thaw(candidate_identity),
            "release_gate_catalog": _thaw(release_gate_catalog),
        }
    )


_CONSUMER_PROOF_SCHEMA_VERSION = "1.0"
_CONSUMER_PROOF_CATALOG_ID = "a1-preview-inherited-matrix-v1"
_SAFE_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CREDENTIAL_SHAPED_ID_PATTERN = re.compile(
    r"^(?:gh[pousr]_|github_pat_|glpat-|AKIA|ASIA|xox[baprs]-|sk-[A-Za-z0-9]|Bearer[._-])",
    re.IGNORECASE,
)
_CONSUMER_PROOF_SCENARIOS: tuple[tuple[str, str, str], ...] = (
    ("launch-cli-invalid-root", "launch_cli", "EXPECT_REFUSAL"),
    ("launch-configuration-hostile-roots", "launch_configuration", "EXPECT_SUCCESS"),
    ("contained-fixture-escape", "contained_fixture", "EXPECT_REFUSAL"),
    ("tool-input-invalid", "tool_input", "EXPECT_REFUSAL"),
    ("file-system-unreadable", "file_system", "EXPECT_REFUSAL"),
    ("resources-ceilings", "resources", "EXPECT_REFUSAL"),
    ("protocol-catalog-exclusions", "protocol_catalog", "EXPECT_REFUSAL"),
    ("transport-eof-cancellation", "transport", "EXPECT_SUCCESS"),
    ("valid-discovery-list-call", "valid_journey", "EXPECT_SUCCESS"),
    ("rollback-python-default", "rollback", "EXPECT_SUCCESS"),
)
_EXPECTED_OBSERVED_OUTCOMES = {
    "EXPECT_SUCCESS": "SUCCESS",
    "EXPECT_FAILURE": "FAILURE",
    "EXPECT_REFUSAL": "REFUSAL",
}


def _expect_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        _refuse(f"{name} must be an array")
    return value


def _expect_boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        _refuse(f"{name} must be a boolean")
    return value


def _expect_nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        _refuse(f"{name} must be a non-negative integer")
    return value


def _expect_safe_opaque_id(value: Any, name: str) -> str:
    identifier = _expect_string(value, name)
    if _SAFE_OPAQUE_ID_PATTERN.fullmatch(identifier) is None:
        _refuse(f"{name} must be a bounded opaque identifier")
    if _CREDENTIAL_SHAPED_ID_PATTERN.match(identifier) is not None:
        _refuse(f"{name} must not contain a credential-shaped value")
    return identifier


def _consumer_proof_scenarios() -> list[dict[str, str]]:
    return [
        {
            "scenario_id": scenario_id,
            "surface": surface,
            "documented_outcome": documented_outcome,
        }
        for scenario_id, surface, documented_outcome in _CONSUMER_PROOF_SCENARIOS
    ]


def consumer_proof_scenario_catalog() -> Mapping[str, Any]:
    """Return the closed inherited denial matrix used by retained-byte proof."""

    scenarios = _consumer_proof_scenarios()
    return _freeze(
        {
            "scenario_catalog_id": _CONSUMER_PROOF_CATALOG_ID,
            "scenario_catalog_sha256": _sha256_bytes(
                _canonical_json_bytes(scenarios, "consumer proof scenario catalog")
            ),
            "scenarios": scenarios,
        }
    )


def _validate_consumer_proof_reference(value: Any, name: str) -> Mapping[str, Any]:
    reference = _expect_mapping(
        value,
        name,
        ("repository", "run_id", "artifact", "path", "sha256"),
    )
    _expect_repository(reference["repository"], f"{name} repository")
    _expect_identifier(reference["run_id"], f"{name} run ID")
    _validate_actions_artifact(reference["artifact"])
    _expect_safe_relative_path(reference["path"], f"{name} path")
    _expect_sha256(reference["sha256"], f"{name} hash")
    return reference


def validate_artifact_consumer_proof_reference(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate a closed GitHub artifact file locator used by consumer proof."""

    return _freeze(_thaw(_validate_consumer_proof_reference(value, "consumer proof reference")))


def _validate_release_gate_catalog_for_consumer_proof(
    value: Mapping[str, Any], candidate: Mapping[str, Any]
) -> Mapping[str, Any]:
    catalog_record = _expect_mapping(
        value, "release gate catalog", ("catalog_schema_version", "catalog")
    )
    if catalog_record["catalog_schema_version"] != "1.0":
        _refuse("release gate catalog schema version is invalid")
    catalog = _expect_mapping(
        catalog_record["catalog"],
        "release gate catalog body",
        (
            "producer",
            "source_ref",
            "source_commit",
            "policy_authority_snapshots",
            "gate_descriptors",
            "resolved_at",
        ),
    )
    producer = _expect_mapping(
        catalog["producer"],
        "release gate catalog producer",
        ("helper_path", "operation"),
    )
    if (
        producer["helper_path"] != "scripts/stateless_preview_artifact.py"
        or producer["operation"] != "resolve_release_gate_catalog"
        or catalog["source_ref"] != _CANONICAL_SOURCE_REF
        or catalog["source_commit"] != candidate["source"]["commit"]
    ):
        _refuse("release gate catalog does not bind the candidate")
    _expect_datetime(catalog["resolved_at"], "release gate catalog resolved_at")
    snapshots = _expect_list(
        catalog["policy_authority_snapshots"], "release gate catalog snapshots"
    )
    if len(snapshots) != len(_POLICY_AUTHORITY_PATHS):
        _refuse("release gate catalog snapshot closure is incomplete")
    for expected_path, snapshot in zip(_POLICY_AUTHORITY_PATHS, snapshots):
        item = _expect_mapping(
            snapshot,
            "release gate catalog snapshot",
            ("path", "sha256", "source_commit"),
        )
        if item["path"] != expected_path or item["source_commit"] != candidate["source"]["commit"]:
            _refuse("release gate catalog snapshot does not bind the candidate")
        _expect_sha256(item["sha256"], "release gate catalog snapshot hash")
    descriptors = _expect_list(catalog["gate_descriptors"], "release gate catalog descriptors")
    if descriptors != _fixed_gate_descriptors():
        _refuse("release gate catalog descriptor closure is invalid")
    return catalog_record


def _validate_retained_download_origin(
    value: Any, candidate: Mapping[str, Any]
) -> Mapping[str, Any]:
    origin = _expect_mapping(
        value,
        "retained download origin",
        ("repository", "workflow_path", "run_id", "artifact", "archive_path", "manifest_path"),
    )
    build = candidate["build"]
    manifest = candidate["preview_manifest"]
    if (
        origin["repository"] != build["repository"]
        or origin["workflow_path"] != _TRUSTED_WORKFLOW_PATH
        or origin["run_id"] != build["run_id"]
        or _thaw(origin["artifact"]) != _thaw(build["artifact"])
        or origin["archive_path"] != manifest["archive_reference"]["path"]
        or origin["manifest_path"] != manifest["manifest_reference"]["path"]
    ):
        _refuse("retained download origin does not bind the candidate")
    _expect_safe_relative_path(origin["archive_path"], "retained download origin archive path")
    _expect_safe_relative_path(origin["manifest_path"], "retained download origin manifest path")
    return origin


def _validate_input_identity_results(value: Any, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    results = _expect_mapping(
        value,
        "consumer proof input identity results",
        (
            "archive",
            "manifest",
            "executable",
            "archive_matches_candidate",
            "manifest_matches_candidate",
            "executable_matches_manifest",
            "inherited_verifier_equations_pass",
        ),
    )
    manifest = candidate["preview_manifest"]
    contents = manifest["contents"]
    _validate_file_descriptor(results["archive"], "consumer proof archive")
    _validate_file_descriptor(results["manifest"], "consumer proof manifest")
    _validate_file_descriptor(results["executable"], "consumer proof executable")
    if (
        _thaw(results["archive"]) != _thaw(contents["archive"])
        or _thaw(results["manifest"]) != _thaw(manifest["file"])
        or _thaw(results["executable"]) != _thaw(contents["executable"])
    ):
        _refuse("consumer proof archive, manifest, or executable does not match the candidate")
    for field in (
        "archive_matches_candidate",
        "manifest_matches_candidate",
        "executable_matches_manifest",
        "inherited_verifier_equations_pass",
    ):
        if not _expect_boolean(results[field], f"consumer proof {field}"):
            _refuse("consumer proof input identities are not all verified")
    return results


def _validate_fixture_identity(value: Any) -> Mapping[str, Any]:
    fixture = _expect_mapping(
        value,
        "consumer proof fixture identity",
        ("fixture_id", "fixture_sha256", "scenario_catalog_id", "scenario_catalog_sha256"),
    )
    catalog = consumer_proof_scenario_catalog()
    _expect_safe_opaque_id(fixture["fixture_id"], "consumer proof fixture ID")
    _expect_sha256(fixture["fixture_sha256"], "consumer proof fixture hash")
    if (
        fixture["scenario_catalog_id"] != catalog["scenario_catalog_id"]
        or fixture["scenario_catalog_sha256"] != catalog["scenario_catalog_sha256"]
    ):
        _refuse("consumer proof fixture scenario catalog is not the inherited matrix")
    return fixture


def _validate_scenario_matrix(value: Any) -> Mapping[str, Any]:
    matrix = _expect_mapping(
        value,
        "consumer proof scenario matrix",
        (
            "required_scenario_ids",
            "executed_scenario_ids",
            "required_count",
            "executed_count",
            "missing_scenario_ids",
            "unexpected_scenario_ids",
            "results",
            "outcome",
        ),
    )
    scenarios = _consumer_proof_scenarios()
    required_ids = [scenario["scenario_id"] for scenario in scenarios]
    expected_by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    declared_required = _expect_list(
        matrix["required_scenario_ids"], "consumer proof required scenario IDs"
    )
    declared_executed = _expect_list(
        matrix["executed_scenario_ids"], "consumer proof executed scenario IDs"
    )
    missing = _expect_list(matrix["missing_scenario_ids"], "consumer proof missing scenario IDs")
    unexpected = _expect_list(
        matrix["unexpected_scenario_ids"], "consumer proof unexpected scenario IDs"
    )
    results = _expect_list(matrix["results"], "consumer proof scenario results")
    if (
        declared_required != required_ids
        or declared_executed != required_ids
        or missing != []
        or unexpected != []
        or _expect_positive_integer(matrix["required_count"], "consumer proof required count")
        != len(required_ids)
        or _expect_nonnegative_integer(matrix["executed_count"], "consumer proof executed count")
        != len(required_ids)
        or len(results) != len(required_ids)
        or matrix["outcome"] != "PASS"
    ):
        _refuse("consumer proof scenario matrix is incomplete")
    observed_ids: list[str] = []
    for result in results:
        item = _expect_mapping(
            result,
            "consumer proof scenario result",
            (
                "scenario_id",
                "surface",
                "documented_outcome",
                "observed_outcome",
                "status",
                "no_partial_output",
                "no_unintended_side_effect",
            ),
        )
        scenario_id = _expect_safe_opaque_id(item["scenario_id"], "consumer proof scenario ID")
        expected = expected_by_id.get(scenario_id)
        if (
            expected is None
            or item["surface"] != expected["surface"]
            or item["documented_outcome"] != expected["documented_outcome"]
            or item["observed_outcome"]
            != _EXPECTED_OBSERVED_OUTCOMES[expected["documented_outcome"]]
            or item["status"] != "PASS"
            or not _expect_boolean(item["no_partial_output"], "consumer proof no_partial_output")
            or not _expect_boolean(
                item["no_unintended_side_effect"], "consumer proof no_unintended_side_effect"
            )
        ):
            _refuse("consumer proof scenario matrix contains an invalid result")
        observed_ids.append(scenario_id)
    if observed_ids != required_ids:
        _refuse("consumer proof scenario matrix has duplicate, missing, or reordered results")
    return matrix


def _validate_runtime_results(value: Any) -> Mapping[str, Any]:
    runtime = _expect_mapping(
        value,
        "consumer proof runtime results",
        (
            "explicit_project_argument",
            "catalog",
            "catalog_is_closed",
            "valid_journey_passed",
            "stdout_jsonrpc_only",
            "clean_eof",
        ),
    )
    if (
        not _expect_boolean(runtime["explicit_project_argument"], "consumer proof explicit project")
        or _expect_list(runtime["catalog"], "consumer proof catalog") != ["find_code_symbol"]
        or not _expect_boolean(runtime["catalog_is_closed"], "consumer proof closed catalog")
        or not _expect_boolean(runtime["valid_journey_passed"], "consumer proof valid journey")
        or not _expect_boolean(runtime["stdout_jsonrpc_only"], "consumer proof stdout purity")
    ):
        _refuse("consumer proof runtime results are not a passing one-tool journey")
    eof = _expect_mapping(
        runtime["clean_eof"],
        "consumer proof clean EOF",
        (
            "stdin_closed",
            "exited_cleanly",
            "cancellation_result_emitted",
            "state_retained_after_exit",
        ),
    )
    if (
        not _expect_boolean(eof["stdin_closed"], "consumer proof EOF stdin")
        or not _expect_boolean(eof["exited_cleanly"], "consumer proof EOF exit")
        or _expect_boolean(eof["cancellation_result_emitted"], "consumer proof EOF cancellation")
        or _expect_boolean(eof["state_retained_after_exit"], "consumer proof EOF state")
    ):
        _refuse("consumer proof clean EOF is invalid")
    return runtime


def _validate_python_rollback(value: Any) -> Mapping[str, Any]:
    rollback = _expect_mapping(
        value,
        "consumer proof Python rollback",
        (
            "only_preview_selection_removed",
            "python_package_reinstalled",
            "python_package_replaced",
            "console_entrypoint_changed",
            "default_selector_changed",
            "legacy_journey_outcome",
        ),
    )
    if (
        not _expect_boolean(
            rollback["only_preview_selection_removed"], "consumer proof rollback selection"
        )
        or _expect_boolean(
            rollback["python_package_reinstalled"], "consumer proof rollback reinstall"
        )
        or _expect_boolean(
            rollback["python_package_replaced"], "consumer proof rollback replacement"
        )
        or _expect_boolean(
            rollback["console_entrypoint_changed"], "consumer proof rollback entrypoint"
        )
        or _expect_boolean(rollback["default_selector_changed"], "consumer proof rollback selector")
        or rollback["legacy_journey_outcome"] != "PRODUCT_WORKS"
    ):
        _refuse("consumer proof Python rollback is invalid")
    return rollback


def seal_artifact_consumer_proof(
    receipt: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate and freeze a passing retained-artifact Consumer Proof Receipt."""

    value = _thaw(receipt)
    record = _expect_mapping(
        value,
        "consumer proof receipt",
        (
            "receipt_schema_version",
            "receipt_id",
            "candidate_identity_record",
            "candidate",
            "proof_stage",
            "download_origin",
            "input_identity_results",
            "fixture_identity",
            "scenario_matrix",
            "runtime_results",
            "python_rollback_result",
            "outcome",
            "recorded_at",
        ),
    )
    if record["receipt_schema_version"] != _CONSUMER_PROOF_SCHEMA_VERSION:
        _refuse("consumer proof receipt schema version is invalid")
    _expect_safe_opaque_id(record["receipt_id"], "consumer proof receipt ID")
    if record["proof_stage"] != "retained_artifact" or record["outcome"] != "PASS":
        _refuse("consumer proof receipt must be a passing retained artifact proof")
    _expect_datetime(record["recorded_at"], "consumer proof recorded_at")
    supplied_identity_reference = _validate_consumer_proof_reference(
        record["candidate_identity_record"], "consumer proof candidate identity reference"
    )
    expected_identity_reference = _validate_consumer_proof_reference(
        candidate_identity_reference, "candidate identity reference"
    )
    if _thaw(supplied_identity_reference) != _thaw(expected_identity_reference):
        _refuse("consumer proof candidate identity reference does not match downloaded bytes")
    if not isinstance(candidate_identity_bytes, bytes):
        _refuse("candidate identity bytes are unavailable")
    if _sha256_bytes(candidate_identity_bytes) != expected_identity_reference["sha256"]:
        _refuse("candidate identity bytes do not match the downloaded reference")
    decoded_identity = _load_json_object(candidate_identity_bytes, "candidate identity")
    validated_identity = _validate_candidate_identity(decoded_identity)
    if _thaw(validated_identity) != _thaw(_validate_candidate_identity(candidate_identity)):
        _refuse("candidate identity bytes do not match the supplied candidate")
    candidate = validated_identity["candidate"]
    if _thaw(record["candidate"]) != _thaw(candidate):
        _refuse("consumer proof candidate does not match the downloaded identity")
    _validate_release_gate_catalog_for_consumer_proof(release_gate_catalog, candidate)
    _validate_retained_download_origin(record["download_origin"], candidate)
    _validate_input_identity_results(record["input_identity_results"], candidate)
    _validate_fixture_identity(record["fixture_identity"])
    _validate_scenario_matrix(record["scenario_matrix"])
    _validate_runtime_results(record["runtime_results"])
    _validate_python_rollback(record["python_rollback_result"])
    return _freeze(value)


_S2_S3_REVIEW_LENSES = (
    "dependency-cve",
    "dotnet-unsafe-pattern",
    "owasp-input-output-path",
    "attack-surface",
    "secret-asset-manifest",
    "path-process-fail-closed",
    "exploitability",
)
_REVIEW_OBSERVED_RESULTS = ("SUCCESS", "FAILURE", "REFUSAL", "HARNESS_FAILURE", "EVIDENCE_REJECTED")


def _review_timestamp(value: Any, name: str) -> datetime:
    text = _expect_string(value, name)
    _expect_datetime(text[:-1] + "Z" if text.endswith("z") else text, name)
    if (
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
            text,
        )
        is None
    ):
        _refuse(f"{name} must be a closed date-time value")
    return datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text)


def _review_record_reference(value: Any, name: str) -> Mapping[str, Any]:
    reference = _validate_artifact_file_reference(value, name)
    if len(reference["repository"]) > 200 or len(reference["path"]) > 1024:
        _refuse(f"{name} exceeds the record locator bounds")
    return reference


def _verified_review_candidate(
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = _validate_consumer_proof_reference(
        candidate_identity_reference, "review candidate identity reference"
    )
    if (
        not isinstance(candidate_identity_bytes, bytes)
        or _sha256_bytes(candidate_identity_bytes) != reference["sha256"]
    ):
        _refuse("review candidate identity bytes do not match the downloaded reference")
    identity = _validate_candidate_identity(
        _load_json_object(candidate_identity_bytes, "review candidate identity")
    )
    if identity != _validate_candidate_identity(candidate_identity):
        _refuse("review candidate identity bytes do not match the supplied candidate")
    candidate = identity["candidate"]
    if reference["repository"] != candidate["source"]["repository"]:
        _refuse("review candidate identity reference has a different repository")
    compact_reference = {
        "repository": reference["repository"],
        "run_id": reference["run_id"],
        "artifact_id": reference["artifact"]["id"],
        "path": reference["path"],
        "sha256": reference["sha256"],
    }
    _review_record_reference(compact_reference, "review candidate identity reference")
    now = datetime.now(timezone.utc)
    for artifact in (reference["artifact"], candidate["build"]["artifact"]):
        if _review_timestamp(artifact["retention"]["expires_at"], "artifact expiry") <= now:
            _refuse("review candidate artifact retention has expired")
    return identity, compact_reference


def _validate_review_candidate_binding(
    record: Mapping[str, Any], candidate: Mapping[str, Any], reference: Mapping[str, Any]
) -> None:
    observed_reference = _review_record_reference(
        record["candidate_identity_record"], "review candidate identity record"
    )
    if (
        observed_reference != reference
        or _expect_sha256(record["candidate_identity_record_sha256"], "review candidate hash")
        != reference["sha256"]
        or record["candidate"] != candidate
    ):
        _refuse("review evidence does not bind the exact downloaded candidate")


def _validate_review_evidence(record: Mapping[str, Any], *, independent_pr: bool) -> int:
    reviewer = _expect_mapping(
        record["reviewer_provenance"],
        "reviewer provenance",
        (
            "reviewer_principal_id",
            "reviewed_author_principal_id" if independent_pr else "reviewer_role",
            "independence_basis",
        ),
    )
    reviewer_id = _expect_safe_opaque_id(reviewer["reviewer_principal_id"], "reviewer principal")
    producer = _expect_mapping(
        record["producer_provenance"], "review producer", ("producer_principal_id", "operation")
    )
    producer_id = _expect_safe_opaque_id(producer["producer_principal_id"], "producer principal")
    operation = "seal_independent_pr_review" if independent_pr else "seal_s2_s3_lens_result"
    if producer["operation"] != operation or reviewer_id == producer_id:
        _refuse("reviewer and producer must have distinct, typed provenance")
    if independent_pr:
        author_id = _expect_safe_opaque_id(
            reviewer["reviewed_author_principal_id"], "reviewed author principal"
        )
        if (
            reviewer["independence_basis"] != "DISTINCT_REVIEWER_AND_AUTHOR"
            or reviewer_id == author_id
        ):
            _refuse("independent PR reviewer must differ from the reviewed author")
    elif reviewer["independence_basis"] != "DISTINCT_REVIEWER_AND_PRODUCER" or reviewer[
        "reviewer_role"
    ] not in ("SECURITY_REVIEWER", "CODE_WORKFLOW_REVIEWER"):
        _refuse("lens reviewer provenance is invalid")

    checks = _expect_list(record["checks"], "review checks")
    required = len(checks)
    if not 1 <= required <= 128:
        _refuse("review checks must contain between 1 and 128 required records")
    check_ids: set[str] = set()
    completed = 0
    for value in checks:
        check = _expect_mapping(value, "review check", ("check_id", "result", "status"))
        identifier = _expect_safe_opaque_id(check["check_id"], "review check ID")
        if identifier in check_ids:
            _refuse("review checks contain duplicate check IDs")
        check_ids.add(identifier)
        result = _expect_mapping(check["result"], "review check result", ("expected", "observed"))
        expected = _expect_string(result["expected"], "review check expected result")
        observed = _expect_string(result["observed"], "review check observed result")
        status = _expect_string(check["status"], "review check status")
        if (
            expected not in _EXPECTED_OBSERVED_OUTCOMES
            or observed not in _REVIEW_OBSERVED_RESULTS
            or status not in ("PASS", "FAIL", "REFUSED", "INCOMPLETE")
        ):
            _refuse("review check contains an unsupported closed value")
        completed += status == "PASS" and observed == _EXPECTED_OBSERVED_OUTCOMES[expected]

    denominator = _expect_mapping(
        record["denominator"],
        "review denominator",
        ("required_evidence_count", "completed_evidence_count"),
    )
    supplied_required = _expect_positive_integer(
        denominator["required_evidence_count"], "review required count"
    )
    supplied_completed = _expect_nonnegative_integer(
        denominator["completed_evidence_count"], "review completed count"
    )
    if supplied_required != required or supplied_completed != completed or completed != required:
        _refuse("review evidence denominator does not match its complete checks")
    findings = _expect_mapping(
        record["findings"], "review findings", ("records", "unresolved_high_severity_count")
    )
    rows = _expect_list(findings["records"], "review finding records")
    if len(rows) > 128:
        _refuse("review finding records exceed their bounds")
    identifiers: set[str] = set()
    unresolved_high = 0
    for value in rows:
        finding = _expect_mapping(
            value, "review finding", ("finding_id", "severity", "disposition", "observed_result")
        )
        identifier = _expect_safe_opaque_id(finding["finding_id"], "review finding ID")
        if identifier in identifiers:
            _refuse("review finding records contain duplicate evidence")
        identifiers.add(identifier)
        if (
            finding["severity"] not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
            or finding["disposition"] not in ("UNRESOLVED", "RESOLVED", "NOT_APPLICABLE")
            or finding["observed_result"] not in _REVIEW_OBSERVED_RESULTS
        ):
            _refuse("review finding contains an unsupported closed value")
        unresolved_high += (
            finding["severity"] in ("CRITICAL", "HIGH") and finding["disposition"] == "UNRESOLVED"
        )
    supplied_high = _expect_nonnegative_integer(
        findings["unresolved_high_severity_count"], "review unresolved high count"
    )
    if supplied_high != unresolved_high or unresolved_high != 0:
        _refuse("review findings do not prove zero unresolved high-severity findings")
    result = _expect_mapping(record["result"], "review result", ("expected", "observed"))
    expected = _expect_string(result["expected"], "review expected result")
    if (
        expected not in _EXPECTED_OBSERVED_OUTCOMES
        or result["observed"] != _EXPECTED_OBSERVED_OUTCOMES[expected]
        or result["observed"] != "SUCCESS"
        or record["outcome"] != "PASS"
    ):
        _refuse("review outcome does not match its passing result")
    _review_timestamp(record["recorded_at"], "review recorded_at")
    return unresolved_high


def _validate_s2_s3_lens_result(
    value: Any, candidate: Mapping[str, Any], reference: Mapping[str, Any]
) -> Mapping[str, Any]:
    record = _expect_mapping(
        value,
        "S2/S3 lens result",
        (
            "lens",
            "lens_order",
            "candidate_identity_record",
            "candidate_identity_record_sha256",
            "candidate",
            "reviewer_provenance",
            "producer_provenance",
            "denominator",
            "checks",
            "findings",
            "result",
            "outcome",
            "recorded_at",
        ),
    )
    order = _expect_positive_integer(record["lens_order"], "review lens order")
    if order > len(_S2_S3_REVIEW_LENSES) or record["lens"] != _S2_S3_REVIEW_LENSES[order - 1]:
        _refuse("review lens does not match its fixed order")
    _validate_review_candidate_binding(record, candidate, reference)
    _validate_review_evidence(record, independent_pr=False)
    return record


def seal_s2_s3_lens_result(
    lens_result: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Admit one independent, exact-candidate S2/S3 lens without changing its claims."""

    identity, reference = _verified_review_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    record = _validate_s2_s3_lens_result(_thaw(lens_result), identity["candidate"], reference)
    return _freeze(record)


def seal_s2_s3_review_aggregate(
    aggregate: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Recompute the fixed seven-lens aggregate from independently admitted lens results."""

    identity, reference = _verified_review_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    record = _expect_mapping(
        _thaw(aggregate),
        "S2/S3 review aggregate",
        (
            "aggregate_schema_version",
            "aggregate_id",
            "aggregate_producer",
            "candidate_identity_record",
            "candidate_identity_record_sha256",
            "candidate",
            "lens_results",
            "coverage_summary",
            "outcome",
            "sealed_at",
        ),
    )
    if record["aggregate_schema_version"] != "1.0":
        _refuse("S2/S3 aggregate schema version is unsupported")
    _expect_safe_opaque_id(record["aggregate_id"], "S2/S3 aggregate ID")
    producer = _expect_mapping(
        record["aggregate_producer"], "S2/S3 aggregate producer", ("helper_path", "operation")
    )
    if producer != {
        "helper_path": "scripts/stateless_preview_artifact.py",
        "operation": "seal_s2_s3_review_aggregate",
    }:
        _refuse("S2/S3 aggregate producer is invalid")
    _validate_review_candidate_binding(record, identity["candidate"], reference)
    sealed_at = _review_timestamp(record["sealed_at"], "S2/S3 aggregate sealed_at")
    lenses = _expect_list(record["lens_results"], "S2/S3 lens results")
    if len(lenses) != len(_S2_S3_REVIEW_LENSES):
        _refuse("S2/S3 aggregate does not contain exactly seven required lenses")
    completed = 0
    unresolved_high = 0
    for order, (lens_id, value) in enumerate(zip(_S2_S3_REVIEW_LENSES, lenses), start=1):
        lens = _validate_s2_s3_lens_result(value, identity["candidate"], reference)
        if lens["lens"] != lens_id or lens["lens_order"] != order:
            _refuse("S2/S3 aggregate lens results are missing, duplicate, or out of order")
        if _review_timestamp(lens["recorded_at"], "lens recorded_at") > sealed_at:
            _refuse("S2/S3 aggregate was sealed before its lens evidence")
        completed += 1
        unresolved_high += sum(
            finding["severity"] in ("CRITICAL", "HIGH") and finding["disposition"] == "UNRESOLVED"
            for finding in lens["findings"]["records"]
        )
    coverage = _expect_mapping(
        record["coverage_summary"],
        "S2/S3 coverage summary",
        ("required_lens_count", "completed_lens_count", "unresolved_high_severity_count"),
    )
    derived_coverage = {
        "required_lens_count": len(_S2_S3_REVIEW_LENSES),
        "completed_lens_count": completed,
        "unresolved_high_severity_count": unresolved_high,
    }
    for field, expected in derived_coverage.items():
        if _expect_nonnegative_integer(coverage[field], f"S2/S3 {field}") != expected:
            _refuse("S2/S3 aggregate coverage does not match its sealed lens results")
    if record["outcome"] != "PASS":
        _refuse("S2/S3 aggregate outcome does not match its passing lens results")
    return _freeze(record)


def seal_independent_pr_review(
    receipt: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Admit the distinct PR review with independent provenance and check-derived coverage."""

    identity, reference = _verified_review_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    record = _expect_mapping(
        _thaw(receipt),
        "independent PR review",
        (
            "receipt_schema_version",
            "receipt_id",
            "candidate_identity_record",
            "candidate_identity_record_sha256",
            "candidate",
            "review_target",
            "reviewer_provenance",
            "producer_provenance",
            "denominator",
            "checks",
            "findings",
            "result",
            "outcome",
            "recorded_at",
        ),
    )
    if record["receipt_schema_version"] != "1.0":
        _refuse("independent PR review schema version is unsupported")
    _expect_safe_opaque_id(record["receipt_id"], "independent PR review ID")
    _validate_review_candidate_binding(record, identity["candidate"], reference)
    target = _expect_mapping(
        record["review_target"],
        "PR review target",
        ("repository", "pull_request_id", "reviewed_commit", "base_ref"),
    )
    _expect_identifier(target["pull_request_id"], "reviewed pull request ID")
    if (
        target["repository"] != identity["candidate"]["source"]["repository"]
        or target["reviewed_commit"] != identity["candidate"]["source"]["commit"]
        or target["base_ref"] != _CANONICAL_SOURCE_REF
    ):
        _refuse("independent PR review target does not match the canonical candidate")
    _validate_review_evidence(record, independent_pr=True)
    return _freeze(record)


def _validate_stage_consumer_proof(
    proof: Mapping[str, Any],
    *,
    remote: bool,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not remote:
        return seal_artifact_consumer_proof(
            proof,
            candidate_identity=candidate_identity,
            candidate_identity_bytes=candidate_identity_bytes,
            candidate_identity_reference=candidate_identity_reference,
            release_gate_catalog=release_gate_catalog,
        )
    record = _expect_mapping(
        proof,
        "remote consumer proof",
        (
            "receipt_schema_version",
            "receipt_id",
            "candidate_identity_record",
            "candidate",
            "proof_stage",
            "download_origin",
            "input_identity_results",
            "fixture_identity",
            "scenario_matrix",
            "runtime_results",
            "python_rollback_result",
            "outcome",
            "recorded_at",
        ),
    )
    _expect_safe_opaque_id(record["receipt_id"], "remote consumer proof ID")
    _review_timestamp(record["recorded_at"], "remote consumer proof recorded_at")
    identity_reference = _validate_consumer_proof_reference(
        record["candidate_identity_record"], "remote consumer proof identity reference"
    )
    candidate = candidate_identity["candidate"]
    if (
        record["receipt_schema_version"] != _CONSUMER_PROOF_SCHEMA_VERSION
        or record["proof_stage"] != "remote_release"
        or record["outcome"] != "PASS"
        or identity_reference != candidate_identity_reference
        or record["candidate"] != candidate
    ):
        _refuse("remote consumer proof does not bind the exact passing candidate")
    origin = _expect_mapping(
        record["download_origin"],
        "remote consumer proof origin",
        ("repository", "release_id", "tag", "prerelease", "archive_asset", "manifest_asset"),
    )
    _expect_identifier(origin["release_id"], "remote consumer proof release ID")
    if (
        origin["repository"] != candidate["source"]["repository"]
        or origin["tag"] != candidate["destination"]["tag"]
        or origin["prerelease"] is not True
    ):
        _refuse("remote consumer proof origin does not match the prerelease candidate")
    manifest = candidate["preview_manifest"]
    for field, expected in (
        ("archive_asset", manifest["contents"]["archive"]),
        ("manifest_asset", manifest["file"]),
    ):
        asset = _expect_mapping(
            origin[field], "remote consumer proof asset", ("id", "name", "size_bytes", "sha256")
        )
        _expect_identifier(asset["id"], "remote consumer proof asset ID")
        descriptor = {key: asset[key] for key in ("name", "size_bytes", "sha256")}
        _validate_file_descriptor(descriptor, "remote consumer proof asset")
        if descriptor != expected:
            _refuse("remote consumer proof asset bytes do not match the candidate")
    if origin["archive_asset"]["id"] == origin["manifest_asset"]["id"]:
        _refuse("remote consumer proof asset identities are not distinct")
    _validate_input_identity_results(record["input_identity_results"], candidate)
    _validate_fixture_identity(record["fixture_identity"])
    _validate_scenario_matrix(record["scenario_matrix"])
    _validate_runtime_results(record["runtime_results"])
    _validate_python_rollback(record["python_rollback_result"])
    return record


def _resolve_stage_record(
    reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
) -> dict[str, Any]:
    validated_reference = _review_record_reference(reference, "stage evidence record reference")
    try:
        raw = record_resolver(_freeze(_thaw(validated_reference)))
    except (OSError, ValueError, KeyError):
        _refuse("stage evidence record is unavailable")
    if not isinstance(raw, bytes) or _sha256_bytes(raw) != validated_reference["sha256"]:
        _refuse("stage evidence record raw bytes do not match their reference")
    return _load_json_object(raw, "stage evidence record")


def seal_stage_gate_evidence(
    evidence: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
    release_gate_catalog_bytes: bytes,
    release_gate_catalog_reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
    repository_root: str | PathLike[str],
) -> Mapping[str, Any]:
    """Seal only the requested catalog stage after revalidating its exact raw evidence."""

    identity, identity_reference = _verified_review_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    candidate = identity["candidate"]
    catalog_reference = _review_record_reference(
        release_gate_catalog_reference, "stage release gate catalog reference"
    )
    if (
        catalog_reference["repository"] != candidate["source"]["repository"]
        or not isinstance(release_gate_catalog_bytes, bytes)
        or _sha256_bytes(release_gate_catalog_bytes) != catalog_reference["sha256"]
    ):
        _refuse("stage release gate catalog bytes do not match their reference")
    catalog_document = _load_json_object(release_gate_catalog_bytes, "stage release gate catalog")
    if catalog_document != _thaw(release_gate_catalog):
        _refuse("stage release gate catalog bytes do not match the supplied catalog")
    _validate_release_gate_catalog_for_consumer_proof(catalog_document, candidate)
    root = _resolve_repository_root(repository_root)
    for snapshot in catalog_document["catalog"]["policy_authority_snapshots"]:
        actual_hash = _sha256_bytes(
            _read_tracked_authority(root, candidate["source"]["commit"], snapshot["path"])
        )
        if snapshot["sha256"] != actual_hash:
            _refuse("stage release gate catalog policy snapshot differs from exact source bytes")

    record = _expect_mapping(
        _thaw(evidence),
        "stage gate evidence",
        (
            "evidence_schema_version",
            "evidence_set_id",
            "producer",
            "release_gate_catalog",
            "candidate_identity_record",
            "candidate_identity_record_sha256",
            "candidate",
            "source_ref",
            "source_commit",
            "stage",
            "gate_evidence",
            "outcome",
            "sealed_at",
        ),
    )
    if record["evidence_schema_version"] != "1.0":
        _refuse("stage evidence schema version is unsupported")
    _expect_safe_opaque_id(record["evidence_set_id"], "stage evidence set ID")
    producer = _expect_mapping(
        record["producer"], "stage evidence producer", ("helper_path", "operation")
    )
    if producer != {
        "helper_path": "scripts/stateless_preview_artifact.py",
        "operation": "seal_stage_gate_evidence",
    }:
        _refuse("stage evidence producer is invalid")
    _validate_review_candidate_binding(record, candidate, identity_reference)
    if (
        _review_record_reference(record["release_gate_catalog"], "stage catalog reference")
        != catalog_reference
        or record["source_ref"] != _CANONICAL_SOURCE_REF
        or record["source_commit"] != candidate["source"]["commit"]
    ):
        _refuse("stage evidence catalog or canonical source does not match the candidate")
    stage = _expect_string(record["stage"], "stage evidence stage")
    if stage not in ("pre-decision", "pre-publication", "post-publication"):
        _refuse("stage evidence stage is unsupported")
    sealed_at = _review_timestamp(record["sealed_at"], "stage evidence sealed_at")
    if (
        _review_timestamp(catalog_document["catalog"]["resolved_at"], "catalog resolved_at")
        > sealed_at
    ):
        _refuse("stage evidence was sealed before its catalog")
    expected_descriptors = [
        descriptor for descriptor in _fixed_gate_descriptors() if descriptor["stage"] == stage
    ]
    entries = _expect_list(record["gate_evidence"], "stage gate evidence entries")
    if len(entries) != len(expected_descriptors):
        _refuse("stage evidence does not contain its exact catalog subset")
    if not callable(record_resolver):
        _refuse("stage evidence record resolver is unavailable")
    outcomes: list[str] = []
    review_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
    }
    for descriptor, value in zip(expected_descriptors, entries):
        entry = _expect_mapping(
            value,
            "stage gate evidence entry",
            (
                "stage",
                "gate_id",
                "record_type",
                "authority_rules",
                "evidence_requirements",
                "record_reference",
                "candidate_identity_record_sha256",
                "source_commit",
                "outcome",
            ),
        )
        if any(entry[field] != expected for field, expected in descriptor.items()):
            _refuse("stage evidence descriptor subset is changed, duplicate, or out of order")
        if (
            _expect_sha256(entry["candidate_identity_record_sha256"], "stage gate candidate hash")
            != identity_reference["sha256"]
            or entry["source_commit"] != candidate["source"]["commit"]
        ):
            _refuse("stage gate evidence entry does not bind the exact candidate source")
        raw_record = _resolve_stage_record(entry["record_reference"], record_resolver)
        if descriptor["record_type"] == "s2-s3-seven-lens-aggregate":
            admitted = seal_s2_s3_review_aggregate(raw_record, **review_binding)
            record_time = admitted["sealed_at"]
        elif descriptor["record_type"] == "independent-pr-review":
            admitted = seal_independent_pr_review(raw_record, **review_binding)
            record_time = admitted["recorded_at"]
        else:
            admitted = _validate_stage_consumer_proof(
                raw_record,
                remote=stage == "post-publication",
                release_gate_catalog=catalog_document,
                **review_binding,
            )
            record_time = admitted["recorded_at"]
        if _review_timestamp(record_time, "stage evidence record time") > sealed_at:
            _refuse("stage evidence was sealed before its required proof")
        if entry["outcome"] != admitted["outcome"]:
            _refuse("stage gate outcome does not match its resolved typed record")
        outcomes.append(admitted["outcome"])
    # The empty pre-publication subset is not review or publication authorization.
    derived_outcome = "PASS" if all(outcome == "PASS" for outcome in outcomes) else "FAIL"
    if record["outcome"] != derived_outcome:
        _refuse("stage outcome does not match its exact resolved catalog subset")
    return _freeze(record)


def seal_promotion_decision(
    decision: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
    release_gate_catalog_bytes: bytes,
    release_gate_catalog_reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
    repository_root: str | PathLike[str],
) -> Mapping[str, Any]:
    """Seal an exact-candidate decision record, never a live promotion authorization."""
    identity, identity_reference = _verified_review_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    record = _expect_mapping(
        _thaw(decision),
        "promotion decision",
        (
            "schema_version",
            "candidate_identity_record",
            "candidate",
            "evidence",
            "decision",
            "promotion_attempt",
            "decided_at",
        ),
    )
    if record["schema_version"] != "1.0" or record["decision"] not in ("APPROVE", "DECLINE"):
        _refuse("promotion decision schema version or decision is unsupported")
    _validate_review_candidate_binding(
        {**record, "candidate_identity_record_sha256": identity_reference["sha256"]},
        identity["candidate"],
        identity_reference,
    )
    authorization = _expect_mapping(
        record["promotion_attempt"],
        "decision authorization template",
        ("decision_author", "authorized_dispatcher"),
    )
    for role in ("decision_author", "authorized_dispatcher"):
        actor = _expect_mapping(authorization[role], role, ("id", "login"))
        _expect_identifier(actor["id"], f"{role} ID")
        login = _expect_pattern(actor["login"], f"{role} login", _GITHUB_ACTOR_LOGIN_PATTERN)
        if len(login) > 255:
            _refuse(f"{role} login exceeds its bound")
    evidence = _expect_mapping(
        record["evidence"], "decision evidence", ("pre_decision_stage_evidence",)
    )
    summary_fields = (
        "candidate_identity_record_sha256",
        "source_ref",
        "source_commit",
        "stage",
        "outcome",
    )
    summary = _expect_mapping(
        evidence["pre_decision_stage_evidence"],
        "decision pre-decision stage evidence",
        ("record_reference", *summary_fields),
    )
    if not callable(record_resolver):
        _refuse("decision stage record resolver is unavailable")
    stage = seal_stage_gate_evidence(
        _resolve_stage_record(summary["record_reference"], record_resolver),
        candidate_identity=identity,
        candidate_identity_bytes=candidate_identity_bytes,
        candidate_identity_reference=candidate_identity_reference,
        release_gate_catalog=release_gate_catalog,
        release_gate_catalog_bytes=release_gate_catalog_bytes,
        release_gate_catalog_reference=release_gate_catalog_reference,
        record_resolver=record_resolver,
        repository_root=repository_root,
    )
    if (
        stage["stage"] != "pre-decision"
        or stage["outcome"] != "PASS"
        or any(summary[field] != stage[field] for field in summary_fields)
    ):
        _refuse("promotion decision does not bind its exact passing pre-decision stage")
    if _review_timestamp(record["decided_at"], "decision decided_at") < _review_timestamp(
        stage["sealed_at"], "pre-decision stage sealed_at"
    ):
        _refuse("promotion decision predates its sealed pre-decision evidence")
    return _freeze(record)


def _write_admission_outputs(admission: Mapping[str, Any], environment: Mapping[str, str]) -> None:
    output_path = _environment_value(environment, "GITHUB_OUTPUT")
    preview = admission["preview"]
    artifacts = admission["artifacts"]
    values = {
        "preview_version": preview["version"],
        "preview_tag": preview["tag"],
        "retention_days": str(preview["retention_days"]),
        "payload_artifact_name": artifacts["payload"],
        "candidate_identity_artifact_name": artifacts["candidate_identity"],
        "release_gate_catalog_artifact_name": artifacts["release_gate_catalog"],
    }
    try:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
    except OSError:
        _refuse("GitHub Actions output file is unavailable")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "admit-build",
        "prepare-payload",
        "seal-build-records",
    ):
        commands.add_parser(command)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command == "admit-build":
            _write_admission_outputs(admit_build(Path.cwd(), os.environ), os.environ)
        elif arguments.command == "prepare-payload":
            prepare_preview_payload(Path.cwd(), os.environ)
        elif arguments.command == "seal-build-records":
            seal_build_records(Path.cwd(), os.environ)
        else:
            raise AssertionError("unreachable")
    except ValueError:
        print("STATELESS_PREVIEW_ARTIFACT_REFUSED", file=sys.stderr)
        return 1
    print("STATELESS_PREVIEW_ARTIFACT_READY")
    return 0


__all__ = [
    "admit_build",
    "assemble_candidate_identity",
    "consumer_proof_scenario_catalog",
    "main",
    "parse_args",
    "prepare_preview_payload",
    "resolve_release_gate_catalog",
    "seal_artifact_consumer_proof",
    "seal_build_records",
    "seal_independent_pr_review",
    "seal_promotion_decision",
    "seal_s2_s3_lens_result",
    "seal_s2_s3_review_aggregate",
    "seal_stage_gate_evidence",
    "validate_artifact_consumer_proof_reference",
    "verify_and_extract_retained_artifact",
    "verify_retained_artifact",
    "verify_retained_artifact_inputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
