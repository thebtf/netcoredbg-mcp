"""Fail-closed identity, shared transport, and retained-byte helpers for the A1 preview artifact."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

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


_EVIDENCE_BUNDLE_MEMBERS = (
    "consumer-proof.json",
    "s2-s3-review.json",
    "independent-pr-review.json",
)
_EVIDENCE_BUNDLE_MAX_ENCODED_CHARACTERS = 60_000
_EVIDENCE_BUNDLE_MAX_ZIP_BYTES = 45_000
_EVIDENCE_BUNDLE_MAX_RECORD_BYTES = 128 * 1024
_EVIDENCE_BUNDLE_MAX_TOTAL_BYTES = 256 * 1024


def decode_evidence_bundle(encoded: str, expected_sha256: Mapping[str, str]) -> Mapping[str, bytes]:
    """Decode original evidence bytes; semantic admission remains with the sealers."""
    return _decode_evidence_bundle(encoded, expected_sha256, _EVIDENCE_BUNDLE_MEMBERS)


def _decode_evidence_bundle(
    encoded: str, expected_sha256: Mapping[str, str], member_names: tuple[str, ...]
) -> Mapping[str, bytes]:
    encoded = _expect_string(encoded, "evidence bundle")
    if len(encoded) > _EVIDENCE_BUNDLE_MAX_ENCODED_CHARACTERS:
        _refuse("evidence bundle exceeds the encoded size limit")
    expected = _expect_mapping(expected_sha256, "evidence bundle hashes", member_names)
    hashes = {
        name: _expect_sha256(expected[name], "evidence bundle record hash") for name in member_names
    }
    try:
        raw_zip = base64.b64decode(encoded, validate=True)
    except ValueError:
        _refuse("evidence bundle must contain strict base64")
    if base64.b64encode(raw_zip).decode("ascii") != encoded:
        _refuse("evidence bundle must contain canonical base64")
    if len(raw_zip) > _EVIDENCE_BUNDLE_MAX_ZIP_BYTES:
        _refuse("evidence bundle exceeds the ZIP size limit")

    records: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            members = archive.infolist()
            if len(members) != len(member_names) or {entry.filename for entry in members} != set(
                member_names
            ):
                _refuse("evidence bundle must contain exactly its fixed records")
            declared_total = 0
            payload_ranges: dict[str, tuple[int, int]] = {}
            for entry in members:
                if not 0 <= entry.header_offset <= len(raw_zip) - 30:
                    _refuse("evidence bundle local ZIP header is outside the archive")
                header = struct.unpack_from("<4s5H3I2H", raw_zip, entry.header_offset)
                if (
                    header[0] != b"PK\x03\x04"
                    or header[2] != entry.flag_bits
                    or header[3] != entry.compress_type
                ):
                    _refuse("evidence bundle local ZIP header disagrees with its directory")
                if header[7] == 0xFFFFFFFF or header[8] == 0xFFFFFFFF:
                    _refuse("evidence bundle does not support ZIP64 local sizes")
                descriptor = bool(header[2] & 0x08)
                for local, central in (
                    (header[6], entry.CRC),
                    (header[7], entry.compress_size),
                    (header[8], entry.file_size),
                ):
                    if local != central and not (descriptor and local == 0):
                        _refuse(
                            "evidence bundle local ZIP sizes or CRC disagree with its directory"
                        )
                mode = stat.S_IFMT(entry.external_attr >> 16)
                allowed_flags = 0x0808 | (
                    0x0006 if entry.compress_type == zipfile.ZIP_DEFLATED else 0
                )
                if (
                    entry.orig_filename != entry.filename
                    or entry.is_dir()
                    or entry.flag_bits & ~allowed_flags
                    or entry.external_attr & (0x10 | 0x400)
                    or mode not in (0, stat.S_IFREG)
                    # Other ZIP codecs may inflate an unbounded internal chunk in read(n).
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    _refuse("evidence bundle contains an unsafe ZIP entry")
                if not 0 <= entry.file_size <= _EVIDENCE_BUNDLE_MAX_RECORD_BYTES:
                    _refuse("evidence bundle record exceeds the size limit")
                payload_start = entry.header_offset + 30 + header[9] + header[10]
                payload_end = payload_start + entry.compress_size
                if not 0 <= payload_start <= payload_end <= len(raw_zip):
                    _refuse("evidence bundle compressed payload is outside the archive")
                if descriptor:
                    next_boundary = min(
                        [archive.start_dir]
                        + [
                            other.header_offset
                            for other in members
                            if other.header_offset > entry.header_offset
                        ]
                    )
                    if not payload_end <= next_boundary <= len(raw_zip):
                        _refuse("evidence bundle data descriptor is outside its member boundary")
                    expected_descriptor = (entry.CRC, entry.compress_size, entry.file_size)
                    unsigned = (
                        payload_end + 12 == next_boundary
                        and struct.unpack_from("<3I", raw_zip, payload_end) == expected_descriptor
                    )
                    signed = (
                        payload_end + 16 == next_boundary
                        and raw_zip[payload_end : payload_end + 4] == b"PK\x07\x08"
                        and struct.unpack_from("<3I", raw_zip, payload_end + 4)
                        == expected_descriptor
                    )
                    if not (unsigned or signed):
                        _refuse("evidence bundle data descriptor is missing or inconsistent")
                payload_ranges[entry.filename] = (payload_start, payload_end)
                declared_total += entry.file_size
            if declared_total > _EVIDENCE_BUNDLE_MAX_TOTAL_BYTES:
                _refuse("evidence bundle records exceed the total size limit")

            actual_total = 0
            for entry in members:
                payload_start, payload_end = payload_ranges[entry.filename]
                # Retain zipfile's name and overlap checks, not its declared-size read cutoff.
                with archive.open(entry):
                    payload = raw_zip[payload_start:payload_end]
                if entry.compress_type == zipfile.ZIP_STORED:
                    if entry.compress_size != entry.file_size:
                        _refuse("evidence bundle stored member sizes disagree")
                    raw = payload
                else:
                    decompressor = zlib.decompressobj(wbits=-15)
                    raw = decompressor.decompress(payload, _EVIDENCE_BUNDLE_MAX_RECORD_BYTES + 1)
                    if (
                        not decompressor.eof
                        or decompressor.unconsumed_tail
                        or decompressor.unused_data
                    ):
                        _refuse("evidence bundle DEFLATE stream is incomplete or has trailing data")
                actual_total += len(raw)
                if (
                    len(raw) != entry.file_size
                    or len(raw) > _EVIDENCE_BUNDLE_MAX_RECORD_BYTES
                    or actual_total > _EVIDENCE_BUNDLE_MAX_TOTAL_BYTES
                ):
                    _refuse("evidence bundle decompressed bytes exceed their bounds")
                if zlib.crc32(raw) != entry.CRC:
                    _refuse("evidence bundle actual member CRC does not match its directory")
                records[entry.filename] = raw
    except (
        OSError,
        RuntimeError,
        ValueError,
        EOFError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        _refuse("evidence bundle ZIP is unsafe or unreadable")

    for name, raw in records.items():
        if _sha256_bytes(raw) != hashes[name]:
            _refuse("evidence bundle record bytes do not match their expected hash")
        try:
            record = _load_json_object(raw, "evidence bundle record")
        except RecursionError:
            _refuse("evidence bundle record JSON is nested too deeply")
        pending: list[Any] = [record]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, float) and not math.isfinite(value):
                _refuse("evidence bundle record contains a non-finite JSON number")
    return _freeze(records)


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


def _load_retained_json_object(raw: bytes, name: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _refuse(f"{name} has duplicate keys")
            result[key] = value
        return result

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse(f"{name} is not valid UTF-8 JSON")
    if not isinstance(result, dict):
        _refuse(f"{name} must be an object")
    return result


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=True))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ArtifactFileReference:
    repository: str
    run_id: str
    artifact_id: str
    artifact_name: str
    artifact_sha256: str
    retention_days: int
    expires_at: str
    path: str
    sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], name: str) -> ArtifactFileReference:
        validated = validate_artifact_consumer_proof_reference(value)
        artifact = validated["artifact"]
        return cls(
            repository=validated["repository"],
            run_id=validated["run_id"],
            artifact_id=artifact["id"],
            artifact_name=artifact["name"],
            artifact_sha256=artifact["sha256"],
            retention_days=artifact["retention"]["configured_days"],
            expires_at=artifact["retention"]["expires_at"],
            path=validated["path"],
            sha256=validated["sha256"],
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "run_id": self.run_id,
            "artifact": {
                "id": self.artifact_id,
                "name": self.artifact_name,
                "sha256": self.artifact_sha256,
                "retention": {
                    "configured_days": self.retention_days,
                    "expires_at": self.expires_at,
                },
            },
            "path": self.path,
            "sha256": self.sha256,
        }

    def same_artifact(self, other: ArtifactFileReference) -> bool:
        return (
            self.repository,
            self.run_id,
            self.artifact_id,
            self.artifact_name,
            self.artifact_sha256,
            self.retention_days,
            self.expires_at,
        ) == (
            other.repository,
            other.run_id,
            other.artifact_id,
            other.artifact_name,
            other.artifact_sha256,
            other.retention_days,
            other.expires_at,
        )


class ArtifactDownloader:
    def __init__(self, root: Path, *, environment: Mapping[str, str] | None = None):
        self._root = root
        self._environment = environment
        self._archives: dict[tuple[str, str, str], tuple[Path, str]] = {}
        self._archive_directory: Path | None = None

    def _run_gh(self, arguments: Sequence[str], name: str, *, timeout: int = 60) -> bytes:
        if self._environment is not None:
            status, raw = _promotion_gh(self._root, self._environment, arguments, name)
            if status != 200:
                _refuse(f"{name}: HTTP {status or 'UNKNOWN'}")
            return raw
        try:
            result = subprocess.run(
                ["gh", *arguments],
                cwd=self._root,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            _refuse(f"{name} could not be downloaded")
        if result.returncode != 0:
            _refuse(f"{name} could not be downloaded")
        return result.stdout

    def _metadata(self, repository: str, run_id: str, artifact: Mapping[str, Any]) -> None:
        raw = self._run_gh(
            ["api", f"repos/{repository}/actions/artifacts/{artifact['id']}"],
            "retained artifact metadata",
        )
        metadata = _load_retained_json_object(raw, "retained artifact metadata")
        workflow_run = metadata.get("workflow_run")
        if (
            str(metadata.get("id")) != artifact["id"]
            or metadata.get("name") != artifact["name"]
            or metadata.get("expired") is not False
            or not isinstance(workflow_run, Mapping)
            or str(workflow_run.get("id")) != run_id
            or metadata.get("expires_at") != artifact["retention"]["expires_at"]
        ):
            _refuse("retained artifact metadata does not match its structural reference")
        digest = metadata.get("digest")
        if digest is not None and digest != f"sha256:{artifact['sha256']}":
            _refuse("retained artifact metadata digest does not match its structural reference")

    def _archive(self, repository: str, run_id: str, artifact: Mapping[str, Any]) -> bytes:
        key = (repository, artifact["id"], artifact["sha256"])
        cached = self._archives.get(key)
        if cached is not None:
            cached_path, wire_sha256 = cached
            wire_bytes = _read_regular_bytes(cached_path, "cached retained artifact")
            if _sha256_bytes(wire_bytes) != wire_sha256 or wire_sha256 != artifact["sha256"]:
                _refuse("cached retained artifact wire bytes do not match the structural reference")
            return wire_bytes
        self._metadata(repository, run_id, artifact)
        wire_bytes = self._run_gh(
            ["api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
            "retained artifact",
            timeout=600,
        )
        wire_sha256 = _sha256_bytes(wire_bytes)
        if wire_sha256 != artifact["sha256"]:
            _refuse("retained artifact wire bytes do not match the structural reference")
        destination = self._root / "artifacts" / f"{artifact['id']}-{artifact['sha256']}.zip"
        try:
            if self._archive_directory is None:
                destination.parent.mkdir(parents=True, exist_ok=False)
                self._archive_directory = destination.parent
            elif destination.parent != self._archive_directory:
                _refuse("retained artifact directory is inconsistent")
            with destination.open("xb") as output:
                output.write(wire_bytes)
        except FileExistsError:
            _refuse("retained artifact destination already exists")
        except OSError:
            _refuse("fresh retained artifact directory is unavailable")
        self._archives[key] = (destination, wire_sha256)
        return wire_bytes

    def _member_bytes(
        self, repository: str, run_id: str, artifact: Mapping[str, Any], path: str
    ) -> bytes:
        wire_bytes = self._archive(repository, run_id, artifact)
        try:
            with zipfile.ZipFile(io.BytesIO(wire_bytes)) as archive:
                entries = [entry for entry in archive.infolist() if entry.filename == path]
                if self._environment is not None and (
                    len(archive.infolist()) > 64
                    or any(
                        entry.file_size > _PROMOTION_BINARY_LIMIT for entry in archive.infolist()
                    )
                    or sum(entry.file_size for entry in archive.infolist())
                    > _PROMOTION_BINARY_LIMIT
                ):
                    _refuse("retained artifact exceeds publisher bounds")
                if len(entries) != 1 or entries[0].is_dir():
                    _refuse("retained artifact file is absent or ambiguous")
                contents = archive.read(entries[0])
        except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            _refuse("retained artifact is unreadable")
        return contents

    def _artifact_member(self, reference: ArtifactFileReference) -> bytes:
        contents = self._member_bytes(
            reference.repository,
            reference.run_id,
            reference.as_mapping()["artifact"],
            reference.path,
        )
        if _sha256_bytes(contents) != reference.sha256:
            _refuse("retained artifact file bytes do not match the structural reference")
        return contents

    def _download_record(
        self, repository: str, run_id: str, artifact: Mapping[str, Any], path: str
    ) -> tuple[ArtifactFileReference, bytes]:
        _expect_repository(repository, "retained record repository")
        _expect_identifier(run_id, "retained record run ID")
        _validate_actions_artifact(artifact)
        _expect_safe_relative_path(path, "retained record path")
        contents = self._member_bytes(repository, run_id, artifact, path)
        reference = ArtifactFileReference.from_mapping(
            {
                "repository": repository,
                "run_id": run_id,
                "artifact": artifact,
                "path": path,
                "sha256": _sha256_bytes(contents),
            },
            "retained record reference",
        )
        self._retain_file(reference, contents)
        return reference, contents

    def _retain_file(self, reference: ArtifactFileReference, contents: bytes) -> Path:
        destination = self._root / "downloaded" / Path(*reference.path.split("/"))
        if not _path_within(destination, self._root):
            _refuse("retained artifact path escapes the fresh download directory")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(contents)
        except OSError:
            _refuse("retained artifact file cannot be retained locally")
        return destination

    def download_file(self, reference: ArtifactFileReference) -> Path:
        return self._retain_file(reference, self._artifact_member(reference))

    def download_payload(
        self,
        archive: ArtifactFileReference,
        manifest: ArtifactFileReference,
        candidate_artifact_sha256: str,
    ) -> tuple[Path, Path]:
        if not archive.same_artifact(manifest):
            _refuse("payload references do not name one retained artifact")
        archive_contents = self._artifact_member(archive)
        manifest_contents = self._artifact_member(manifest)
        payload_digest = hashlib.sha256()
        payload_digest.update(archive_contents)
        payload_digest.update(manifest_contents)
        if payload_digest.hexdigest() != candidate_artifact_sha256:
            _refuse("downloaded retained payload does not match the candidate artifact")
        return (
            self._retain_file(archive, archive_contents),
            self._retain_file(manifest, manifest_contents),
        )


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


def _verified_raw_candidate(
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
    for artifact in (reference["artifact"], candidate["build"]["artifact"]):
        _review_timestamp(artifact["retention"]["expires_at"], "artifact expiry")
    return identity, compact_reference


def _verified_review_candidate(
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    identity, compact_reference = _verified_raw_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    now = datetime.now(timezone.utc)
    for artifact in (
        candidate_identity_reference["artifact"],
        identity["candidate"]["build"]["artifact"],
    ):
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


def _verified_release_gate_catalog(
    catalog: Mapping[str, Any],
    catalog_bytes: bytes,
    catalog_reference: Mapping[str, Any],
    candidate: Mapping[str, Any],
    repository_root: str | PathLike[str],
) -> Mapping[str, Any]:
    reference = _review_record_reference(catalog_reference, "stage release gate catalog reference")
    if (
        reference["repository"] != candidate["source"]["repository"]
        or not isinstance(catalog_bytes, bytes)
        or _sha256_bytes(catalog_bytes) != reference["sha256"]
    ):
        _refuse("stage release gate catalog bytes do not match their reference")
    document = _load_json_object(catalog_bytes, "stage release gate catalog")
    if document != _thaw(catalog):
        _refuse("stage release gate catalog bytes do not match the supplied catalog")
    _validate_release_gate_catalog_for_consumer_proof(document, candidate)
    root = _resolve_repository_root(repository_root)
    for snapshot in document["catalog"]["policy_authority_snapshots"]:
        actual_hash = _sha256_bytes(
            _read_tracked_authority(root, candidate["source"]["commit"], snapshot["path"])
        )
        if snapshot["sha256"] != actual_hash:
            _refuse("stage release gate catalog policy snapshot differs from exact source bytes")
    return document


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
    catalog_document = _verified_release_gate_catalog(
        release_gate_catalog,
        release_gate_catalog_bytes,
        catalog_reference,
        candidate,
        repository_root,
    )

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


def _decision_actor(value: Any, role: str) -> Mapping[str, Any]:
    actor = _expect_mapping(value, role, ("id", "login"))
    _expect_identifier(actor["id"], f"{role} ID")
    login = _expect_pattern(actor["login"], f"{role} login", _GITHUB_ACTOR_LOGIN_PATTERN)
    if len(login) > 255:
        _refuse(f"{role} login exceeds its bound")
    return actor


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
        _decision_actor(authorization[role], role)
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


def _expect_remote_tag(value: Any, name: str) -> str:
    tag = _expect_string(value, name)
    prefix = "stateless-preview-v"
    if not tag.startswith(prefix):
        _refuse(f"{name} is not a preview tag")
    _expect_pattern(tag[len(prefix) :], name, _PREVIEW_VERSION_PATTERN)
    return tag


def _expect_remote_asset_name(value: Any, kind: str) -> str:
    name = _expect_string(value, f"remote {kind} name")
    prefix = "netcoredbg-mcp-stateless-preview-win-x64-"
    suffix = ".zip" if kind == "archive" else ".manifest.json"
    if not name.startswith(prefix) or not name.endswith(suffix):
        _refuse(f"remote {kind} name is not a preview asset")
    _expect_pattern(
        name[len(prefix) : -len(suffix)], f"remote {kind} version", _PREVIEW_VERSION_PATTERN
    )
    return name


def _validate_remote_tag(value: Any) -> None:
    if not isinstance(value, Mapping):
        _refuse("remote tag observation must be an object")
    status = value.get("status")
    if status == "ABSENT":
        _expect_mapping(value, "absent remote tag", ("status",))
    elif status == "PRESENT":
        tag = _expect_mapping(
            value, "present remote tag", ("status", "tag", "tag_type", "object_id", "peeled_commit")
        )
        _expect_remote_tag(tag["tag"], "remote tag")
        if tag["tag_type"] not in ("ANNOTATED", "LIGHTWEIGHT"):
            _refuse("remote tag type is unsupported")
        _expect_commit(tag["object_id"], "remote tag object ID")
        _expect_commit(tag["peeled_commit"], "remote tag peeled commit")
    elif status == "UNREADABLE":
        tag = _expect_mapping(value, "unreadable remote tag", ("status", "reason_code"))
        if tag["reason_code"] not in (
            "TAG_READBACK_UNAVAILABLE",
            "TAG_READBACK_UNAUTHORIZED",
            "TAG_READBACK_INCOMPLETE",
        ):
            _refuse("remote tag readback reason is unsupported")
    else:
        _refuse("remote tag observation status is unsupported")


def _validate_remote_release(value: Any) -> None:
    if not isinstance(value, Mapping):
        _refuse("remote release observation must be an object")
    status = value.get("status")
    if status == "ABSENT":
        _expect_mapping(value, "absent remote release", ("status",))
    elif status == "PRESENT":
        release = _expect_mapping(
            value,
            "present remote release",
            (
                "status",
                "release_id",
                "tag",
                "is_draft",
                "is_prerelease",
                "destination_metadata_status",
            ),
        )
        _expect_identifier(release["release_id"], "remote release ID")
        _expect_remote_tag(release["tag"], "remote release tag")
        _expect_boolean(release["is_draft"], "remote release is_draft")
        _expect_boolean(release["is_prerelease"], "remote release is_prerelease")
        if release["destination_metadata_status"] not in ("MATCHING", "MISMATCHED"):
            _refuse("remote release metadata status is unsupported")
    elif status == "UNREADABLE":
        release = _expect_mapping(value, "unreadable remote release", ("status", "reason_code"))
        if release["reason_code"] not in (
            "RELEASE_READBACK_UNAVAILABLE",
            "RELEASE_READBACK_UNAUTHORIZED",
            "RELEASE_READBACK_INCOMPLETE",
        ):
            _refuse("remote release readback reason is unsupported")
    else:
        _refuse("remote release observation status is unsupported")


def _validate_remote_asset(value: Any, kind: str) -> None:
    if not isinstance(value, Mapping):
        _refuse(f"remote {kind} observation must be an object")
    status = value.get("status")
    if status == "ABSENT":
        _expect_mapping(value, f"absent remote {kind}", ("status",))
        return
    if status == "PRESENT":
        asset = _expect_mapping(
            value,
            f"present remote {kind}",
            ("status", "metadata", "downloaded_identity", "observed_result", "identity_status"),
        )
        descriptor = _validate_file_descriptor(
            asset["downloaded_identity"], f"remote {kind} downloaded identity"
        )
        _expect_remote_asset_name(descriptor["name"], kind)
        if asset["observed_result"] not in _REVIEW_OBSERVED_RESULTS or asset[
            "identity_status"
        ] not in (
            "MATCHES_CANDIDATE",
            "METADATA_SIZE_MISMATCH",
            "DOWNLOADED_SIZE_MISMATCH",
            "DOWNLOADED_SHA256_MISMATCH",
        ):
            _refuse(f"remote {kind} identity result is unsupported")
    elif status in ("PRESENT_UNREADABLE", "UNREADABLE"):
        keys = (
            ("status", "metadata", "reason_code")
            if status == "PRESENT_UNREADABLE"
            else ("status", "reason_code")
        )
        asset = _expect_mapping(value, f"unreadable remote {kind}", keys)
        reason_prefix = kind.upper() + (
            "_DOWNLOAD_" if status == "PRESENT_UNREADABLE" else "_READBACK_"
        )
        if asset["reason_code"] not in tuple(
            reason_prefix + code for code in ("UNAVAILABLE", "UNAUTHORIZED", "INCOMPLETE")
        ):
            _refuse(f"remote {kind} readback reason is unsupported")
        if status == "UNREADABLE":
            return
    else:
        _refuse(f"remote {kind} observation status is unsupported")
    metadata = _expect_mapping(
        asset["metadata"], f"remote {kind} metadata", ("id", "name", "size_bytes")
    )
    _expect_identifier(metadata["id"], f"remote {kind} asset ID")
    _expect_remote_asset_name(metadata["name"], kind)
    _expect_positive_integer(metadata["size_bytes"], f"remote {kind} metadata size")


def _operation_outcome(response: Mapping[str, Any], postcondition: str) -> str:
    if response["kind"] == "DENIED":
        return "DENIED"
    if response["kind"] == "CONFLICT":
        return "COLLISION"
    if postcondition == "MATCH":
        return "CONFIRMED" if response["kind"] == "ACKNOWLEDGED" else "RECONCILED"
    return "COLLISION" if postcondition == "MISMATCH" else "UNKNOWN"


def _operation_postcondition(
    result: Mapping[str, Any],
    observation: Mapping[str, Any],
    candidate: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> str:
    if (
        _remote_collision_reason(
            observation,
            candidate,
            _review_timestamp(observation["observed_at"], "operation observation time"),
        )
        is not None
    ):
        unreadable = (
            observation["readback_status"]["observed_result"] != "SUCCESS"
            or observation["tag_observation"]["status"] == "UNREADABLE"
            or observation["release_observation"]["status"] == "UNREADABLE"
            or any(
                observation["asset_observations"][k]["status"]
                in ("UNREADABLE", "PRESENT_UNREADABLE")
                for k in ("archive", "manifest")
            )
        )
        return "UNREADABLE" if unreadable else "MISMATCH"
    operation, target, response = result["operation"], result["target"], result["response"]
    tag, release, assets = (
        observation["tag_observation"],
        observation["release_observation"],
        observation["asset_observations"],
    )
    if operation == "CREATE_TAG":
        if tag["status"] == "ABSENT":
            return "ABSENT"
        return "MATCH" if tag["object_id"] == target["object_id"] else "MISMATCH"
    if release["status"] == "ABSENT":
        return "ABSENT"
    if operation != "CREATE_DRAFT" and release["release_id"] != target["release_id"]:
        return "MISMATCH"
    if operation.startswith("UPLOAD_"):
        asset = assets["archive" if operation == "UPLOAD_ARCHIVE" else "manifest"]
        if asset["status"] == "ABSENT":
            return "ABSENT"
        identity = asset["metadata"]["id"]
        if asset["downloaded_identity"] != target["descriptor"]:
            return "MISMATCH"
    else:
        identity = release["release_id"]
    if response["resource_id"] is not None and response["resource_id"] != identity:
        return "MISMATCH"
    if operation == "PUBLISH_RELEASE":
        if release["is_draft"]:
            return "ABSENT"
        if record_resolver is not None:
            try:
                fragment = _resolve_stage_record(
                    target["pre_publication_verification"], record_resolver
                )
            except (ValueError, OSError, URLError):
                return "UNREADABLE"
            if fragment != _promotion_fragment({"candidate": candidate}, observation):
                return "MISMATCH"
    return "MATCH"


def _validate_operation_results(
    record: Mapping[str, Any],
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> None:
    results = _expect_list(record["operation_results"], "operation results")
    if len(results) > 2:
        _refuse("operation results exceed one advancement block")
    operations = []
    origins = set()
    for result in results:
        _expect_mapping(
            result,
            "operation result",
            (
                "origin_attempt_id",
                "operation",
                "intent_sha256",
                "target",
                "sent_at",
                "response",
                "postcondition",
                "outcome",
            ),
        )
        origin = _expect_safe_opaque_id(result["origin_attempt_id"], "operation origin")
        origins.add(origin)
        operation = result["operation"]
        fields = {
            "CREATE_TAG": ("tag", "object_id"),
            "CREATE_DRAFT": ("tag", "source_commit"),
            "UPLOAD_ARCHIVE": ("release_id", "descriptor"),
            "UPLOAD_MANIFEST": ("release_id", "descriptor"),
            "PUBLISH_RELEASE": ("release_id", "pre_publication_verification"),
        }.get(operation)
        if fields is None or operation in operations:
            _refuse("operation result is unsupported or duplicated")
        operations.append(operation)
        target = _expect_mapping(
            result["target"],
            "operation target",
            (
                "repository",
                "candidate_identity_record",
                *fields,
            ),
        )
        if (
            _expect_repository(target["repository"], "operation repository")
            != candidate["source"]["repository"]
            or _review_record_reference(target["candidate_identity_record"], "operation Candidate")
            != reference
        ):
            _refuse("operation target does not bind the exact Candidate/repository")
        if "tag" in fields and target["tag"] != candidate["destination"]["tag"]:
            _refuse("operation targets a different tag")
        if operation == "CREATE_TAG":
            _expect_commit(target["object_id"], "operation tag OID")
        if operation == "CREATE_DRAFT" and target["source_commit"] != candidate["source"]["commit"]:
            _refuse("draft intent targets a different source")
        if "release_id" in fields:
            _expect_identifier(target["release_id"], "operation release ID")
        if "descriptor" in fields:
            expected = (
                candidate["preview_manifest"]["contents"]["archive"]
                if operation == "UPLOAD_ARCHIVE"
                else candidate["preview_manifest"]["file"]
            )
            if _validate_file_descriptor(target["descriptor"], "operation descriptor") != expected:
                _refuse("upload intent differs from the approved descriptor")
        if operation == "PUBLISH_RELEASE":
            _review_record_reference(target["pre_publication_verification"], "publication fragment")
        intent = {key: result[key] for key in ("origin_attempt_id", "operation", "target")}
        if _expect_sha256(result["intent_sha256"], "intent hash") != _sha256_bytes(
            _canonical_json_bytes(intent, "write intent")
        ):
            _refuse("operation result differs from its canonical original intent")
        sent = result["sent_at"]
        if sent is None:
            if origin == record["promotion_attempt_id"]:
                _refuse("a current send must record transmission time")
        elif _review_timestamp(sent, "operation sent_at") > _review_timestamp(
            record["observed_at"], "operation readback"
        ):
            _refuse("operation readback predates transmission")
        response = _expect_mapping(
            result["response"],
            "operation response",
            (
                "kind",
                "http_status",
                "git_returncode",
                "resource_id",
            ),
        )
        status, code, resource = (
            response["http_status"],
            response["git_returncode"],
            response["resource_id"],
        )
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            _refuse("operation HTTP status is invalid")
        if code is not None and type(code) is not int:
            _refuse("operation Git return code is invalid")
        if resource is not None:
            _expect_identifier(resource, "operation resource ID")
        kind = response["kind"]
        if (
            status is not None
            and 400 <= status < 500
            and kind != ("CONFLICT" if status in (409, 422) else "DENIED")
        ):
            _refuse("definite server refusal cannot be relabeled UNKNOWN or success")
        if kind not in ("ACKNOWLEDGED", "DENIED", "CONFLICT", "UNKNOWN"):
            _refuse("operation response kind is unsupported")
        if operation == "CREATE_TAG":
            if status is not None or resource is not None or (kind == "ACKNOWLEDGED" and code != 0):
                _refuse("tag response does not bind native Git")
            if kind in ("DENIED", "CONFLICT") and code is None:
                _refuse("tag refusal lacks a completed native result")
        elif (
            code is not None
            or (
                kind == "ACKNOWLEDGED"
                and (status != (200 if operation == "PUBLISH_RELEASE" else 201) or resource is None)
            )
            or (
                kind == "DENIED"
                and (status is None or not 400 <= status < 500 or status in (409, 422))
            )
            or (kind == "CONFLICT" and status not in (409, 422))
        ):
            _refuse("operation response does not establish its claimed acknowledgement/refusal")
        postcondition = _operation_postcondition(result, record, candidate, record_resolver)
        if result["postcondition"] != postcondition or result["outcome"] != _operation_outcome(
            response, postcondition
        ):
            _refuse("operation result is not derived from its response and exact readback")
    if len(origins) > 1 or (
        len(operations) == 2
        and operations
        not in (
            ["CREATE_TAG", "CREATE_DRAFT"],
            ["UPLOAD_ARCHIVE", "UPLOAD_MANIFEST"],
        )
    ):
        _refuse("operation results mix origins or advancement blocks")


def _validate_remote_observation(
    value: Any,
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    current_attempt_id: str,
    record_resolver: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> Mapping[str, Any]:
    record = _expect_mapping(
        value,
        "remote observation",
        (
            "observation_schema_version",
            "observation_id",
            "promotion_attempt_id",
            "candidate_identity_record",
            "candidate",
            "retained_artifact_availability",
            "tag_observation",
            "release_observation",
            "asset_observations",
            "readback_status",
            "operation_results",
            "observed_at",
        ),
    )
    if record["observation_schema_version"] != "2.0":
        _refuse("remote observation schema version is unsupported")
    _expect_safe_opaque_id(record["observation_id"], "remote observation ID")
    attempt_id = _expect_safe_opaque_id(current_attempt_id, "current promotion attempt ID")
    if (
        _expect_safe_opaque_id(record["promotion_attempt_id"], "observation promotion attempt ID")
        != attempt_id
    ):
        _refuse("remote observation belongs to another promotion attempt")
    observed_candidate = _validate_candidate_identity(
        {"schema_version": "1.0", "candidate": record["candidate"]}
    )["candidate"]
    if (
        _review_record_reference(
            record["candidate_identity_record"], "remote candidate identity reference"
        )
        != reference
        or observed_candidate != candidate
    ):
        _refuse("remote observation does not bind the exact downloaded candidate")
    _review_timestamp(record["observed_at"], "remote observed_at")
    retained = _expect_mapping(
        record["retained_artifact_availability"],
        "retained artifact availability",
        ("status", "observed_result"),
    )
    if (
        retained["status"]
        not in (
            "AVAILABLE_AND_VERIFIED",
            "UNAVAILABLE",
            "EXPIRED",
            "IDENTITY_MISMATCH",
            "UNREADABLE",
        )
        or retained["observed_result"] not in _REVIEW_OBSERVED_RESULTS
    ):
        _refuse("retained artifact availability contains an unsupported value")
    _validate_remote_tag(record["tag_observation"])
    _validate_remote_release(record["release_observation"])
    assets = _expect_mapping(
        record["asset_observations"],
        "remote asset observations",
        ("archive", "manifest", "unexpected_asset_ids"),
    )
    for kind in ("archive", "manifest"):
        _validate_remote_asset(assets[kind], kind)
    unexpected = _expect_list(assets["unexpected_asset_ids"], "unexpected asset IDs")
    if len(unexpected) > 64:
        _refuse("unexpected asset IDs exceed their bound")
    identifiers: set[str] = set()
    for value in unexpected:
        identifier = _expect_safe_opaque_id(value, "unexpected asset ID")
        if identifier in identifiers:
            _refuse("unexpected asset IDs contain duplicates")
        identifiers.add(identifier)
    readback = _expect_mapping(
        record["readback_status"],
        "remote readback status",
        ("expected_result", "observed_result", "reason_code"),
    )
    if (
        readback["expected_result"] != "EXPECT_SUCCESS"
        or readback["observed_result"] not in _REVIEW_OBSERVED_RESULTS
        or readback["reason_code"]
        not in (
            "READBACK_COMPLETE",
            "READBACK_UNAVAILABLE",
            "READBACK_UNAUTHORIZED",
            "READBACK_INCOMPLETE",
            "READBACK_EVIDENCE_REJECTED",
        )
    ):
        _refuse("remote readback status contains an unsupported value")
    _validate_operation_results(record, candidate, reference, record_resolver)
    return record


def seal_remote_observation(
    observation: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    current_attempt_id: str,
    record_resolver: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> Mapping[str, Any]:
    """Seal candidate-bound recorded facts, preserving negative observations without remote I/O."""
    identity, reference = _verified_raw_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    record = _validate_remote_observation(
        _thaw(observation), identity["candidate"], reference, current_attempt_id, record_resolver
    )
    return _freeze(record)


def _remote_collision_reason(
    record: Mapping[str, Any], candidate: Mapping[str, Any], classified_at: datetime
) -> str | None:
    retained = record["retained_artifact_availability"]
    expiry = _review_timestamp(
        candidate["build"]["artifact"]["retention"]["expires_at"], "retained payload expiry"
    )
    if (
        retained["status"] == "EXPIRED"
        or expiry <= _review_timestamp(record["observed_at"], "remote observed_at")
        or expiry <= classified_at
    ):
        return "RETAINED_ARTIFACT_EXPIRED"
    if retained["status"] == "UNAVAILABLE":
        return "RETAINED_ARTIFACT_UNAVAILABLE"
    if retained["status"] == "IDENTITY_MISMATCH":
        return "RETAINED_ARTIFACT_IDENTITY_MISMATCH"
    tag = record["tag_observation"]
    release = record["release_observation"]
    assets = record["asset_observations"]
    readback = record["readback_status"]
    if (
        retained["status"] == "UNREADABLE"
        or readback["reason_code"] in ("READBACK_UNAVAILABLE", "READBACK_UNAUTHORIZED")
        or tag["status"] == "UNREADABLE"
        or release["status"] == "UNREADABLE"
        or any(
            assets[kind]["status"] in ("UNREADABLE", "PRESENT_UNREADABLE")
            for kind in ("archive", "manifest")
        )
    ):
        return "REMOTE_READBACK_UNREADABLE"
    present = [
        assets[kind] for kind in ("archive", "manifest") if assets[kind]["status"] == "PRESENT"
    ]
    if (
        retained["observed_result"] != "SUCCESS"
        or readback["observed_result"] != "SUCCESS"
        or readback["reason_code"] != "READBACK_COMPLETE"
        or (release["status"] == "ABSENT" and (present or assets["unexpected_asset_ids"]))
        or (len(present) == 2 and present[0]["metadata"]["id"] == present[1]["metadata"]["id"])
    ):
        return "REMOTE_READBACK_INCOMPLETE"
    if tag["status"] == "PRESENT":
        if tag["tag_type"] != "ANNOTATED":
            return "TAG_NON_ANNOTATED"
        if (
            tag["tag"] != candidate["destination"]["tag"]
            or tag["peeled_commit"] != candidate["source"]["commit"]
        ):
            return "TAG_TARGET_MISMATCH"
    if release["status"] == "PRESENT":
        if (
            release["tag"] != candidate["destination"]["tag"]
            or release["destination_metadata_status"] != "MATCHING"
        ):
            return "RELEASE_METADATA_MISMATCH"
        if (
            tag["status"] != "PRESENT"
            or not release["is_prerelease"]
            or (not release["is_draft"] and len(present) != 2)
        ):
            return "RELEASE_STATE_MISMATCH"
    if assets["unexpected_asset_ids"]:
        return "UNEXPECTED_ASSET_PRESENT"
    for kind, expected in (
        ("archive", candidate["preview_manifest"]["contents"]["archive"]),
        ("manifest", candidate["preview_manifest"]["file"]),
    ):
        asset = assets[kind]
        if asset["status"] == "PRESENT" and (
            asset["metadata"]["name"] != expected["name"]
            or asset["metadata"]["size_bytes"] != expected["size_bytes"]
            or asset["downloaded_identity"] != expected
            or asset["observed_result"] != "SUCCESS"
            or asset["identity_status"] != "MATCHES_CANDIDATE"
        ):
            return "ASSET_IDENTITY_MISMATCH"
    return None


def classify_remote_observation(
    remote_observed_state_reference: Mapping[str, Any],
    *,
    classification_id: str,
    classified_at: str,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    current_attempt_id: str,
    current_workflow_run: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
) -> Mapping[str, Any]:
    """Derive one immutable recovery action from verified recorded facts, not effect authority."""
    identity, candidate_reference = _verified_raw_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    candidate = identity["candidate"]
    _expect_safe_opaque_id(classification_id, "remote classification ID")
    classification_time = _review_timestamp(classified_at, "remote classified_at")
    workflow = _expect_mapping(
        current_workflow_run,
        "current promote workflow run",
        ("repository", "workflow_path", "mode", "event", "run_id", "run_attempt", "ref", "commit"),
    )
    _expect_repository(workflow["repository"], "current workflow repository")
    _expect_identifier(workflow["run_id"], "current workflow run ID")
    _expect_positive_integer(workflow["run_attempt"], "current workflow run attempt")
    _expect_commit(workflow["commit"], "current workflow commit")
    if (
        workflow["repository"] != candidate["source"]["repository"]
        or workflow["commit"] != candidate["source"]["commit"]
        or workflow["workflow_path"] != ".github/workflows/stateless-preview.yml"
        or workflow["mode"] != "promote"
        or workflow["event"] != "workflow_dispatch"
        or workflow["ref"] != _CANONICAL_SOURCE_REF
    ):
        _refuse("remote classification requires the current canonical candidate promote run")
    reference = _review_record_reference(
        _thaw(remote_observed_state_reference), "remote observation reference"
    )
    if (
        reference["repository"] != workflow["repository"]
        or reference["run_id"] != workflow["run_id"]
    ):
        _refuse("remote observation reference belongs to another workflow run")
    if not callable(record_resolver):
        _refuse("remote observation resolver is unavailable")
    record = _validate_remote_observation(
        _resolve_stage_record(reference, record_resolver),
        candidate,
        candidate_reference,
        current_attempt_id,
        record_resolver,
    )
    if classification_time < _review_timestamp(record["observed_at"], "remote observed_at"):
        _refuse("remote classification predates its observation")
    _validate_operation_results(record, candidate, candidate_reference, record_resolver)
    reason = _remote_collision_reason(record, candidate, classification_time)
    for operation in record["operation_results"]:
        outcome = operation["outcome"]
        if outcome in ("COLLISION", "UNKNOWN") or (
            outcome == "DENIED" and operation["origin_attempt_id"] == current_attempt_id
        ):
            reason = {
                "DENIED": "OPERATION_DENIED",
                "COLLISION": "OPERATION_COLLISION",
                "UNKNOWN": "OPERATION_UNKNOWN",
            }[outcome]
            break
    if reason is not None:
        state, admission, action = "collision", "REFUSED", "REFUSE"
        result = {"expected": "EXPECT_SUCCESS", "observed": "REFUSAL", "status": "REFUSED"}
    else:
        admission = "ADMITTED"
        result = {"expected": "EXPECT_SUCCESS", "observed": "SUCCESS", "status": "PASS"}
        if record["tag_observation"]["status"] == "ABSENT":
            state, action, reason = (
                "unstarted",
                "CREATE_ANNOTATED_TAG_AND_DRAFT_RELEASE",
                "TAG_ABSENT",
            )
        elif record["release_observation"]["status"] == "ABSENT":
            state, action, reason = "tag_only", "CREATE_DRAFT_PRERELEASE", "TAG_MATCHING_ANNOTATED"
        elif not record["release_observation"]["is_draft"]:
            state, action, reason = (
                "published_complete",
                "RUN_REMOTE_CONSUMER_PROOF",
                "RELEASE_MATCHING_PRERELEASE",
            )
        else:
            count = sum(
                record["asset_observations"][kind]["status"] == "PRESENT"
                for kind in ("archive", "manifest")
            )
            if count == 2:
                state, action, reason = (
                    "draft_complete",
                    "VERIFY_REMOTE_ASSETS_AND_PUBLISH_PRERELEASE",
                    "EXPECTED_ASSETS_COMPLETE_MATCH",
                )
            elif count == 1:
                state, action, reason = (
                    "draft_partial",
                    "UPLOAD_MISSING_APPROVED_ASSETS",
                    "EXPECTED_ASSETS_PARTIAL_MATCH",
                )
            else:
                state, action, reason = (
                    "draft_empty",
                    "UPLOAD_MISSING_APPROVED_ASSETS",
                    "EXPECTED_ASSETS_ABSENT",
                )
    return _freeze(
        {
            "classification_schema_version": "1.0",
            "classification_id": classification_id,
            "promotion_attempt_id": current_attempt_id,
            "candidate_identity_record": candidate_reference,
            "candidate": candidate,
            "remote_observed_state_reference": reference,
            "state": state,
            "admission": admission,
            "allowed_action": action,
            "reason_codes": [reason],
            "classification_result": result,
            "classified_at": classified_at,
        }
    )


def seal_promotion_attempt(
    attempt: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
    release_gate_catalog_bytes: bytes,
    release_gate_catalog_reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
    repository_root: str | PathLike[str],
    current_promotion_context: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Revalidate immutable Attempt 3.0 against independently captured current facts."""
    identity, identity_reference = _verified_raw_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    candidate = identity["candidate"]
    context = _expect_mapping(
        current_promotion_context,
        "current promotion context",
        (
            "current_attempt_id",
            "workflow_run",
            "job_id",
            "job_key",
            "workflow_sha",
            "checkout_head",
            "origin_main_target",
            "current_github_actor",
            "current_github_triggering_actor",
            "predecessor_attempt",
            "predecessor_operation_results",
        ),
    )
    current_attempt_id = _expect_safe_opaque_id(context["current_attempt_id"], "current attempt ID")
    _expect_identifier(context["job_id"], "current promote job ID")
    for field in ("workflow_sha", "checkout_head", "origin_main_target"):
        if _expect_commit(context[field], f"current {field}") != candidate["source"]["commit"]:
            _refuse("current source/workflow does not equal the approved canonical candidate")
    if context["job_key"] != "promote":
        _refuse("promotion requires the current promote job")
    workflow = _expect_mapping(
        context["workflow_run"],
        "current promote workflow run",
        ("repository", "workflow_path", "mode", "event", "run_id", "run_attempt", "ref", "commit"),
    )
    _expect_identifier(workflow["run_id"], "current workflow run ID")
    _expect_positive_integer(workflow["run_attempt"], "current workflow run attempt")
    record = _expect_mapping(
        _thaw(attempt),
        "promotion attempt",
        (
            "schema_version",
            "attempt_id",
            "candidate_identity_record",
            "candidate_identity_record_sha256",
            "candidate",
            "promotion_decision",
            "promotion_decision_sha256",
            "pre_decision_stage_evidence",
            "pre_publication_stage_evidence",
            "authorization",
            "workflow_run",
            "remote_observation",
            "remote_classification",
            "predecessor_attempt",
            "admission_status",
            "recorded_at",
        ),
    )
    if (
        record["schema_version"] != "3.0"
        or record["admission_status"] != "PASS"
        or _expect_safe_opaque_id(record["attempt_id"], "promotion attempt ID")
        != current_attempt_id
        or record["workflow_run"] != workflow
    ):
        _refuse("promotion attempt version or independent current identity does not match")
    recorded_workflow = _expect_mapping(
        record["workflow_run"], "recorded promote workflow run", tuple(workflow)
    )
    _expect_positive_integer(recorded_workflow["run_attempt"], "recorded workflow run attempt")
    _validate_candidate_identity({"schema_version": "1.0", "candidate": record["candidate"]})
    _validate_review_candidate_binding(record, candidate, identity_reference)
    recorded_at = _review_timestamp(record["recorded_at"], "promotion attempt recorded_at")
    for artifact in (candidate_identity_reference["artifact"], candidate["build"]["artifact"]):
        if recorded_at >= _review_timestamp(artifact["retention"]["expires_at"], "artifact expiry"):
            _refuse("promotion attempt candidate retention has expired")
    if not callable(record_resolver):
        _refuse("promotion attempt raw record resolver is unavailable")
    stage_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
        "release_gate_catalog": release_gate_catalog,
        "release_gate_catalog_bytes": release_gate_catalog_bytes,
        "release_gate_catalog_reference": release_gate_catalog_reference,
        "record_resolver": record_resolver,
        "repository_root": repository_root,
    }
    decision_reference = _review_record_reference(record["promotion_decision"], "attempt Decision")
    if decision_reference["repository"] != candidate["source"]["repository"]:
        _refuse("promotion Decision reference belongs to another repository")
    if (
        _expect_sha256(record["promotion_decision_sha256"], "attempt Decision hash")
        != decision_reference["sha256"]
    ):
        _refuse("promotion attempt Decision hash differs from its raw reference")
    decision = seal_promotion_decision(
        _resolve_stage_record(decision_reference, record_resolver), **stage_binding
    )
    if (
        decision["decision"] != "APPROVE"
        or record["pre_decision_stage_evidence"]
        != decision["evidence"]["pre_decision_stage_evidence"]
    ):
        _refuse("promotion attempt requires the approved Decision's exact pre-decision receipt")
    authorization = _expect_mapping(
        record["authorization"],
        "attempt authorization",
        (
            "decision_authorization",
            "current_github_actor",
            "current_github_triggering_actor",
            "execution_policy",
        ),
    )
    if authorization["decision_authorization"] != decision["promotion_attempt"]:
        _refuse("promotion attempt authorization differs from its resolved Decision")
    for role in ("current_github_actor", "current_github_triggering_actor"):
        if (
            _decision_actor(context[role], f"independent {role}")
            != decision["promotion_attempt"]["authorized_dispatcher"]
            or _decision_actor(authorization[role], role) != context[role]
        ):
            _refuse("current actor and rerun initiator must equal the authorized dispatcher")
    summary_fields = (
        "candidate_identity_record_sha256",
        "source_ref",
        "source_commit",
        "stage",
        "outcome",
    )
    pre_publication = _expect_mapping(
        record["pre_publication_stage_evidence"],
        "attempt pre-publication receipt",
        ("record_reference", *summary_fields),
    )
    stage = seal_stage_gate_evidence(
        _resolve_stage_record(pre_publication["record_reference"], record_resolver), **stage_binding
    )
    if (
        stage["stage"] != "pre-publication"
        or stage["outcome"] != "PASS"
        or any(pre_publication[field] != stage[field] for field in summary_fields)
    ):
        _refuse("promotion attempt requires its exact passing pre-publication Stage")
    decision_time = _review_timestamp(decision["decided_at"], "Decision decided_at")
    stage_time = _review_timestamp(stage["sealed_at"], "pre-publication sealed_at")
    if not decision_time <= stage_time <= recorded_at:
        _refuse("promotion attempt stages are outside Decision/admission chronology")
    policy = _expect_mapping(
        authorization["execution_policy"],
        "promotion execution policy",
        (
            "model",
            "run_attempt",
            "job_key",
            "job_id",
            "workflow_sha",
            "workflow_blob_sha256",
            "execution_token",
        ),
    )
    _expect_positive_integer(policy["run_attempt"], "execution run attempt")
    _expect_identifier(policy["job_id"], "execution job ID")
    _expect_sha256(policy["workflow_blob_sha256"], "workflow blob SHA-256")
    _expect_mapping(
        policy["execution_token"],
        "execution token provenance",
        ("source", "kind", "identity_observation", "app_id", "installation_id"),
    )
    if policy != _promotion_execution_policy(Path(repository_root), context):
        _refuse("execution policy differs from exact tracked workflow/current job/token provenance")
    predecessor = record["predecessor_attempt"]
    if predecessor is not None:
        predecessor = _review_record_reference(predecessor, "predecessor Attempt")
        prior = _resolve_stage_record(predecessor, record_resolver)
        if (
            prior.get("schema_version") != "3.0"
            or prior.get("admission_status") != "PASS"
            or prior.get("candidate") != candidate
            or prior.get("attempt_id") == current_attempt_id
            or _review_timestamp(prior.get("recorded_at"), "predecessor time") >= recorded_at
        ):
            _refuse("predecessor Attempt does not bind earlier exact candidate admission")
    if predecessor != context["predecessor_attempt"]:
        _refuse("predecessor differs from independent complete discovery")
    for reference in (record["remote_observation"], record["remote_classification"]):
        reference = _review_record_reference(reference, "current attempt record reference")
        if (
            reference["repository"] != workflow["repository"]
            or reference["run_id"] != workflow["run_id"]
        ):
            _refuse("current attempt record reference belongs to another run")
    remote_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
        "current_attempt_id": current_attempt_id,
    }
    observation = seal_remote_observation(
        _resolve_stage_record(record["remote_observation"], record_resolver),
        record_resolver=record_resolver,
        **remote_binding,
    )
    expected_results = _thaw(context["predecessor_operation_results"])
    if predecessor is None and expected_results:
        _refuse("initial admission cannot invent predecessor operations")
    if predecessor is not None and any(
        result.get("origin_attempt_id") != prior["attempt_id"] for result in expected_results
    ):
        _refuse("predecessor recovery must preserve the independently resolved original attempt")
    for result in expected_results:
        post = _operation_postcondition(result, observation, candidate, record_resolver)
        result["postcondition"] = post
        result["outcome"] = _operation_outcome(result["response"], post)
    if observation["operation_results"] != expected_results:
        _refuse("admission observation changed independent predecessor outcome history")
    classification = _resolve_stage_record(record["remote_classification"], record_resolver)
    _validate_candidate_identity(
        {"schema_version": "1.0", "candidate": classification.get("candidate")}
    )
    # Re-derive the complete closed record; a self-asserted ADMITTED is not authority.
    derived = classify_remote_observation(
        record["remote_observation"],
        classification_id=classification.get("classification_id"),
        classified_at=classification.get("classified_at"),
        current_workflow_run=workflow,
        record_resolver=record_resolver,
        **remote_binding,
    )
    if classification != derived or derived["admission"] != "ADMITTED":
        _refuse("promotion attempt remote classification is forged, stale, or collision")
    if not (
        stage_time
        <= _review_timestamp(observation["observed_at"], "remote observed_at")
        <= _review_timestamp(derived["classified_at"], "remote classified_at")
        <= recorded_at
    ):
        _refuse("promotion attempt remote facts are outside current admission chronology")
    return _freeze(record)


def seal_remote_verification(
    verification: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
    release_gate_catalog_bytes: bytes,
    release_gate_catalog_reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
    repository_root: str | PathLike[str],
    current_promotion_context: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Admit recorded pre/post byte proof and exact Attempt 3.0/Observation 2.0 lineage."""
    record = _expect_mapping(
        _thaw(verification),
        "remote verification",
        (
            "verification_schema_version",
            "verification_id",
            "promotion_attempt_id",
            "candidate_identity_record",
            "candidate",
            "promotion_decision",
            "promotion_attempt",
            "remote_observation",
            "remote_classification",
            "pre_publication_verification",
            "post_publication_verification",
            "remote_consumer_proof",
            "outcome",
            "verified_at",
        ),
    )
    if record["verification_schema_version"] != "1.0" or record["outcome"] != "PASS":
        _refuse("remote verification requires a passing supported receipt")
    _expect_safe_opaque_id(record["verification_id"], "remote verification ID")
    _expect_safe_opaque_id(record["promotion_attempt_id"], "remote verification attempt ID")
    verified_at = _review_timestamp(record["verified_at"], "remote verification verified_at")
    identity, identity_reference = _verified_raw_candidate(
        candidate_identity, candidate_identity_bytes, candidate_identity_reference
    )
    candidate = identity["candidate"]
    _validate_candidate_identity({"schema_version": "1.0", "candidate": record["candidate"]})
    if (
        _review_record_reference(record["candidate_identity_record"], "verification candidate")
        != identity_reference
        or record["candidate"] != candidate
    ):
        _refuse("remote verification does not bind the exact candidate")
    if not callable(record_resolver):
        _refuse("remote verification raw record resolver is unavailable")
    if _resolve_stage_record(
        identity_reference, record_resolver
    ) != identity or _resolve_stage_record(
        release_gate_catalog_reference, record_resolver
    ) != _thaw(release_gate_catalog):
        _refuse("remote verification candidate or catalog differs from its resolved raw record")
    stage_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
        "release_gate_catalog": release_gate_catalog,
        "release_gate_catalog_bytes": release_gate_catalog_bytes,
        "release_gate_catalog_reference": release_gate_catalog_reference,
        "record_resolver": record_resolver,
        "repository_root": repository_root,
    }
    attempt = seal_promotion_attempt(
        _resolve_stage_record(record["promotion_attempt"], record_resolver),
        current_promotion_context=_thaw(current_promotion_context),
        **stage_binding,
    )
    if (
        record["promotion_attempt_id"] != attempt["attempt_id"]
        or record["promotion_decision"] != attempt["promotion_decision"]
    ):
        _refuse("remote verification Decision or attempt differs from the admitted lineage")
    for field in (
        "promotion_decision",
        "promotion_attempt",
        "remote_observation",
        "remote_classification",
        "remote_consumer_proof",
    ):
        reference = _review_record_reference(record[field], f"verification {field}")
        if reference["repository"] != candidate["source"]["repository"] or (
            field in ("promotion_attempt", "remote_observation", "remote_classification")
            and reference["run_id"] != attempt["workflow_run"]["run_id"]
        ):
            _refuse("remote verification reference belongs to another repository or attempt run")
    remote_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
        "current_attempt_id": attempt["attempt_id"],
    }
    observation = seal_remote_observation(
        _resolve_stage_record(record["remote_observation"], record_resolver),
        record_resolver=record_resolver,
        **remote_binding,
    )
    classification = _resolve_stage_record(record["remote_classification"], record_resolver)
    _validate_candidate_identity(
        {"schema_version": "1.0", "candidate": classification.get("candidate")}
    )
    derived = classify_remote_observation(
        record["remote_observation"],
        classification_id=classification.get("classification_id"),
        classified_at=classification.get("classified_at"),
        current_workflow_run=attempt["workflow_run"],
        record_resolver=record_resolver,
        **remote_binding,
    )
    if (
        classification != derived
        or derived["admission"] != "ADMITTED"
        or derived["state"] != "published_complete"
    ):
        _refuse("remote verification requires honestly derived published_complete")
    consumer = _validate_stage_consumer_proof(
        _resolve_stage_record(record["remote_consumer_proof"], record_resolver),
        remote=True,
        candidate_identity=identity,
        candidate_identity_bytes=candidate_identity_bytes,
        candidate_identity_reference=candidate_identity_reference,
        release_gate_catalog=release_gate_catalog,
    )
    _validate_candidate_identity({"schema_version": "1.0", "candidate": consumer["candidate"]})
    # Expected identities are comparisons only; both actual fragments must already be present.
    expected = _promotion_fragment({"candidate": candidate}, observation)
    if consumer["download_origin"] != expected["release"]:
        _refuse("remote consumer release or asset IDs differ from the published observation")
    equations = (
        "archive_matches_candidate",
        "manifest_matches_candidate",
        "executable_matches_manifest",
        "inherited_verifier_equations_pass",
    )
    for field, phase in (
        ("pre_publication_verification", "PRE_PUBLICATION"),
        ("post_publication_verification", "POST_PUBLICATION"),
    ):
        fragment = _expect_mapping(record[field], f"remote verification {phase}", tuple(expected))
        release = _expect_mapping(
            fragment["release"], "verification release", tuple(expected["release"])
        )
        for kind in ("archive", "manifest"):
            asset = _expect_mapping(
                release[f"{kind}_asset"],
                "verification asset",
                ("id", "name", "size_bytes", "sha256"),
            )
            _expect_identifier(asset["id"], "verification asset ID")
            _validate_file_descriptor(
                {key: asset[key] for key in ("name", "size_bytes", "sha256")},
                "verification asset bytes",
            )
        _validate_file_descriptor(fragment["executable"], "verification executable")
        if (
            release["prerelease"] is not True
            or any(fragment[equation] is not True for equation in equations)
            or fragment != {**expected, "phase": phase}
        ):
            _refuse(
                "remote verification phase does not prove the exact passing published bytes/IDs"
            )
    observed_at = _review_timestamp(observation["observed_at"], "published observation observed_at")
    if not (
        _review_timestamp(attempt["recorded_at"], "attempt recorded_at")
        <= observed_at
        <= _review_timestamp(derived["classified_at"], "published classification classified_at")
        <= verified_at
        and observed_at
        <= _review_timestamp(consumer["recorded_at"], "remote consumer recorded_at")
        <= verified_at
    ):
        _refuse("remote verification proof is outside its attempt/publication chronology")
    return _freeze(record)


def seal_program_b_handoff(
    handoff: Mapping[str, Any],
    *,
    candidate_identity: Mapping[str, Any],
    candidate_identity_bytes: bytes,
    candidate_identity_reference: Mapping[str, Any],
    release_gate_catalog: Mapping[str, Any],
    release_gate_catalog_bytes: bytes,
    release_gate_catalog_reference: Mapping[str, Any],
    record_resolver: Callable[[Mapping[str, Any]], bytes],
    repository_root: str | PathLike[str],
    current_promotion_context: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Seal A1 completion from the complete proof graph, never Program B/C authorization."""
    record = _expect_mapping(
        _thaw(handoff),
        "Program B handoff",
        (
            "handoff_schema_version",
            "handoff_id",
            "producer",
            "candidate_identity_record",
            "candidate",
            "source_ref",
            "source_commit",
            "approved_promotion_decision",
            "promotion_attempt",
            "promotion_attempt_id",
            "published_remote_observation",
            "published_remote_classification",
            "remote_verification_receipt",
            "remote_consumer_proof",
            "release_gate_catalog",
            "pre_decision_stage_evidence",
            "pre_publication_stage_evidence",
            "post_publication_stage_evidence",
            "completion_scope",
            "program_b_authorization_required",
            "program_c_authorization_required",
            "recorded_at",
        ),
    )
    _expect_safe_opaque_id(record["handoff_id"], "Program B handoff ID")
    recorded_at = _review_timestamp(record["recorded_at"], "Program B handoff recorded_at")
    producer = _expect_mapping(record["producer"], "handoff producer", ("helper_path", "operation"))
    if (
        record["handoff_schema_version"] != "1.0"
        or producer
        != {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "seal_program_b_handoff",
        }
        or record["completion_scope"] != "A1_OPT_IN_PREVIEW_COMPLETE"
        or record["program_b_authorization_required"] is not True
        or record["program_c_authorization_required"] is not True
    ):
        _refuse("handoff must preserve the A1-only producer and separate Program B/C authorization")
    if not callable(record_resolver):
        _refuse("handoff raw record resolver is unavailable")
    stage_binding = {
        "candidate_identity": candidate_identity,
        "candidate_identity_bytes": candidate_identity_bytes,
        "candidate_identity_reference": candidate_identity_reference,
        "release_gate_catalog": release_gate_catalog,
        "release_gate_catalog_bytes": release_gate_catalog_bytes,
        "release_gate_catalog_reference": release_gate_catalog_reference,
        "record_resolver": record_resolver,
        "repository_root": repository_root,
    }
    verification = seal_remote_verification(
        _resolve_stage_record(record["remote_verification_receipt"], record_resolver),
        current_promotion_context=current_promotion_context,
        **stage_binding,
    )
    candidate = verification["candidate"]
    _validate_candidate_identity({"schema_version": "1.0", "candidate": record["candidate"]})
    for handoff_field, verification_field in (
        ("candidate_identity_record", "candidate_identity_record"),
        ("approved_promotion_decision", "promotion_decision"),
        ("promotion_attempt", "promotion_attempt"),
        ("published_remote_observation", "remote_observation"),
        ("published_remote_classification", "remote_classification"),
        ("remote_consumer_proof", "remote_consumer_proof"),
    ):
        if (
            _review_record_reference(record[handoff_field], f"handoff {handoff_field}")
            != verification[verification_field]
        ):
            _refuse("handoff predecessor differs from the verified publication lineage")
    if (
        record["candidate"] != candidate
        or record["source_ref"] != _CANONICAL_SOURCE_REF
        or record["source_commit"] != candidate["source"]["commit"]
        or record["promotion_attempt_id"] != verification["promotion_attempt_id"]
        or _review_record_reference(record["release_gate_catalog"], "handoff catalog")
        != release_gate_catalog_reference
        or record["remote_verification_receipt"]["repository"] != candidate["source"]["repository"]
        or _review_timestamp(verification["verified_at"], "remote verification verified_at")
        > recorded_at
    ):
        _refuse("handoff candidate, source, attempt, catalog, or chronology is inconsistent")
    attempt = _resolve_stage_record(record["promotion_attempt"], record_resolver)
    for name, stage_name in (
        ("pre_decision", "pre-decision"),
        ("pre_publication", "pre-publication"),
        ("post_publication", "post-publication"),
    ):
        reference = record[f"{name}_stage_evidence"]
        stage = seal_stage_gate_evidence(
            _resolve_stage_record(reference, record_resolver), **stage_binding
        )
        if (
            stage["stage"] != stage_name
            or stage["outcome"] != "PASS"
            or _review_timestamp(stage["sealed_at"], "handoff Stage sealed_at") > recorded_at
        ):
            _refuse("handoff requires all three exact passing catalog stages before closeout")
        if name != "post_publication":
            if reference != attempt[f"{name}_stage_evidence"]["record_reference"]:
                _refuse("handoff pre-publication lineage differs from the admitted Attempt stages")
        elif any(
            entry["record_reference"] != record["remote_consumer_proof"]
            for entry in stage["gate_evidence"]
        ):
            _refuse("handoff post-publication Stage does not bind its exact remote consumer proof")
    return _freeze(record)


def _read_cli_dispatch(command: str, environment: Mapping[str, str]) -> Mapping[str, Any]:
    event = _load_json_object(
        _read_regular_bytes(
            Path(_environment_value(environment, "GITHUB_EVENT_PATH")), "workflow dispatch event"
        ),
        "workflow dispatch event",
    )
    inputs = event.get("inputs")
    allowed = {
        "mode",
        "preview_version",
        "preview_tag",
        "retention_days",
        "evidence_operation",
        "evidence_request",
        "promote_request",
    }
    if (
        not isinstance(inputs, Mapping)
        or set(inputs) - allowed
        or any(not isinstance(value, str) for value in inputs.values())
    ):
        _refuse("workflow dispatch inputs are not closed strings")
    if len(json.dumps(inputs, ensure_ascii=True, separators=(",", ":"))) > 65_535:
        _refuse("workflow dispatch inputs exceed the total escaped input limit")
    repository = _expect_repository(
        _environment_value(environment, "GITHUB_REPOSITORY"), "workflow repository"
    )
    repository_id = _expect_environment_identifier(environment, "GITHUB_REPOSITORY_ID")
    event_repository = event.get("repository")
    if not isinstance(event_repository, Mapping):
        _refuse("workflow dispatch repository is unavailable")
    if (
        str(_expect_positive_integer(event_repository.get("id"), "event repository ID"))
        != repository_id
        or event_repository.get("full_name") != repository
        or event.get("ref") not in ("main", _CANONICAL_SOURCE_REF)
        or _environment_value(environment, "GITHUB_EVENT_NAME") != _TRUSTED_BUILD_EVENT
        or _environment_value(environment, "GITHUB_REF") != _CANONICAL_SOURCE_REF
        or _environment_value(environment, "GITHUB_WORKFLOW_REF")
        != f"{repository}/{_TRUSTED_WORKFLOW_PATH}@{_CANONICAL_SOURCE_REF}"
        or _expect_commit(_environment_value(environment, "GITHUB_WORKFLOW_SHA"), "workflow SHA")
        != _expect_commit(_environment_value(environment, "GITHUB_SHA"), "source SHA")
    ):
        _refuse("workflow dispatch does not bind the canonical workflow context")
    mode = inputs.get("mode", "build")
    build_commands = ("admit-build", "prepare-payload", "seal-build-records")
    promote_commands = (
        "admit-promote",
        "observe-promote",
        "classify-promote",
        "seal-promote-attempt",
        "advance-promote",
        "finish-promote",
    )
    if mode == "build":
        if command not in build_commands or any(
            inputs.get(key, "")
            for key in ("evidence_operation", "evidence_request", "promote_request")
        ):
            _refuse("build dispatch cannot execute or accept evidence/promotion inputs")
        for key, variable in (
            ("preview_version", "A1_PREVIEW_VERSION"),
            ("preview_tag", "A1_PREVIEW_TAG"),
            ("retention_days", "A1_RETENTION_DAYS"),
        ):
            if not inputs.get(key) or inputs[key] != _environment_value(environment, variable):
                _refuse("build dispatch inputs differ from the runner inputs")
    elif mode == "evidence":
        evidence_commands = {
            "retain": (
                "admit-evidence",
                "seal-evidence-stage",
                "seal-evidence-decision",
                "verify-evidence-retention",
            ),
            "closeout": _CLOSEOUT_COMMANDS,
        }
        operation = inputs.get("evidence_operation")
        if (
            command not in evidence_commands.get(operation, ())
            or any(
                inputs.get(key, "")
                for key in ("preview_version", "preview_tag", "retention_days", "promote_request")
            )
            or not inputs.get("evidence_request")
        ):
            _refuse("evidence dispatch operation, command, or request is unsupported")
    elif mode == "promote":
        if (
            command not in promote_commands
            or any(
                inputs.get(key, "")
                for key in (
                    "preview_version",
                    "retention_days",
                    "evidence_operation",
                    "evidence_request",
                )
            )
            or not inputs.get("promote_request")
            or not inputs.get("preview_tag")
        ):
            _refuse("promotion requires only its closed Decision request and destination tag")
    else:
        _refuse("workflow dispatch mode is unsupported")
    return event


def _validate_cli_run(
    run: Mapping[str, Any],
    context: Mapping[str, Any],
    run_id: str,
    attempt: int,
    *,
    successful_build: bool,
    completed_run: bool = False,
) -> None:
    source = context["source"]
    if (
        str(_expect_positive_integer(run.get("id"), "workflow run ID")) != run_id
        or _expect_positive_integer(run.get("run_attempt"), "workflow run attempt") != attempt
        or run.get("event") != _TRUSTED_BUILD_EVENT
        or run.get("path") != _TRUSTED_WORKFLOW_PATH
        or run.get("head_branch") != "main"
        or run.get("head_sha") != source["commit"]
    ):
        _refuse("workflow run does not bind the exact canonical source and attempt")
    for field in ("repository", "head_repository"):
        repository = run.get(field)
        if not isinstance(repository, Mapping):
            _refuse("workflow run repository is unavailable")
        if (
            str(_expect_positive_integer(repository.get("id"), "workflow repository ID"))
            != context["repository_id"]
            or repository.get("full_name") != source["repository"]
        ):
            _refuse("workflow run belongs to a different repository or fork")
    if completed_run:
        if run.get("status") != "completed" or not isinstance(run.get("conclusion"), str):
            _refuse("predecessor run is not conclusively finished")
    elif successful_build:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            _refuse("source build has not completed successfully")
    elif run.get("status") != "in_progress" or run.get("conclusion") is not None:
        _refuse("evidence run is not the current unfinished run")


def _evidence_context(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> dict[str, Any]:
    context = {
        "source": _canonical_build_source(root, environment),
        "repository_id": _expect_environment_identifier(environment, "GITHUB_REPOSITORY_ID"),
        "run_id": _expect_environment_identifier(environment, "GITHUB_RUN_ID"),
        "attempt": int(_expect_environment_identifier(environment, "GITHUB_RUN_ATTEMPT")),
    }
    repository = context["source"]["repository"]
    run = _load_json_object(
        ArtifactDownloader(root)._run_gh(
            ["api", f"repos/{repository}/actions/runs/{context['run_id']}"],
            "current evidence workflow run",
        ),
        "current evidence workflow run",
    )
    _validate_cli_run(run, context, context["run_id"], context["attempt"], successful_build=False)
    principal = {"id": "7106373", "login": "thebtf"}
    for field in ("actor", "triggering_actor"):
        actor = run.get(field)
        if not isinstance(actor, Mapping):
            _refuse("evidence run actor is unavailable")
        observed = {
            "id": str(_expect_positive_integer(actor.get("id"), "authenticated actor ID")),
            "login": actor.get("login"),
        }
        _decision_actor(observed, field)
        if observed != principal:
            _refuse("evidence author or rerun initiator is not authorized")
    sender = event.get("sender")
    if not isinstance(sender, Mapping):
        _refuse("evidence event sender is unavailable")
    if (
        str(_expect_positive_integer(sender.get("id"), "event sender ID")) != principal["id"]
        or sender.get("login") != principal["login"]
        or _expect_environment_identifier(environment, "GITHUB_ACTOR_ID") != principal["id"]
        or _environment_value(environment, "GITHUB_ACTOR") != principal["login"]
        or _environment_value(environment, "GITHUB_TRIGGERING_ACTOR") != principal["login"]
    ):
        _refuse("evidence actor differs from the actual event or runner context")
    if _environment_value(environment, "GITHUB_JOB") != "retain_evidence":
        _refuse("evidence command is running in a different job")
    context["actor"] = principal
    return context


def _evidence_request(event: Mapping[str, Any]) -> Mapping[str, Any]:
    request = _expect_mapping(
        _load_json_object(event["inputs"]["evidence_request"].encode("utf-8"), "evidence request"),
        "evidence request",
        (
            "candidate_identity_reference",
            "release_gate_catalog_reference",
            "bundle_base64",
            "record_sha256",
            "decision",
            "authorized_dispatcher",
            "retention_days",
        ),
    )
    for field, path in (
        ("candidate_identity_reference", _CANDIDATE_IDENTITY_FILENAME),
        ("release_gate_catalog_reference", _RELEASE_GATE_CATALOG_FILENAME),
    ):
        reference = ArtifactFileReference.from_mapping(request[field], field)
        if reference.path != path or reference.retention_days > _MAX_ACTIONS_RETENTION_DAYS:
            _refuse("evidence source reference has an unexpected path or retention")
    _expect_string(request["bundle_base64"], "evidence bundle")
    _expect_retention_days({"A1_RETENTION_DAYS": request["retention_days"]})
    if request["decision"] not in ("APPROVE", "DECLINE"):
        _refuse("evidence request requires an explicit decision")
    _decision_actor(request["authorized_dispatcher"], "authorized_dispatcher")
    return request


def _evidence_locations(
    environment: Mapping[str, str], context: Mapping[str, Any]
) -> dict[str, str]:
    try:
        temporary = Path(_environment_value(environment, "RUNNER_TEMP")).resolve(strict=True)
        if not temporary.is_dir():
            _refuse("runner temporary directory is unavailable")
        suffix = f"{context['run_id']}-{context['attempt']}"
        directory = temporary / f"stateless-preview-evidence-{suffix}"
        paths = {
            "records_directory": directory / "records",
            "stage_path": directory / "pre-decision-stage.json",
            "decision_path": directory / "promotion-decision.json",
        }
        if directory.is_symlink() or any(
            path.is_symlink() or not _path_within(path, temporary) for path in paths.values()
        ):
            _refuse("evidence destination escapes the runner temporary directory")
    except (OSError, RuntimeError):
        _refuse("runner temporary directory is unavailable")
    return {
        **{key: str(path) for key, path in paths.items()},
        "records_artifact_name": f"stateless-preview-evidence-records-{suffix}",
        "stage_artifact_name": f"stateless-preview-evidence-stage-{suffix}",
        "decision_artifact_name": f"stateless-preview-evidence-decision-{suffix}",
    }


def _compact_evidence_reference(reference: ArtifactFileReference) -> dict[str, str]:
    return {
        "repository": reference.repository,
        "run_id": reference.run_id,
        "artifact_id": reference.artifact_id,
        "path": reference.path,
        "sha256": reference.sha256,
    }


def _evidence_reference_key(reference: Mapping[str, Any]) -> tuple[str, ...]:
    value = _review_record_reference(reference, "retained evidence reference")
    return tuple(value[key] for key in ("repository", "run_id", "artifact_id", "path", "sha256"))


def _checked_evidence_artifact(
    downloader: ArtifactDownloader,
    context: Mapping[str, Any],
    artifact_id: str,
    run_id: str,
    expected_name: str,
    retention_days: int,
    expected_digest: str | None,
) -> Mapping[str, Any]:
    _expect_identifier(artifact_id, "evidence artifact ID")
    _expect_identifier(run_id, "evidence artifact run ID")
    source = context["source"]
    metadata = _load_json_object(
        downloader._run_gh(
            ["api", f"repos/{source['repository']}/actions/artifacts/{artifact_id}"],
            "evidence artifact metadata",
        ),
        "evidence artifact metadata",
    )
    admitted = _validate_uploaded_artifact(
        metadata, artifact_id=artifact_id, expected_name=expected_name, current_run_id=run_id
    )
    workflow_run = metadata["workflow_run"]
    if (
        str(_expect_positive_integer(metadata.get("id"), "uploaded artifact ID")) != artifact_id
        or str(_expect_positive_integer(workflow_run.get("id"), "artifact workflow run ID"))
        != run_id
        or str(
            _expect_positive_integer(workflow_run.get("repository_id"), "artifact repository ID")
        )
        != context["repository_id"]
        or str(
            _expect_positive_integer(
                workflow_run.get("head_repository_id"), "artifact head repository ID"
            )
        )
        != context["repository_id"]
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != source["commit"]
        or _review_timestamp(admitted["expires_at"], "artifact expiry")
        <= datetime.now(timezone.utc)
    ):
        _refuse("evidence artifact has expired or has different source provenance")
    digest = metadata.get("digest")
    if expected_digest is not None:
        expected_digest = _expect_sha256(expected_digest, "uploaded artifact container digest")
        if digest is not None and digest != f"sha256:{expected_digest}":
            _refuse("uploaded artifact container digest differs from metadata")
    else:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            _refuse("ancestor artifact container digest is unavailable")
        expected_digest = _expect_sha256(digest[7:], "ancestor artifact container digest")
    return {
        "id": artifact_id,
        "name": expected_name,
        "sha256": expected_digest,
        "retention": {"configured_days": retention_days, "expires_at": admitted["expires_at"]},
    }


def _verified_evidence_build(
    downloader: ArtifactDownloader, context: Mapping[str, Any], candidate: Mapping[str, Any]
) -> datetime:
    build = candidate["build"]
    repository = context["source"]["repository"]
    run_id, attempt = build["run_id"], build["run_attempt"]
    if run_id == context["run_id"]:
        _refuse("evidence cannot consume its own unfinished source build")
    run = _load_json_object(
        downloader._run_gh(
            ["api", f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"],
            "source build run",
        ),
        "source build run",
    )
    _validate_cli_run(run, context, run_id, attempt, successful_build=True)
    jobs = _load_json_object(
        downloader._run_gh(
            [
                "api",
                f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
            ],
            "source build jobs",
        ),
        "source build jobs",
    )
    entries = _expect_list(jobs.get("jobs"), "source build jobs")
    if _expect_nonnegative_integer(jobs.get("total_count"), "source build job count") != len(
        entries
    ):
        _refuse("source build job readback is incomplete")
    if any(not isinstance(job, Mapping) for job in entries):
        _refuse("source build job is invalid")
    matches = [
        job for job in entries if job.get("name") == "Build canonical-main preview candidate"
    ]
    if len(matches) != 1:
        _refuse("source build job is absent or ambiguous")
    job = matches[0]
    _expect_positive_integer(job.get("id"), "source build job ID")
    if (
        str(_expect_positive_integer(job.get("run_id"), "source build job run ID")) != run_id
        or _expect_positive_integer(job.get("run_attempt"), "source build job attempt") != attempt
        or job.get("head_sha") != context["source"]["commit"]
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
    ):
        _refuse("source build job does not prove the successful exact build attempt")
    return _review_timestamp(job.get("completed_at"), "source build completed_at")


def _evidence_inputs(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any], command: str
) -> dict[str, Any]:
    context = _evidence_context(root, environment, event)
    request = _evidence_request(event)
    records = decode_evidence_bundle(request["bundle_base64"], request["record_sha256"])
    locations = _evidence_locations(environment, context)
    locations["retention_days"] = request["retention_days"]
    download_directory = Path(locations["stage_path"]).parent / command
    try:
        if download_directory.is_symlink():
            _refuse("evidence download directory is not fresh")
        download_directory.mkdir(parents=True, exist_ok=False)
    except OSError:
        _refuse("evidence download directory is unavailable")
    downloader = ArtifactDownloader(download_directory)
    identity_reference = ArtifactFileReference.from_mapping(
        request["candidate_identity_reference"], "candidate identity reference"
    )
    catalog_reference = ArtifactFileReference.from_mapping(
        request["release_gate_catalog_reference"], "release gate catalog reference"
    )
    if (
        identity_reference.repository != context["source"]["repository"]
        or catalog_reference.repository != identity_reference.repository
        or catalog_reference.run_id != identity_reference.run_id
        or catalog_reference.artifact_id == identity_reference.artifact_id
    ):
        _refuse("evidence source references do not bind distinct artifacts of one build")
    artifact = _checked_evidence_artifact(
        downloader,
        context,
        identity_reference.artifact_id,
        identity_reference.run_id,
        identity_reference.artifact_name,
        identity_reference.retention_days,
        identity_reference.artifact_sha256,
    )
    if artifact != identity_reference.as_mapping()["artifact"]:
        _refuse("candidate identity artifact metadata differs from the request")
    identity_bytes = _read_regular_bytes(
        downloader.download_file(identity_reference), "downloaded candidate identity"
    )
    identity, _ = _verified_review_candidate(
        _load_json_object(identity_bytes, "candidate identity"),
        identity_bytes,
        identity_reference.as_mapping(),
    )
    candidate = identity["candidate"]
    if (
        any(candidate["source"][key] != value for key, value in context["source"].items())
        or candidate["build"]["run_id"] != identity_reference.run_id
    ):
        _refuse("evidence candidate differs from the exact canonical source build")
    names = _artifact_names(
        candidate["preview_manifest"]["contents"]["version"],
        candidate["build"]["run_id"],
        candidate["build"]["run_attempt"],
    )
    if (
        identity_reference.artifact_name != names["candidate_identity"]
        or catalog_reference.artifact_name != names["release_gate_catalog"]
        or candidate["build"]["artifact"]["name"] != names["payload"]
    ):
        _refuse("evidence source artifact names do not bind the build attempt")
    completed_at = _verified_evidence_build(downloader, context, candidate)
    artifact = _checked_evidence_artifact(
        downloader,
        context,
        catalog_reference.artifact_id,
        catalog_reference.run_id,
        names["release_gate_catalog"],
        catalog_reference.retention_days,
        catalog_reference.artifact_sha256,
    )
    if artifact != catalog_reference.as_mapping()["artifact"]:
        _refuse("release gate catalog artifact metadata differs from the request")
    catalog_bytes = _read_regular_bytes(
        downloader.download_file(catalog_reference), "downloaded release gate catalog"
    )
    catalog = _verified_release_gate_catalog(
        _load_json_object(catalog_bytes, "release gate catalog"),
        catalog_bytes,
        _compact_evidence_reference(catalog_reference),
        candidate,
        root,
    )
    review_binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": identity_bytes,
        "candidate_identity_reference": identity_reference.as_mapping(),
    }
    admitted = {
        "consumer-proof.json": seal_artifact_consumer_proof(
            _load_json_object(records["consumer-proof.json"], "consumer proof"),
            release_gate_catalog=catalog,
            **review_binding,
        ),
        "s2-s3-review.json": seal_s2_s3_review_aggregate(
            _load_json_object(records["s2-s3-review.json"], "S2/S3 review"),
            **review_binding,
        ),
        "independent-pr-review.json": seal_independent_pr_review(
            _load_json_object(records["independent-pr-review.json"], "independent PR review"),
            **review_binding,
        ),
    }
    for name, record in admitted.items():
        timestamps = [record["sealed_at" if name == "s2-s3-review.json" else "recorded_at"]]
        if name == "s2-s3-review.json":
            timestamps.extend(lens["recorded_at"] for lens in record["lens_results"])
        for timestamp in timestamps:
            if (
                not completed_at
                <= _review_timestamp(timestamp, "evidence record time")
                <= datetime.now(timezone.utc)
            ):
                _refuse("evidence record does not follow the completed source build")
    resolved: dict[tuple[str, ...], bytes] = {}
    return {
        "context": context,
        "request": request,
        "locations": locations,
        "downloader": downloader,
        "records": records,
        "admitted": admitted,
        "resolved": resolved,
        "binding": {
            **review_binding,
            "release_gate_catalog": catalog,
            "release_gate_catalog_bytes": catalog_bytes,
            "release_gate_catalog_reference": _compact_evidence_reference(catalog_reference),
            "record_resolver": lambda reference: resolved[_evidence_reference_key(reference)],
            "repository_root": root,
        },
    }


def _evidence_upload_metadata(
    inputs: Mapping[str, Any], environment: Mapping[str, str], kind: str, artifact_id: str
) -> Mapping[str, Any]:
    prefix = f"A1_EVIDENCE_{kind.upper()}_ARTIFACT"
    uploaded_id = environment.get(f"{prefix}_ID")
    if uploaded_id is not None and uploaded_id != artifact_id:
        _refuse("retained ancestor artifact differs from the upload output")
    context = inputs["context"]
    return _checked_evidence_artifact(
        inputs["downloader"],
        context,
        artifact_id,
        context["run_id"],
        inputs["locations"][f"{kind}_artifact_name"],
        int(inputs["request"]["retention_days"]),
        environment.get(f"{prefix}_DIGEST"),
    )


def _read_evidence_records(
    inputs: Mapping[str, Any],
    environment: Mapping[str, str],
    references: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, str]]:
    context = inputs["context"]
    if references is None:
        artifact_id = _expect_environment_identifier(environment, "A1_EVIDENCE_RECORDS_ARTIFACT_ID")
    else:
        if len(references) != len(_EVIDENCE_BUNDLE_MEMBERS):
            _refuse("retained evidence predecessor set is incomplete")
        artifact_id = _review_record_reference(references[0], "retained evidence predecessor")[
            "artifact_id"
        ]
    expected = [
        {
            "repository": context["source"]["repository"],
            "run_id": context["run_id"],
            "artifact_id": artifact_id,
            "path": name,
            "sha256": inputs["request"]["record_sha256"][name],
        }
        for name in _EVIDENCE_BUNDLE_MEMBERS
    ]
    if (
        references is not None
        and [
            _review_record_reference(reference, "retained evidence predecessor")
            for reference in references
        ]
        != expected
    ):
        _refuse("retained evidence references differ from the exact requested original records")
    artifact = _evidence_upload_metadata(inputs, environment, "records", artifact_id)
    for reference in expected:
        expanded = ArtifactFileReference.from_mapping(
            {
                **{key: reference[key] for key in ("repository", "run_id", "path", "sha256")},
                "artifact": artifact,
            },
            "retained evidence record",
        )
        raw = _read_regular_bytes(
            inputs["downloader"].download_file(expanded), "retained evidence record"
        )
        if raw != inputs["records"][reference["path"]]:
            _refuse("retained evidence differs from the original byte sequence")
        inputs["resolved"][_evidence_reference_key(reference)] = raw
    return expected


def _read_evidence_stage(
    inputs: Mapping[str, Any],
    environment: Mapping[str, str],
    reference: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    context = inputs["context"]
    if reference is None:
        artifact_id = _expect_environment_identifier(environment, "A1_EVIDENCE_STAGE_ARTIFACT_ID")
    else:
        reference = _review_record_reference(reference, "retained stage predecessor")
        if (
            reference["repository"] != context["source"]["repository"]
            or reference["run_id"] != context["run_id"]
            or reference["path"] != "pre-decision-stage.json"
        ):
            _refuse("retained stage predecessor has different provenance or path")
        artifact_id = reference["artifact_id"]
    artifact = _evidence_upload_metadata(inputs, environment, "stage", artifact_id)
    actual, raw = inputs["downloader"]._download_record(
        context["source"]["repository"], context["run_id"], artifact, "pre-decision-stage.json"
    )
    compact = _compact_evidence_reference(actual)
    if reference is not None and reference != compact:
        _refuse("retained stage predecessor bytes differ from the sealed reference")
    stage = _load_json_object(raw, "retained pre-decision stage")
    entries = _expect_list(stage.get("gate_evidence"), "retained pre-decision gate entries")
    if any(not isinstance(entry, Mapping) for entry in entries):
        _refuse("retained pre-decision gate entry is invalid")
    _read_evidence_records(
        inputs, environment, [entry.get("record_reference") for entry in entries]
    )
    sealed = seal_stage_gate_evidence(stage, **inputs["binding"])
    if sealed["stage"] != "pre-decision" or sealed["outcome"] != "PASS":
        _refuse("retained stage is not passing pre-decision evidence")
    inputs["resolved"][_evidence_reference_key(compact)] = raw
    return sealed, compact


def _admit_evidence(root: Path, environment: Mapping[str, str], event: Mapping[str, Any]) -> None:
    inputs = _evidence_inputs(root, environment, event, "admit-evidence")
    directory = Path(inputs["locations"]["records_directory"])
    try:
        directory.mkdir(exist_ok=False)
    except OSError:
        _refuse("original evidence destination is unavailable")
    for name, raw in inputs["records"].items():
        _write_bytes_once(directory / name, raw, "original evidence record")
    _write_action_outputs(inputs["locations"], environment)


def _seal_evidence_stage(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> None:
    inputs = _evidence_inputs(root, environment, event, "seal-evidence-stage")
    references = _read_evidence_records(inputs, environment)
    binding = inputs["binding"]
    candidate = binding["candidate_identity"]["candidate"]
    identity_reference = binding["candidate_identity_reference"]
    identity_hash = identity_reference["sha256"]
    descriptors = [
        descriptor
        for descriptor in binding["release_gate_catalog"]["catalog"]["gate_descriptors"]
        if descriptor["stage"] == "pre-decision"
    ]
    entries = [
        {
            **descriptor,
            "record_reference": reference,
            "candidate_identity_record_sha256": identity_hash,
            "source_commit": candidate["source"]["commit"],
            "outcome": inputs["admitted"][reference["path"]]["outcome"],
        }
        for descriptor, reference in zip(descriptors, references)
    ]
    context = inputs["context"]
    stage = seal_stage_gate_evidence(
        {
            "evidence_schema_version": "1.0",
            "evidence_set_id": f"a1-pre-decision-{context['run_id']}-{context['attempt']}",
            "producer": {
                "helper_path": "scripts/stateless_preview_artifact.py",
                "operation": "seal_stage_gate_evidence",
            },
            "release_gate_catalog": binding["release_gate_catalog_reference"],
            "candidate_identity_record": {
                "repository": identity_reference["repository"],
                "run_id": identity_reference["run_id"],
                "artifact_id": identity_reference["artifact"]["id"],
                "path": identity_reference["path"],
                "sha256": identity_hash,
            },
            "candidate_identity_record_sha256": identity_hash,
            "candidate": candidate,
            "source_ref": candidate["source"]["ref"],
            "source_commit": candidate["source"]["commit"],
            "stage": "pre-decision",
            "gate_evidence": entries,
            "outcome": "PASS" if all(entry["outcome"] == "PASS" for entry in entries) else "FAIL",
            "sealed_at": _utc_timestamp(),
        },
        **binding,
    )
    _write_bytes_once(
        Path(inputs["locations"]["stage_path"]),
        _canonical_json_bytes(stage, "stage evidence"),
        "stage evidence",
    )


def _seal_evidence_decision(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> None:
    inputs = _evidence_inputs(root, environment, event, "seal-evidence-decision")
    stage, stage_reference = _read_evidence_stage(inputs, environment)
    binding = inputs["binding"]
    identity_reference = ArtifactFileReference.from_mapping(
        binding["candidate_identity_reference"], "candidate identity reference"
    )
    decision = seal_promotion_decision(
        {
            "schema_version": "1.0",
            "candidate_identity_record": _compact_evidence_reference(identity_reference),
            "candidate": binding["candidate_identity"]["candidate"],
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
                },
            },
            "decision": inputs["request"]["decision"],
            "promotion_attempt": {
                "decision_author": inputs["context"]["actor"],
                "authorized_dispatcher": inputs["request"]["authorized_dispatcher"],
            },
            "decided_at": _utc_timestamp(),
        },
        **binding,
    )
    _write_bytes_once(
        Path(inputs["locations"]["decision_path"]),
        _canonical_json_bytes(decision, "decision"),
        "promotion decision",
    )


def _verify_evidence_retention(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> None:
    inputs = _evidence_inputs(root, environment, event, "verify-evidence-retention")
    artifact_id = _expect_environment_identifier(environment, "A1_EVIDENCE_DECISION_ARTIFACT_ID")
    artifact = _evidence_upload_metadata(inputs, environment, "decision", artifact_id)
    context = inputs["context"]
    actual, raw = inputs["downloader"]._download_record(
        context["source"]["repository"], context["run_id"], artifact, "promotion-decision.json"
    )
    decision = _load_json_object(raw, "retained promotion decision")
    if decision.get("decision") != inputs["request"]["decision"] or decision.get(
        "promotion_attempt"
    ) != {
        "decision_author": context["actor"],
        "authorized_dispatcher": inputs["request"]["authorized_dispatcher"],
    }:
        _refuse("retained decision differs from the explicit request or authenticated author")
    evidence = _expect_mapping(
        decision.get("evidence"), "retained decision evidence", ("pre_decision_stage_evidence",)
    )
    summary = _expect_mapping(
        evidence["pre_decision_stage_evidence"],
        "retained decision stage summary",
        (
            "record_reference",
            "candidate_identity_record_sha256",
            "source_ref",
            "source_commit",
            "stage",
            "outcome",
        ),
    )
    _read_evidence_stage(inputs, environment, summary["record_reference"])
    seal_promotion_decision(decision, **inputs["binding"])
    compact = _compact_evidence_reference(actual)
    try:
        with Path(_environment_value(environment, "GITHUB_STEP_SUMMARY")).open(
            "a", encoding="utf-8", newline="\n"
        ) as output:
            output.write("Retained A1 promotion decision (not publication):\n\n```json\n")
            output.write(
                _canonical_json_bytes(compact, "retained decision reference").decode("utf-8")
            )
            output.write("```\n")
    except OSError:
        _refuse("GitHub Actions summary file is unavailable")


_CLOSEOUT_COMMANDS = (
    "admit-closeout",
    "seal-closeout-verification",
    "seal-closeout-stage",
    "seal-closeout-handoff",
    "verify-closeout-retention",
)
_CLOSEOUT_FILES = {
    "records": {
        "remote_consumer_proof_path": "remote-consumer-proof.json",
        "post_publication_verification_path": "post-publication-verification.json",
    },
    "verification": {"remote_verification_path": "remote-verification.json"},
    "stage": {"post_publication_stage_path": "post-publication-stage.json"},
    "handoff": {"program_b_handoff_path": "program-b-handoff.json"},
}


def _closeout_context(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> dict[str, Any]:
    context = {
        "source": _canonical_build_source(root, environment),
        "repository_id": _expect_environment_identifier(environment, "GITHUB_REPOSITORY_ID"),
        "run_id": _expect_environment_identifier(environment, "GITHUB_RUN_ID"),
        "attempt": int(_expect_environment_identifier(environment, "GITHUB_RUN_ATTEMPT")),
    }
    if (
        _expect_commit(
            _environment_value(environment, "GITHUB_WORKFLOW_SHA"), "closeout workflow SHA"
        )
        != context["source"]["commit"]
    ):
        _refuse("closeout workflow differs from the canonical source")
    reads = {"root": root, "environment": environment, "context": context, "diagnostics": []}
    live = _promotion_api(reads, "git/ref/heads/main")
    if (
        not isinstance(live, Mapping)
        or live.get("ref") != _CANONICAL_SOURCE_REF
        or not isinstance(live.get("object"), Mapping)
        or live["object"].get("type") != "commit"
        or live["object"].get("sha") != context["source"]["commit"]
    ):
        _refuse("closeout requires the exact live canonical main")
    run = _promotion_api(reads, f"actions/runs/{context['run_id']}/attempts/{context['attempt']}")
    _validate_cli_run(run, context, context["run_id"], context["attempt"], successful_build=False)
    actors = {}
    for role in ("actor", "triggering_actor"):
        value = run.get(role)
        if not isinstance(value, Mapping):
            _refuse("closeout initiator is unavailable")
        actors[role] = {
            "id": str(_expect_positive_integer(value.get("id"), "closeout actor ID")),
            "login": value.get("login"),
        }
        _decision_actor(actors[role], role)
    sender = event.get("sender")
    if (
        not isinstance(sender, Mapping)
        or {
            "id": str(_expect_positive_integer(sender.get("id"), "closeout sender ID")),
            "login": sender.get("login"),
        }
        != actors["actor"]
        or _expect_environment_identifier(environment, "GITHUB_ACTOR_ID") != actors["actor"]["id"]
        or _environment_value(environment, "GITHUB_ACTOR") != actors["actor"]["login"]
        or _environment_value(environment, "GITHUB_TRIGGERING_ACTOR")
        != actors["triggering_actor"]["login"]
        or _environment_value(environment, "GITHUB_JOB") != "retain_closeout"
    ):
        _refuse("closeout actors or job differ from the actual event/runner")
    selected = _select_current_job(
        _promotion_api(
            reads,
            f"actions/runs/{context['run_id']}/attempts/{context['attempt']}/jobs?per_page=100",
        ),
        repository=context["source"]["repository"],
        run_id=int(context["run_id"]),
        run_attempt=context["attempt"],
        head_sha=context["source"]["commit"],
        job_name="Retain canonical-main preview closeout",
    )
    context.update(
        actors=actors,
        job_id=str(selected["job_id"]),
        created_at=_expect_datetime(run.get("created_at"), "closeout run creation"),
    )
    if _review_timestamp(context["created_at"], "closeout run creation") > datetime.now(
        timezone.utc
    ):
        _refuse("closeout run creation is in the future")
    return context


def _closeout_publisher(
    inputs: MutableMapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, Any]:
    artifact = _promotion_artifact(inputs, reference)
    match = re.fullmatch(
        rf"stateless-preview-promote-attempt-{reference['run_id']}-([1-9][0-9]*)-([1-9][0-9]*)-handoff",
        artifact["name"],
    )
    if match is None or reference["run_id"] == inputs["closeout_context"]["run_id"]:
        _refuse("closeout requires a distinct historical publisher handoff")
    attempt, job_id = int(match[1]), match[2]
    run_id = reference["run_id"]
    run = inputs["producers"][(run_id, attempt)]
    _validate_cli_run(run, inputs["context"], run_id, attempt, successful_build=True)
    jobs = _promotion_collection(inputs, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    matches = [job for job in jobs if job.get("name") == _PROMOTION_JOB_NAME]
    if len(matches) != 1:
        _refuse("historical publisher job is absent or ambiguous")
    job = matches[0]
    repository = inputs["context"]["source"]["repository"]
    source = inputs["context"]["source"]
    if (
        str(_expect_positive_integer(job.get("id"), "publisher job ID")) != job_id
        or job.get("run_id") != int(run_id)
        or job.get("run_attempt") != attempt
        or job.get("head_sha") != source["commit"]
        or job.get("url") != f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}"
        or job.get("run_url") != f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
    ):
        _refuse("publisher handoff does not bind its successful exact job")
    steps = _expect_list(job.get("steps"), "publisher job steps")
    if any(not isinstance(step, Mapping) for step in steps):
        _refuse("publisher step evidence is incomplete")
    for name in (
        "Set up job",
        "Read back A and the last retained classification for the exact handoff",
        "Retain exact published references for separate remote consumer proof",
    ):
        selected = [step for step in steps if step.get("name") == name]
        if (
            len(selected) != 1
            or selected[0].get("status") != "completed"
            or selected[0].get("conclusion") != "success"
            or (name == "Set up job" and steps[0] is not selected[0])
        ):
            _refuse("publisher finish or handoff retention did not complete successfully")
    completed_at = _review_timestamp(job.get("completed_at"), "publisher completion")
    if not (
        _review_timestamp(run.get("created_at"), "publisher creation")
        <= completed_at
        <= _review_timestamp(inputs["closeout_context"]["created_at"], "closeout creation")
        <= datetime.now(timezone.utc)
    ):
        _refuse("publisher does not precede the actual closeout run")
    actors = {}
    for role in ("actor", "triggering_actor"):
        value = run.get(role)
        if not isinstance(value, Mapping):
            _refuse("historical publisher initiator is unavailable")
        actors[role] = {
            "id": str(_expect_positive_integer(value.get("id"), "publisher actor ID")),
            "login": value.get("login"),
        }
        _decision_actor(actors[role], role)
    context = {
        "source": source,
        "repository_id": inputs["context"]["repository_id"],
        "run_id": run_id,
        "attempt": attempt,
        "publisher_attempt_created_at": run["created_at"],
        "current": {
            "current_attempt_id": f"a1-promote-{run_id}-{attempt}-{job_id}",
            "workflow_run": {
                "repository": repository,
                "workflow_path": _TRUSTED_WORKFLOW_PATH,
                "mode": "promote",
                "event": _TRUSTED_BUILD_EVENT,
                "run_id": run_id,
                "run_attempt": attempt,
                "ref": _CANONICAL_SOURCE_REF,
                "commit": source["commit"],
            },
            "job_id": job_id,
            "job_key": "promote",
            "workflow_sha": source["commit"],
            "checkout_head": source["commit"],
            "origin_main_target": source["commit"],
            "current_github_actor": actors["actor"],
            "current_github_triggering_actor": actors["triggering_actor"],
        },
    }
    inputs["publisher_job"] = job
    inputs["publisher_completed_at"] = completed_at
    return context


def _closeout_resolve(inputs: Mapping[str, Any], reference: Mapping[str, Any]) -> bytes:
    reference = _review_record_reference(reference, "closeout retained reference")
    if reference["run_id"] == inputs["closeout_context"]["run_id"]:
        for kind in _CLOSEOUT_FILES:
            prefix = f"A1_CLOSEOUT_{kind.upper()}_ARTIFACT"
            if reference["artifact_id"] == inputs["environment"].get(f"{prefix}_ID"):
                records = _closeout_uploaded(inputs, kind)
                retained, raw = records.get(reference["path"], (None, None))
                if retained != reference:
                    _refuse("closeout reference differs from its actual uploaded bytes")
                return raw
        _refuse("closeout reference has no actual upload output")
    return _promotion_resolve(inputs, reference)


def _closeout_inputs(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any], command: str
) -> dict[str, Any]:
    request = _expect_mapping(
        _load_json_object(event["inputs"]["evidence_request"].encode("utf-8"), "closeout request"),
        "closeout request",
        ("publisher_handoff_reference", "bundle_base64", "record_sha256"),
    )
    reference = _review_record_reference(
        request["publisher_handoff_reference"], "publisher handoff"
    )
    if reference["path"] != "publisher-handoff.json":
        _refuse("closeout request must select publisher-handoff.json")
    records = _decode_evidence_bundle(
        request["bundle_base64"], request["record_sha256"], ("remote-consumer-proof.json",)
    )
    context = _closeout_context(root, environment, event)
    temporary = Path(_environment_value(environment, "RUNNER_TEMP")).resolve(strict=True)
    suffix = f"{context['run_id']}-{context['attempt']}-{context['job_id']}"
    directory = temporary / f"stateless-preview-closeout-{suffix}" / command
    if not temporary.is_dir() or not _path_within(directory, temporary):
        _refuse("closeout destination escapes the runner temporary directory")
    for parent in (directory, *directory.parents):
        if parent == temporary:
            break
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            _refuse("closeout destination is redirected")
    directory.mkdir(parents=True, exist_ok=False)
    inputs: dict[str, Any] = {
        "root": root,
        "environment": environment,
        "event": event,
        "context": context,
        "closeout_context": context,
        "directory": directory,
        "suffix": suffix,
        "records": records,
        "publisher_handoff_reference": reference,
        "resolved": {},
        "producers": {},
        "uploads": {},
        "diagnostics": [],
        "downloader": ArtifactDownloader(directory, environment=environment),
    }
    inputs["context"] = _closeout_publisher(inputs, reference)
    handoff = _expect_mapping(
        _load_json_object(_promotion_resolve(inputs, reference), "publisher handoff"),
        "publisher handoff",
        (
            "candidate_identity_record",
            "release_gate_catalog",
            "promotion_decision",
            "pre_decision_stage_evidence",
            "pre_publication_stage_evidence",
            "promotion_attempt",
            "remote_observation",
            "remote_classification",
            "pre_publication_verification",
        ),
    )
    for field in handoff:
        retained = _review_record_reference(handoff[field], f"publisher handoff {field}")
        if retained["repository"] != context["source"]["repository"]:
            _refuse("publisher handoff dependency belongs to another repository")
        if retained["run_id"] == context["run_id"]:
            _refuse("historical publisher cannot depend on the current closeout")

    def resolve(retained: Mapping[str, Any]) -> bytes:
        return _closeout_resolve(inputs, retained)

    identity_compact = handoff["candidate_identity_record"]
    identity_raw = resolve(identity_compact)
    identity = _validate_candidate_identity(_load_json_object(identity_raw, "retained Candidate"))
    candidate = identity["candidate"]
    retention = candidate["build"]["artifact"]["retention"]["configured_days"]
    _expect_retention_days({"A1_RETENTION_DAYS": str(retention)})
    artifact = _promotion_artifact(inputs, identity_compact)
    artifact = {**artifact, "retention": {**artifact["retention"], "configured_days": retention}}
    identity_reference = {
        **{key: identity_compact[key] for key in ("repository", "run_id", "path", "sha256")},
        "artifact": artifact,
    }
    _verified_review_candidate(identity, identity_raw, identity_reference)
    names = _artifact_names(
        candidate["preview_manifest"]["contents"]["version"],
        candidate["build"]["run_id"],
        candidate["build"]["run_attempt"],
    )
    if (
        any(candidate["source"][key] != value for key, value in context["source"].items())
        or identity_compact["path"] != _CANDIDATE_IDENTITY_FILENAME
        or identity_compact["run_id"] != candidate["build"]["run_id"]
        or artifact["name"] != names["candidate_identity"]
        or candidate["build"]["artifact"]["name"] != names["payload"]
    ):
        _refuse("closeout Candidate differs from the exact canonical source build")
    completed_at = _verified_evidence_build(inputs["downloader"], context, candidate)
    catalog_reference = handoff["release_gate_catalog"]
    if (
        catalog_reference["path"] != _RELEASE_GATE_CATALOG_FILENAME
        or catalog_reference["run_id"] != candidate["build"]["run_id"]
        or catalog_reference["artifact_id"] == identity_compact["artifact_id"]
        or _promotion_artifact(inputs, catalog_reference)["name"] != names["release_gate_catalog"]
    ):
        _refuse("closeout Catalog does not bind the distinct source-build artifact")
    catalog_raw = resolve(catalog_reference)
    binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": identity_raw,
        "candidate_identity_reference": identity_reference,
        "release_gate_catalog": _load_json_object(catalog_raw, "Catalog"),
        "release_gate_catalog_bytes": catalog_raw,
        "release_gate_catalog_reference": catalog_reference,
        "record_resolver": resolve,
        "repository_root": root,
    }
    decision_reference = handoff["promotion_decision"]
    if decision_reference["path"] != "promotion-decision.json":
        _refuse("publisher handoff Decision has an unexpected path")
    decision = seal_promotion_decision(
        _load_json_object(resolve(decision_reference), "retained Decision"), **binding
    )
    if (
        decision["decision"] != "APPROVE"
        or decision["candidate_identity_record"] != identity_compact
        or decision["evidence"]["pre_decision_stage_evidence"]["record_reference"]
        != handoff["pre_decision_stage_evidence"]
        or _review_timestamp(decision["decided_at"], "Decision time") > datetime.now(timezone.utc)
    ):
        _refuse("closeout requires the publisher's exact existing APPROVE")
    decision_artifact = _promotion_artifact(inputs, decision_reference)
    match = re.fullmatch(
        rf"stateless-preview-evidence-decision-{decision_reference['run_id']}-([1-9][0-9]*)",
        decision_artifact["name"],
    )
    if match is None:
        _refuse("publisher Decision does not identify its evidence producer")
    evidence_attempt = int(match[1])
    evidence_suffix = f"{decision_reference['run_id']}-{evidence_attempt}"
    stage_reference = handoff["pre_decision_stage_evidence"]
    if (
        stage_reference["path"] != "pre-decision-stage.json"
        or stage_reference["run_id"] != decision_reference["run_id"]
        or _promotion_artifact(inputs, stage_reference)["name"]
        != f"stateless-preview-evidence-stage-{evidence_suffix}"
    ):
        _refuse("publisher pre-decision Stage has a different producer")
    producer = inputs["producers"][(decision_reference["run_id"], evidence_attempt)]
    for role in ("actor", "triggering_actor"):
        observed = producer.get(role)
        if (
            not isinstance(observed, Mapping)
            or {"id": str(observed.get("id")), "login": observed.get("login")}
            != decision["promotion_attempt"]["decision_author"]
            or context["actors"][role] != decision["promotion_attempt"]["authorized_dispatcher"]
        ):
            _refuse("Decision author or closeout initiators differ from authenticated provenance")
    stage = _load_json_object(resolve(stage_reference), "pre-decision Stage")
    for entry in stage["gate_evidence"]:
        retained = entry["record_reference"]
        if (
            retained["run_id"] != decision_reference["run_id"]
            or _promotion_artifact(inputs, retained)["name"]
            != f"stateless-preview-evidence-records-{evidence_suffix}"
        ):
            _refuse("publisher pre-decision proof has a different evidence producer")
        proof = _load_json_object(resolve(retained), "pre-decision proof")
        times = [
            proof[
                "sealed_at"
                if entry["record_type"] == "s2-s3-seven-lens-aggregate"
                else "recorded_at"
            ]
        ]
        if entry["record_type"] == "s2-s3-seven-lens-aggregate":
            times.extend(lens["recorded_at"] for lens in proof["lens_results"])
        if any(
            not completed_at <= _review_timestamp(value, "proof time") <= datetime.now(timezone.utc)
            for value in times
        ):
            _refuse("publisher proof does not follow its completed source build")
    inputs.update(
        binding=binding,
        candidate=candidate,
        retention_days=retention,
        handoff=handoff,
        decision=decision,
        decision_reference=decision_reference,
    )
    # History is anchored at P, never Q or the latest attempt of P's run.
    predecessor, results = _promotion_discover_predecessor(inputs)
    inputs["context"]["current"].update(
        predecessor_attempt=predecessor, predecessor_operation_results=results
    )
    _closeout_admit_lineage(inputs)
    return inputs


def _closeout_admit_lineage(inputs: MutableMapping[str, Any]) -> None:
    handoff, binding, context = inputs["handoff"], inputs["binding"], inputs["context"]
    suffix = f"{context['run_id']}-{context['attempt']}-{context['current']['job_id']}"
    final_step = re.fullmatch(
        r"remote-observation-([0-3])\.json", handoff["remote_observation"]["path"]
    )
    if final_step is None:
        _refuse("publisher final observation has an unexpected path")
    step = int(final_step[1])
    for field, path, name in (
        (
            "promotion_attempt",
            "promotion-attempt.json",
            f"stateless-preview-promote-attempt-{suffix}",
        ),
        (
            "pre_publication_stage_evidence",
            "pre-publication-stage.json",
            f"stateless-preview-promote-admission-{suffix}",
        ),
        (
            "remote_observation",
            f"remote-observation-{step}.json",
            f"stateless-preview-promote-{'observation' if step else 'admission'}-{suffix}"
            + (f"-{step}" if step else ""),
        ),
        (
            "remote_classification",
            f"remote-classification-{step}.json",
            f"stateless-preview-promote-classification-{suffix}-{step}",
        ),
    ):
        reference = handoff[field]
        if (
            reference["path"] != path
            or reference["run_id"] != context["run_id"]
            or _promotion_artifact(inputs, reference)["name"] != name
        ):
            _refuse("publisher handoff reference does not bind the exact producing job")
    attempt = seal_promotion_attempt(
        _resolve_stage_record(handoff["promotion_attempt"], binding["record_resolver"]),
        current_promotion_context=context["current"],
        **binding,
    )
    if not (
        _review_timestamp(context["publisher_attempt_created_at"], "publisher attempt creation")
        <= _review_timestamp(attempt["recorded_at"], "publisher Attempt time")
        <= inputs["publisher_completed_at"]
    ):
        _refuse("publisher Attempt is outside its actual execution chronology")
    if (
        attempt["candidate_identity_record"] != handoff["candidate_identity_record"]
        or attempt["promotion_decision"] != handoff["promotion_decision"]
        or any(
            attempt[f"{name}_stage_evidence"]["record_reference"]
            != handoff[f"{name}_stage_evidence"]
            for name in ("pre_decision", "pre_publication")
        )
    ):
        _refuse("publisher handoff differs from its admitted Attempt lineage")
    artifacts = _promotion_collection(
        inputs, f"actions/runs/{context['run_id']}/artifacts", "artifacts"
    )
    expected_handoff = _promotion_artifact(inputs, inputs["publisher_handoff_reference"])
    retained_handoffs = [item for item in artifacts if item.get("name") == expected_handoff["name"]]
    if (
        len(retained_handoffs) != 1
        or str(retained_handoffs[0].get("id"))
        != inputs["publisher_handoff_reference"]["artifact_id"]
    ):
        _refuse("complete publisher discovery does not contain the exact handoff")
    advancement = []
    for name in _PROMOTION_ADVANCEMENT_STEPS:
        selected = [item for item in inputs["publisher_job"]["steps"] if item.get("name") == name]
        if len(selected) != 1:
            _refuse("publisher advancement evidence is absent or ambiguous")
        advancement.append(selected[0])
    observed_reference, classified_reference, observation, classification, _ = (
        _promotion_revalidate_advancements(inputs, attempt, advancement, artifacts)
    )
    if (
        observed_reference != handoff["remote_observation"]
        or classified_reference != handoff["remote_classification"]
        or classification["admission"] != "ADMITTED"
        or classification["state"] != "published_complete"
        or not (
            _review_timestamp(attempt["recorded_at"], "Attempt time")
            <= _review_timestamp(observation["observed_at"], "published observation time")
            <= _review_timestamp(classification["classified_at"], "published classification time")
            <= inputs["publisher_completed_at"]
        )
    ):
        _refuse("publisher lineage does not prove completed, admitted publication")
    pre_reference = handoff["pre_publication_verification"]
    pre = _resolve_stage_record(pre_reference, binding["record_resolver"])
    if pre != _promotion_fragment(inputs, observation) or pre_reference != inputs.get(
        "predecessor_fragment_reference"
    ):
        _refuse("original PRE_PUBLICATION fragment is absent or disconnected from publication")
    proof = _validate_stage_consumer_proof(
        _load_json_object(inputs["records"]["remote-consumer-proof.json"], "root public proof"),
        remote=True,
        **{
            key: binding[key]
            for key in (
                "candidate_identity",
                "candidate_identity_bytes",
                "candidate_identity_reference",
                "release_gate_catalog",
            )
        },
    )
    if proof["download_origin"] != pre["release"] or not (
        _review_timestamp(observation["observed_at"], "publication time")
        <= _review_timestamp(proof["recorded_at"], "root public proof time")
        <= datetime.now(timezone.utc)
    ):
        _refuse("root public proof does not follow this exact publication")
    inputs.update(attempt=attempt, observation=observation, pre_publication_verification=pre)


def _closeout_uploaded(
    inputs: Mapping[str, Any], kind: str
) -> dict[str, tuple[dict[str, str], bytes]]:
    if kind in inputs["uploads"]:
        return inputs["uploads"][kind]
    context = inputs["closeout_context"]
    prefix = f"A1_CLOSEOUT_{kind.upper()}_ARTIFACT"
    artifact_id = _expect_environment_identifier(inputs["environment"], f"{prefix}_ID")
    digest = _expect_sha256(
        _environment_value(inputs["environment"], f"{prefix}_DIGEST").removeprefix("sha256:"),
        "closeout upload container digest",
    )
    artifact = _checked_evidence_artifact(
        inputs["downloader"],
        context,
        artifact_id,
        context["run_id"],
        f"stateless-preview-closeout-{kind}-{inputs['suffix']}",
        inputs["retention_days"],
        None,
    )
    if artifact["sha256"] != digest:
        _refuse("closeout upload output differs from the actual container digest")
    wire = inputs["downloader"]._archive(
        context["source"]["repository"], context["run_id"], artifact
    )
    try:
        with zipfile.ZipFile(io.BytesIO(wire)) as archive:
            entries = archive.infolist()
            if (
                len(entries) != len(_CLOSEOUT_FILES[kind])
                or {entry.filename for entry in entries} != set(_CLOSEOUT_FILES[kind].values())
                or any(
                    entry.is_dir()
                    or entry.orig_filename != entry.filename
                    or entry.file_size > _PROMOTION_JSON_LIMIT
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                    or entry.external_attr & (0x10 | 0x400)
                    for entry in entries
                )
            ):
                _refuse("closeout artifact does not contain exactly its original fixed records")
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _refuse("closeout artifact ZIP is unreadable")
    records = {}
    for filename in _CLOSEOUT_FILES[kind].values():
        retained, raw = inputs["downloader"]._download_record(
            context["source"]["repository"], context["run_id"], artifact, filename
        )
        compact = _compact_evidence_reference(retained)
        records[filename] = (compact, raw)
        inputs["resolved"][_evidence_reference_key(compact)] = raw
    inputs["uploads"][kind] = records
    return records


def _closeout_read_records(inputs: Mapping[str, Any]) -> tuple[dict[str, str], Mapping[str, Any]]:
    records = _closeout_uploaded(inputs, "records")
    proof_reference, proof_raw = records["remote-consumer-proof.json"]
    if proof_raw != inputs["records"]["remote-consumer-proof.json"]:
        _refuse("retained root public proof differs from its original dispatch bytes")
    _, raw = records["post-publication-verification.json"]
    post = _load_json_object(raw, "retained post-publication byte verification")
    if post != {**inputs["pre_publication_verification"], "phase": "POST_PUBLICATION"}:
        _refuse("retained post-publication fragment does not prove the published candidate")
    return proof_reference, post


def _closeout_verification_record(
    inputs: Mapping[str, Any], proof_reference: Mapping[str, Any], post: Mapping[str, Any]
) -> dict[str, Any]:
    handoff = inputs["handoff"]
    return {
        "verification_schema_version": "1.0",
        "verification_id": f"a1-closeout-verification-{inputs['suffix']}",
        "promotion_attempt_id": inputs["attempt"]["attempt_id"],
        **{
            field: handoff[field]
            for field in (
                "candidate_identity_record",
                "promotion_decision",
                "promotion_attempt",
                "remote_observation",
                "remote_classification",
            )
        },
        "candidate": inputs["candidate"],
        "pre_publication_verification": inputs["pre_publication_verification"],
        "post_publication_verification": post,
        "remote_consumer_proof": proof_reference,
        "outcome": "PASS",
        "verified_at": _utc_timestamp(),
    }


def _closeout_read_verification(
    inputs: Mapping[str, Any],
) -> tuple[dict[str, str], Mapping[str, Any], dict[str, str]]:
    proof_reference, post = _closeout_read_records(inputs)
    reference, raw = _closeout_uploaded(inputs, "verification")["remote-verification.json"]
    verification = seal_remote_verification(
        _load_json_object(raw, "retained RemoteVerification"),
        current_promotion_context=inputs["context"]["current"],
        **inputs["binding"],
    )
    expected = _closeout_verification_record(inputs, proof_reference, post)
    if any(
        verification[key] != value for key, value in expected.items() if key != "verified_at"
    ) or _review_timestamp(
        verification["verified_at"], "retained Verification time"
    ) > datetime.now(timezone.utc):
        _refuse("retained Verification differs from the actual publisher/closeout graph")
    return reference, verification, proof_reference


def _closeout_stage_record(
    inputs: Mapping[str, Any], proof_reference: Mapping[str, Any]
) -> dict[str, Any]:
    handoff, candidate = inputs["handoff"], inputs["candidate"]
    identity_hash = handoff["candidate_identity_record"]["sha256"]
    return {
        "evidence_schema_version": "1.0",
        "evidence_set_id": f"a1-closeout-stage-{inputs['suffix']}",
        "producer": {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "seal_stage_gate_evidence",
        },
        "release_gate_catalog": handoff["release_gate_catalog"],
        "candidate_identity_record": handoff["candidate_identity_record"],
        "candidate_identity_record_sha256": identity_hash,
        "candidate": candidate,
        "source_ref": candidate["source"]["ref"],
        "source_commit": candidate["source"]["commit"],
        "stage": "post-publication",
        "gate_evidence": [
            {
                **descriptor,
                "record_reference": proof_reference,
                "candidate_identity_record_sha256": identity_hash,
                "source_commit": candidate["source"]["commit"],
                "outcome": "PASS",
            }
            for descriptor in inputs["binding"]["release_gate_catalog"]["catalog"][
                "gate_descriptors"
            ]
            if descriptor["stage"] == "post-publication"
        ],
        "outcome": "PASS",
        "sealed_at": _utc_timestamp(),
    }


def _closeout_read_stage(
    inputs: Mapping[str, Any], verification: Mapping[str, Any], proof_reference: Mapping[str, Any]
) -> tuple[dict[str, str], Mapping[str, Any]]:
    reference, raw = _closeout_uploaded(inputs, "stage")["post-publication-stage.json"]
    stage = seal_stage_gate_evidence(
        _load_json_object(raw, "retained post-publication Stage"), **inputs["binding"]
    )
    expected = _closeout_stage_record(inputs, proof_reference)
    if any(stage[key] != value for key, value in expected.items() if key != "sealed_at") or not (
        _review_timestamp(verification["verified_at"], "Verification time")
        <= _review_timestamp(stage["sealed_at"], "post-publication Stage time")
        <= datetime.now(timezone.utc)
    ):
        _refuse("retained post-publication Stage differs from the read-back proof graph")
    return reference, stage


def _closeout_handoff_record(
    inputs: Mapping[str, Any],
    verification_reference: Mapping[str, Any],
    proof_reference: Mapping[str, Any],
    stage_reference: Mapping[str, Any],
) -> dict[str, Any]:
    publisher, candidate = inputs["handoff"], inputs["candidate"]
    return {
        "handoff_schema_version": "1.0",
        "handoff_id": f"a1-closeout-handoff-{inputs['suffix']}",
        "producer": {
            "helper_path": "scripts/stateless_preview_artifact.py",
            "operation": "seal_program_b_handoff",
        },
        "candidate_identity_record": publisher["candidate_identity_record"],
        "candidate": candidate,
        "source_ref": candidate["source"]["ref"],
        "source_commit": candidate["source"]["commit"],
        "approved_promotion_decision": publisher["promotion_decision"],
        "promotion_attempt": publisher["promotion_attempt"],
        "promotion_attempt_id": inputs["attempt"]["attempt_id"],
        "published_remote_observation": publisher["remote_observation"],
        "published_remote_classification": publisher["remote_classification"],
        "remote_verification_receipt": verification_reference,
        "remote_consumer_proof": proof_reference,
        "release_gate_catalog": publisher["release_gate_catalog"],
        "pre_decision_stage_evidence": publisher["pre_decision_stage_evidence"],
        "pre_publication_stage_evidence": publisher["pre_publication_stage_evidence"],
        "post_publication_stage_evidence": stage_reference,
        "completion_scope": "A1_OPT_IN_PREVIEW_COMPLETE",
        "program_b_authorization_required": True,
        "program_c_authorization_required": True,
        "recorded_at": _utc_timestamp(),
    }


def _closeout_recheck(inputs: Mapping[str, Any]) -> None:
    fresh = _closeout_context(inputs["root"], inputs["environment"], inputs["event"])
    if fresh != inputs["closeout_context"] or any(
        actor != inputs["decision"]["promotion_attempt"]["authorized_dispatcher"]
        for actor in fresh["actors"].values()
    ):
        _refuse("current closeout context or canonical source changed during readback")


def _closeout_emit(inputs: Mapping[str, Any], kind: str, records: Mapping[str, bytes]) -> None:
    _closeout_recheck(inputs)
    paths = {
        key: inputs["directory"] / "outputs" / filename
        for key, filename in _CLOSEOUT_FILES[kind].items()
    }
    for key, path in paths.items():
        _write_bytes_once(path, records[path.name], "closeout record")
    _write_action_outputs(
        {
            **{key: str(path) for key, path in paths.items()},
            f"closeout_{kind}_artifact_name": f"stateless-preview-closeout-{kind}-{inputs['suffix']}",
            "retention_days": str(inputs["retention_days"]),
        },
        inputs["environment"],
    )


def _closeout_read_graph(inputs: Mapping[str, Any], handoff: Mapping[str, Any]) -> None:
    pending: list[Any] = [handoff, inputs["handoff"]]
    seen: set[tuple[str, ...]] = set()
    compact_fields = {"repository", "run_id", "artifact_id", "path", "sha256"}
    expanded_fields = {"repository", "run_id", "artifact", "path", "sha256"}
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            fields = set(value)
            if fields in (compact_fields, expanded_fields):
                reference = (
                    _compact_evidence_reference(
                        ArtifactFileReference.from_mapping(value, "graph reference")
                    )
                    if fields == expanded_fields
                    else value
                )
                key = _evidence_reference_key(reference)
                if key in seen:
                    continue
                if len(seen) >= 10000:
                    _refuse("closeout reference graph exceeds its discovery bound")
                seen.add(key)
                raw = _closeout_resolve(inputs, reference)
                if reference["path"].endswith(".json"):
                    pending.append(_load_json_object(raw, "closeout graph record"))
            else:
                pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def _run_closeout(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any], command: str
) -> None:
    inputs = _closeout_inputs(root, environment, event, command)
    if command == "admit-closeout":
        from scripts.stateless_preview_public_release import download_public_release

        expected_origin = inputs["pre_publication_verification"]["release"]
        archive, manifest, origin = download_public_release(
            inputs["candidate"], expected_origin["release_id"], inputs["directory"] / "public"
        )
        verified = verify_and_extract_retained_artifact(
            inputs["binding"]["candidate_identity"],
            archive,
            manifest,
            inputs["directory"] / "verified-executable",
        )
        if origin != expected_origin:
            _refuse("fresh anonymous assets differ from publisher/root public proof")
        # These equations follow the fresh public download and inherited extractor above.
        post = {
            "phase": "POST_PUBLICATION",
            "expected_result": "EXPECT_SUCCESS",
            "observed_result": "SUCCESS",
            "outcome": "PASS",
            "release": origin,
            "executable": verified["executable"],
            "archive_matches_candidate": verified["archive"]
            == inputs["candidate"]["preview_manifest"]["contents"]["archive"],
            "manifest_matches_candidate": verified["manifest"]
            == inputs["candidate"]["preview_manifest"]["file"],
            "executable_matches_manifest": verified["executable"]
            == inputs["candidate"]["preview_manifest"]["contents"]["executable"],
            "inherited_verifier_equations_pass": True,
        }
        if post != {**inputs["pre_publication_verification"], "phase": "POST_PUBLICATION"}:
            _refuse("fresh public verification equations do not match the published candidate")
        _closeout_emit(
            inputs,
            "records",
            {
                "remote-consumer-proof.json": inputs["records"]["remote-consumer-proof.json"],
                "post-publication-verification.json": _canonical_json_bytes(
                    post, "public verification"
                ),
            },
        )
        return
    if command == "seal-closeout-verification":
        proof_reference, post = _closeout_read_records(inputs)
        verification = seal_remote_verification(
            _closeout_verification_record(inputs, proof_reference, post),
            current_promotion_context=inputs["context"]["current"],
            **inputs["binding"],
        )
        _closeout_emit(
            inputs,
            "verification",
            {"remote-verification.json": _canonical_json_bytes(verification, "RemoteVerification")},
        )
        return
    verification_reference, verification, proof_reference = _closeout_read_verification(inputs)
    if command == "seal-closeout-stage":
        stage = seal_stage_gate_evidence(
            _closeout_stage_record(inputs, proof_reference), **inputs["binding"]
        )
        if _review_timestamp(stage["sealed_at"], "new Stage time") < _review_timestamp(
            verification["verified_at"], "retained Verification time"
        ):
            _refuse("post-publication Stage precedes its retained Verification")
        _closeout_emit(
            inputs,
            "stage",
            {"post-publication-stage.json": _canonical_json_bytes(stage, "post-publication Stage")},
        )
        return
    stage_reference, _ = _closeout_read_stage(inputs, verification, proof_reference)
    expected = _closeout_handoff_record(
        inputs, verification_reference, proof_reference, stage_reference
    )
    if command == "seal-closeout-handoff":
        handoff = seal_program_b_handoff(
            expected, current_promotion_context=inputs["context"]["current"], **inputs["binding"]
        )
        _closeout_emit(
            inputs,
            "handoff",
            {"program-b-handoff.json": _canonical_json_bytes(handoff, "Program B handoff")},
        )
        return
    if command != "verify-closeout-retention":
        _refuse("closeout command is unsupported")
    reference, raw = _closeout_uploaded(inputs, "handoff")["program-b-handoff.json"]
    handoff = seal_program_b_handoff(
        _load_json_object(raw, "retained Program B handoff"),
        current_promotion_context=inputs["context"]["current"],
        **inputs["binding"],
    )
    if any(
        handoff[key] != value for key, value in expected.items() if key != "recorded_at"
    ) or _review_timestamp(handoff["recorded_at"], "retained handoff time") > datetime.now(
        timezone.utc
    ):
        _refuse("retained handoff differs from the actual non-authorizing closeout graph")
    _closeout_read_graph(inputs, handoff)
    _closeout_recheck(inputs)
    compact_json = json.dumps(reference, ensure_ascii=True, separators=(",", ":"))
    try:
        with Path(_environment_value(environment, "GITHUB_STEP_SUMMARY")).open(
            "a", encoding="utf-8", newline="\n"
        ) as output:
            output.write("Retained A1 closeout (not Program B/C authorization):\n\n")
            output.write(f"program_b_handoff_reference:\n```json\n{compact_json}\n```\n")
    except OSError:
        _refuse("GitHub Actions summary file is unavailable")
    _write_action_outputs({"program_b_handoff_reference": compact_json}, environment)


_PROMOTION_BINARY_LIMIT = 256 * 1024 * 1024
_PROMOTION_JSON_LIMIT = 1024 * 1024
_PROMOTION_JOB_NAME = "Promote canonical-main preview candidate"


def _promotion_environment(environment: Mapping[str, str]) -> dict[str, str]:
    token = _environment_value(environment, "GITHUB_TOKEN")
    if any(character.isspace() for character in token):
        _refuse("workflow token contains unsupported whitespace")
    child = {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith(("GH_", "GIT_", "GCM_"))
        and key.upper()
        not in (
            "SSH_ASKPASS",
            "CURL_HOME",
            "CURL_VERBOSE",
            "GITHUB_ENTERPRISE_TOKEN",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
        )
    }
    temporary = Path(_environment_value(environment, "RUNNER_TEMP")).resolve(strict=True)
    suffix = "-".join(
        _expect_environment_identifier(environment, key)
        for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
    )
    config = temporary / f"stateless-preview-empty-gh-{suffix}"
    if config.is_symlink() or not _path_within(config, temporary):
        _refuse("publisher authentication directory is unsafe")
    config.mkdir(parents=True, exist_ok=True)
    if any(config.iterdir()):
        _refuse("publisher authentication directory must not contain cached credentials")
    child.update(
        GITHUB_TOKEN=token,
        GH_CONFIG_DIR=str(config),
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GH_NO_EXTENSION_UPDATE_NOTIFIER="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )
    return child


def _promotion_process(
    root: Path,
    environment: Mapping[str, str],
    command: Sequence[str],
    *,
    data: bytes | None = None,
    limit: int = _PROMOTION_JSON_LIMIT,
) -> tuple[int | None, bytes]:
    # File-backed capture bounds accepted bytes without an unbounded PIPE allocation.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                list(command),
                cwd=root,
                env=dict(environment),
                input=data,
                stdout=output,
                stderr=errors,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None, b""
        output.seek(0)
        raw = result.stdout if isinstance(result.stdout, bytes) else output.read(limit + 1)
        if len(raw) > limit:
            _refuse("publisher process response exceeds its byte bound")
        return result.returncode, raw


def _promotion_gh(
    root: Path,
    environment: Mapping[str, str],
    arguments: Sequence[str],
    name: str,
    *,
    data: bytes | None = None,
) -> tuple[int, bytes]:
    if not arguments or arguments[0] != "api":
        _refuse("publisher supports only fixed GitHub REST operations")
    endpoint = arguments[1]
    repository = _expect_repository(environment.get("GITHUB_REPOSITORY"), "publisher repository")
    if not endpoint.startswith(f"repos/{repository}/") or any(
        character in endpoint for character in ("#", "\r", "\n", "\\")
    ):
        _refuse("publisher endpoint is outside the bound repository")
    limit = (
        _PROMOTION_BINARY_LIMIT
        if endpoint.endswith("/zip") or "/releases/assets/" in endpoint
        else _PROMOTION_JSON_LIMIT
    )
    if not any(value.lower().startswith("accept:") for value in arguments):
        arguments = [*arguments, "-H", "Accept: application/vnd.github+json"]
    code, raw = _promotion_process(
        root,
        _promotion_environment(environment),
        [
            "gh",
            *arguments,
            "--hostname",
            "github.com",
            "--include",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            "-H",
            "Cache-Control: no-cache",
        ],
        data=data,
        limit=limit + 16 * 1024,
    )
    # Only an actual HTTP status can establish absence; stderr is never parsed.
    separators = [(raw.find(delimiter), delimiter) for delimiter in (b"\r\n\r\n", b"\n\n")]
    separators = [(position, delimiter) for position, delimiter in separators if position >= 0]
    if not separators:
        return 0, b""
    position, delimiter = min(separators)
    header, body = raw[:position], raw[position + len(delimiter) :]
    match = re.match(rb"HTTP/\S+ ([1-5][0-9]{2})(?: |\r?\n|$)", header)
    if match is None or len(header) > 16 * 1024:
        return 0, b""
    status = int(match[1])
    if len(body) > limit:
        _refuse(f"{name} exceeds its byte bound")
    if code != 0 and 200 <= status < 300:
        return 0, b""
    return status, body


def _promotion_api(
    inputs: Mapping[str, Any],
    route: str,
    *,
    method: str = "GET",
    body: Any = None,
    absent: bool = False,
    binary: bool = False,
) -> Any:
    endpoint = f"repos/{inputs['context']['source']['repository']}/{route}"
    arguments = [
        "api",
        endpoint,
        "--method",
        method,
        "-H",
        "Accept: application/octet-stream" if binary else "Accept: application/vnd.github+json",
    ]
    raw = None
    if body is not None:
        raw = _canonical_json_bytes(body, "publisher request")
        arguments.extend(("--input", "-"))
    status, response = _promotion_gh(
        inputs["root"], inputs["environment"], arguments, route, data=raw
    )
    inputs["diagnostics"].append(
        {"operation": method, "endpoint": route, "http_status": status or None}
    )
    if absent and status == 404:
        return None
    if status not in ((200,) if method == "GET" else (200, 201)):
        _refuse(f"{method} {route}: HTTP {status or 'UNKNOWN'}")
    if binary:
        return response
    # Lists also use the strict duplicate-key parser by wrapping the original bytes.
    return _load_json_object(b'{"value":' + response + b"}", route)["value"]


def _promotion_listing(inputs: Mapping[str, Any], route: str) -> list[Any]:
    entries: list[Any] = []
    for page in range(1, 101):
        batch = _expect_list(_promotion_api(inputs, f"{route}?per_page=100&page={page}"), route)
        if len(batch) > 100 or any(not isinstance(value, Mapping) for value in batch):
            _refuse("publisher listing is malformed")
        entries.extend(batch)
        if len(batch) < 100:
            return entries
    _refuse("publisher listing exceeds its pagination bound")


def _select_current_job(
    jobs: Mapping,
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    job_name: str,
) -> Mapping:
    """Return a new job/zero-based setup selector or refuse incomplete identity."""
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or type(run_id) is not int
        or run_id <= 0
        or type(run_attempt) is not int
        or run_attempt <= 0
        or not isinstance(head_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None
        or not isinstance(job_name, str)
        or not job_name.strip()
    ):
        raise ValueError("Current-job identity is missing or invalid.")
    if not isinstance(jobs, Mapping):
        raise ValueError("The job listing must be an API response mapping.")
    entries = jobs.get("jobs")
    total = jobs.get("total_count")
    if not isinstance(entries, list) or type(total) is not int or total != len(entries):
        raise ValueError("The job listing is malformed or incomplete.")
    if any(
        not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str)
        for entry in entries
    ):
        raise ValueError("The job listing contains malformed job metadata.")
    matches = [entry for entry in entries if entry["name"] == job_name]
    if len(matches) != 1:
        raise ValueError("The current job name must identify exactly one job.")
    job = matches[0]
    job_id = job.get("id")
    if (
        type(job_id) is not int
        or job_id <= 0
        or type(job.get("run_id")) is not int
        or job["run_id"] != run_id
        or type(job.get("run_attempt")) is not int
        or job["run_attempt"] != run_attempt
        or job.get("head_sha") != head_sha
        or job.get("status") != "in_progress"
        or "conclusion" not in job
        or job["conclusion"] is not None
        or job.get("url") != f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}"
        or job.get("run_url") != f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
    ):
        raise ValueError("Job metadata does not bind to the requested running job.")
    steps = job.get("steps")
    if not isinstance(steps, list) or any(
        not isinstance(step, Mapping) or not isinstance(step.get("name"), str) for step in steps
    ):
        raise ValueError("The current job has malformed setup metadata.")
    positions = [index for index, step in enumerate(steps) if step["name"] == "Set up job"]
    if positions != [0]:
        raise ValueError("The setup step must occur exactly once at array position zero.")
    setup = steps[0]
    if setup.get("status") != "completed" or setup.get("conclusion") != "success":
        raise ValueError("The setup step has not completed successfully.")
    # Setup identity uses array position, not its unrelated API step number.
    return {"job_id": job_id, "setup_step_position": positions[0]}


def _promotion_context(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any]
) -> dict[str, Any]:
    _environment_value(environment, "GITHUB_TOKEN")
    source = _canonical_build_source(root, environment)
    context = {
        "source": source,
        "repository_id": _expect_environment_identifier(environment, "GITHUB_REPOSITORY_ID"),
        "run_id": _expect_environment_identifier(environment, "GITHUB_RUN_ID"),
        "attempt": int(_expect_environment_identifier(environment, "GITHUB_RUN_ATTEMPT")),
    }
    read_inputs = {"root": root, "environment": environment, "context": context, "diagnostics": []}
    live = _promotion_api(read_inputs, "git/ref/heads/main")
    if (
        not isinstance(live, Mapping)
        or live.get("ref") != _CANONICAL_SOURCE_REF
        or live.get("object") != {"type": "commit", "sha": source["commit"]}
    ):
        # GitHub may include additional object metadata, but not a different target/type.
        if (
            not isinstance(live, Mapping)
            or live.get("ref") != _CANONICAL_SOURCE_REF
            or not isinstance(live.get("object"), Mapping)
            or live["object"].get("type") != "commit"
            or live["object"].get("sha") != source["commit"]
        ):
            _refuse("live canonical main differs from the exact checkout")
    run = _promotion_api(
        read_inputs, f"actions/runs/{context['run_id']}/attempts/{context['attempt']}"
    )
    _validate_cli_run(run, context, context["run_id"], context["attempt"], successful_build=False)
    actors = {}
    for field in ("actor", "triggering_actor"):
        actor = run.get(field)
        if not isinstance(actor, Mapping):
            _refuse("current promote actor is unavailable")
        actors[field] = {
            "id": str(_expect_positive_integer(actor.get("id"), field)),
            "login": actor.get("login"),
        }
        _decision_actor(actors[field], field)
    sender = event.get("sender")
    if (
        not isinstance(sender, Mapping)
        or {
            "id": str(_expect_positive_integer(sender.get("id"), "event sender ID")),
            "login": sender.get("login"),
        }
        != actors["actor"]
        or any(
            (
                _expect_environment_identifier(environment, "GITHUB_ACTOR_ID")
                != actors["actor"]["id"],
                _environment_value(environment, "GITHUB_ACTOR") != actors["actor"]["login"],
                _environment_value(environment, "GITHUB_TRIGGERING_ACTOR")
                != actors["triggering_actor"]["login"],
                _environment_value(environment, "GITHUB_JOB") != "promote",
            )
        )
    ):
        _refuse("current promote actors or job differ from the event/runner context")
    jobs = _promotion_api(
        read_inputs,
        f"actions/runs/{context['run_id']}/attempts/{context['attempt']}/jobs?per_page=100",
    )
    selected = _select_current_job(
        jobs,
        repository=source["repository"],
        run_id=int(context["run_id"]),
        run_attempt=context["attempt"],
        head_sha=source["commit"],
        job_name=_PROMOTION_JOB_NAME,
    )
    job_id = str(selected["job_id"])
    context["current"] = {
        "current_attempt_id": f"a1-promote-{context['run_id']}-{context['attempt']}-{job_id}",
        "workflow_run": {
            "repository": source["repository"],
            "workflow_path": _TRUSTED_WORKFLOW_PATH,
            "mode": "promote",
            "event": _TRUSTED_BUILD_EVENT,
            "run_id": context["run_id"],
            "run_attempt": context["attempt"],
            "ref": source["ref"],
            "commit": source["commit"],
        },
        "job_id": job_id,
        "job_key": "promote",
        "workflow_sha": environment["GITHUB_WORKFLOW_SHA"],
        "checkout_head": source["commit"],
        "origin_main_target": live["object"]["sha"],
        "current_github_actor": actors["actor"],
        "current_github_triggering_actor": actors["triggering_actor"],
    }
    return context


def _promotion_execution_policy(root: Path, current: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": "GITHUB_JOB_TOKEN_SERVER_ENFORCED",
        "run_attempt": current["workflow_run"]["run_attempt"],
        **{key: current[key] for key in ("job_key", "job_id", "workflow_sha")},
        "workflow_blob_sha256": _sha256_bytes(
            _read_tracked_authority(root, current["workflow_sha"], _TRUSTED_WORKFLOW_PATH)
        ),
        "execution_token": {
            "source": "github.token",
            "kind": "GITHUB_APP_INSTALLATION_TOKEN",
            "identity_observation": "UNOBSERVED",
            "app_id": None,
            "installation_id": None,
        },
    }


def _promotion_locations(
    environment: Mapping[str, str], context: Mapping[str, Any], step: int
) -> dict[str, str]:
    temporary = Path(_environment_value(environment, "RUNNER_TEMP")).resolve(strict=True)
    suffix = context["current"]["current_attempt_id"].removeprefix("a1-promote-")
    directory = temporary / f"stateless-preview-promote-{suffix}"
    if not temporary.is_dir() or directory.is_symlink() or not _path_within(directory, temporary):
        _refuse("publisher destination escapes the runner temporary directory")
    paths = {
        "stage": "pre-publication-stage.json",
        "observation": f"remote-observation-{step}.json",
        "classification": f"remote-classification-{step}.json",
        "attempt": "promotion-attempt.json",
        "handoff": "publisher-handoff.json",
        "pre_publication_verification": f"pre-publication-verification-{step}.json",
    }
    return {
        **{f"{key}_path": str(directory / name) for key, name in paths.items()},
        **{
            f"{kind}_artifact_name": f"stateless-preview-promote-{kind}-{suffix}"
            + (f"-{step}" if kind in ("observation", "classification") else "")
            for kind in ("admission", "observation", "classification", "attempt")
        },
    }


def _promotion_artifact(
    inputs: Mapping[str, Any], reference: Mapping[str, Any]
) -> Mapping[str, Any]:
    reference = _review_record_reference(reference, "publisher retained reference")
    context = inputs["context"]
    if reference["repository"] != context["source"]["repository"]:
        _refuse("publisher retained reference belongs to another repository")
    metadata = _promotion_api(inputs, f"actions/artifacts/{reference['artifact_id']}")
    name = _expect_string(metadata.get("name"), "retained artifact name")
    admitted = _validate_uploaded_artifact(
        metadata,
        artifact_id=reference["artifact_id"],
        expected_name=name,
        current_run_id=reference["run_id"],
    )
    workflow = metadata["workflow_run"]
    if any(
        (
            str(workflow.get("repository_id")) != context["repository_id"],
            str(workflow.get("head_repository_id")) != context["repository_id"],
            workflow.get("head_branch") != "main",
            workflow.get("head_sha") != context["source"]["commit"],
            _review_timestamp(admitted["expires_at"], "retained artifact expiry")
            <= datetime.now(timezone.utc),
        )
    ):
        _refuse("publisher retained artifact has expired or has different provenance")
    producer_attempt = re.search(
        rf"-{reference['run_id']}-([1-9][0-9]*)(?:-[1-9][0-9]*(?:-[0-3])?)?$", name
    )
    if (
        producer_attempt is None
        and reference == inputs.get("publisher_handoff_reference")
        and reference["path"] == "publisher-handoff.json"
    ):
        producer_attempt = re.fullmatch(
            rf"stateless-preview-promote-attempt-{reference['run_id']}-([1-9][0-9]*)-[1-9][0-9]*-handoff",
            name,
        )
    if producer_attempt is None:
        _refuse("retained artifact name does not identify its producer attempt")
    attempt = int(producer_attempt[1])
    producer_key = (reference["run_id"], attempt)
    if producer_key not in inputs["producers"]:
        run = _promotion_api(inputs, f"actions/runs/{reference['run_id']}/attempts/{attempt}")
        _validate_cli_run(
            run,
            context,
            reference["run_id"],
            attempt,
            successful_build=reference["run_id"] != inputs["context"]["run_id"],
            completed_run=(
                name.startswith("stateless-preview-promote-")
                and (reference["run_id"], attempt) != (context["run_id"], context["attempt"])
            ),
        )
        inputs["producers"][producer_key] = run
    digest = metadata.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        _refuse("publisher retained container digest is unavailable")
    # Compact references do not assert configured_days; recover it only from Candidate later.
    return {
        "id": reference["artifact_id"],
        "name": name,
        "sha256": _expect_sha256(digest[7:], "container digest"),
        "retention": {"expires_at": admitted["expires_at"]},
    }


def _promotion_resolve(inputs: Mapping[str, Any], reference: Mapping[str, Any]) -> bytes:
    key = _evidence_reference_key(reference)
    if key not in inputs["resolved"]:
        artifact = _promotion_artifact(inputs, reference)
        raw = inputs["downloader"]._member_bytes(
            reference["repository"], reference["run_id"], artifact, reference["path"]
        )
        limit = (
            _PROMOTION_JSON_LIMIT
            if reference["path"].endswith(".json")
            else _PROMOTION_BINARY_LIMIT
        )
        if len(raw) > limit:
            _refuse("retained publisher member exceeds its byte bound")
        if _sha256_bytes(raw) != reference["sha256"]:
            _refuse("publisher retained raw record hash differs from its reference")
        inputs["resolved"][key] = raw
    return inputs["resolved"][key]


def _promotion_collection(inputs: Mapping[str, Any], route: str, member: str) -> list[Any]:
    entries: list[Any] = []
    total = None
    for page in range(1, 101):
        separator = "&" if "?" in route else "?"
        envelope = _promotion_api(inputs, f"{route}{separator}per_page=100&page={page}")
        if not isinstance(envelope, Mapping):
            _refuse("predecessor discovery listing is unavailable")
        count = _expect_nonnegative_integer(envelope.get("total_count"), "discovery total")
        batch = _expect_list(envelope.get(member), "discovery members")
        if (
            len(batch) > 100
            or count > (1000 if member == "workflow_runs" else 10000)
            or (total is not None and total != count)
            or any(not isinstance(entry, Mapping) for entry in batch)
        ):
            _refuse("predecessor discovery listing is incomplete or changed")
        total = count
        entries.extend(batch)
        if len(batch) < 100:
            ids = [
                str(_expect_positive_integer(item.get("id"), "discovery ID")) for item in entries
            ]
            if len(entries) != total or len(ids) != len(set(ids)):
                _refuse("predecessor discovery listing is incomplete or duplicated")
            return entries
    _refuse("predecessor discovery exceeds its pagination bound")


def _promotion_member(
    inputs: Mapping[str, Any], metadata: Mapping[str, Any], run_id: str, filename: str
) -> tuple[dict[str, str], Mapping[str, Any]]:
    reference = {
        "repository": inputs["candidate"]["source"]["repository"],
        "run_id": run_id,
        "artifact_id": str(_expect_positive_integer(metadata.get("id"), "predecessor artifact ID")),
        "path": filename,
        "sha256": "0" * 64,
    }
    artifact = _promotion_artifact(inputs, reference)
    raw = inputs["downloader"]._member_bytes(reference["repository"], run_id, artifact, filename)
    if len(raw) > _PROMOTION_JSON_LIMIT:
        _refuse("predecessor member exceeds its byte bound")
    reference["sha256"] = _sha256_bytes(raw)
    inputs["resolved"][_evidence_reference_key(reference)] = raw
    return reference, _load_json_object(raw, "predecessor record")


def _promotion_possible_operations(
    attempt: Mapping[str, Any],
    classification: Mapping[str, Any],
    observation: Mapping[str, Any],
    observation_reference: Mapping[str, Any],
) -> list[dict[str, Any]]:
    candidate, state = attempt["candidate"], classification["state"]
    common = {
        "repository": candidate["source"]["repository"],
        "candidate_identity_record": attempt["candidate_identity_record"],
    }
    targets = []
    if state == "unstarted":
        raw = _promotion_tag_bytes(attempt)
        oid = hashlib.sha1(b"tag " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        targets.append(("CREATE_TAG", {"tag": candidate["destination"]["tag"], "object_id": oid}))
    if state in ("unstarted", "tag_only"):
        targets.append(
            (
                "CREATE_DRAFT",
                {
                    "tag": candidate["destination"]["tag"],
                    "source_commit": candidate["source"]["commit"],
                },
            )
        )
    if state in ("draft_empty", "draft_partial"):
        for kind in ("archive", "manifest"):
            if observation["asset_observations"][kind]["status"] == "ABSENT":
                descriptor = (
                    candidate["preview_manifest"]["contents"]["archive"]
                    if kind == "archive"
                    else candidate["preview_manifest"]["file"]
                )
                targets.append(
                    (
                        "UPLOAD_" + kind.upper(),
                        {
                            "release_id": observation["release_observation"]["release_id"],
                            "descriptor": descriptor,
                        },
                    )
                )
    if state == "draft_complete":
        number = re.fullmatch(r"remote-observation-([0-3])\.json", observation_reference["path"])
        if number is None:
            _refuse("predecessor publication lacks the genuine observation lineage")
        fragment = _promotion_fragment({"candidate": candidate}, observation)
        fragment_reference = {
            **observation_reference,
            "path": f"pre-publication-verification-{number[1]}.json",
            "sha256": _sha256_bytes(_canonical_json_bytes(fragment, "pre-publication fragment")),
        }
        targets.append(
            (
                "PUBLISH_RELEASE",
                {
                    "release_id": observation["release_observation"]["release_id"],
                    "pre_publication_verification": fragment_reference,
                },
            )
        )
    results = []
    for operation, details in targets:
        intent = {
            "origin_attempt_id": attempt["attempt_id"],
            "operation": operation,
            "target": {**common, **details},
        }
        results.append(
            {
                **intent,
                "intent_sha256": _sha256_bytes(_canonical_json_bytes(intent, "write intent")),
                "sent_at": None,
                "response": {
                    "kind": "UNKNOWN",
                    "http_status": None,
                    "git_returncode": None,
                    "resource_id": None,
                },
                "postcondition": "UNREADABLE",
                "outcome": "UNKNOWN",
            }
        )
    return results


_PROMOTION_ADVANCEMENT_STEPS = (
    "Read back A and execute the first legal advancement",
    "Execute the second legal advancement from retained C1",
    "Execute the third and final legal advancement from retained C2",
)


def _promotion_revalidate_advancements(
    inputs: MutableMapping[str, Any],
    attempt: Mapping[str, Any],
    advancement: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], list[Any]]:
    run_id = attempt["workflow_run"]["run_id"]
    number = attempt["workflow_run"]["run_attempt"]
    job_id = attempt["authorization"]["execution_policy"]["job_id"]
    observation_reference = attempt["remote_observation"]
    classification_reference = attempt["remote_classification"]
    observation = _resolve_stage_record(observation_reference, inputs["binding"]["record_resolver"])
    classification = _resolve_stage_record(
        classification_reference, inputs["binding"]["record_resolver"]
    )
    last_results = []
    suffix = f"{run_id}-{number}-{job_id}"
    for step_number, step in enumerate(advancement, 1):
        if step.get("status") == "completed" and step.get("conclusion") == "skipped":
            if any(later.get("conclusion") != "skipped" for later in advancement[step_number:]):
                _refuse("predecessor skipped a required advancement boundary")
            break
        possible = _promotion_possible_operations(
            attempt, classification, observation, observation_reference
        )
        for possible_result in possible:
            if possible_result["operation"] == "PUBLISH_RELEASE":
                fragment_reference = possible_result["target"]["pre_publication_verification"]
                if _resolve_stage_record(
                    fragment_reference, inputs["binding"]["record_resolver"]
                ) != _promotion_fragment(inputs, observation):
                    _refuse("predecessor publication fragment is unavailable or changed")
                inputs["predecessor_fragment_reference"] = fragment_reference
        matches = [
            item
            for item in artifacts
            if item.get("name") == f"stateless-preview-promote-observation-{suffix}-{step_number}"
        ]
        if len(matches) > 1:
            _refuse("predecessor outcome artifacts are ambiguous")
        if matches:
            result_reference, result_record = _promotion_member(
                inputs, matches[0], run_id, f"remote-observation-{step_number}.json"
            )
            result_record = seal_remote_observation(
                result_record,
                record_resolver=inputs["binding"]["record_resolver"],
                **{
                    **_promotion_remote_binding(inputs),
                    "current_attempt_id": attempt["attempt_id"],
                },
            )
            _validate_operation_results(
                result_record,
                inputs["candidate"],
                attempt["candidate_identity_record"],
                inputs["binding"]["record_resolver"],
            )
            actual = result_record["operation_results"]
            if len(actual) > len(possible) or any(
                {
                    k: result[k]
                    for k in ("origin_attempt_id", "operation", "intent_sha256", "target")
                }
                != {
                    k: expected[k]
                    for k in ("origin_attempt_id", "operation", "intent_sha256", "target")
                }
                for result, expected in zip(actual, possible)
            ):
                _refuse("predecessor outcomes do not bind their original admitted intents")
            last_results = _thaw(actual)
            if step.get("status") != "completed" or step.get("conclusion") != "success":
                # A successful prefix does not prove the remaining writes were never sent.
                if all(result["outcome"] in ("CONFIRMED", "RECONCILED") for result in actual):
                    last_results.extend(_thaw(possible[len(actual) :]))
                break
            classifications = [
                item
                for item in artifacts
                if item.get("name")
                == f"stateless-preview-promote-classification-{suffix}-{step_number}"
            ]
            if len(classifications) != 1:
                _refuse("predecessor success lacks its immutable classification")
            result_classification_reference, sealed_classification = _promotion_member(
                inputs, classifications[0], run_id, f"remote-classification-{step_number}.json"
            )
            derived = classify_remote_observation(
                result_reference,
                classification_id=sealed_classification.get("classification_id"),
                classified_at=sealed_classification.get("classified_at"),
                current_workflow_run=attempt["workflow_run"],
                record_resolver=inputs["binding"]["record_resolver"],
                **{
                    **_promotion_remote_binding(inputs),
                    "current_attempt_id": attempt["attempt_id"],
                },
            )
            if (
                sealed_classification != derived
                or derived["admission"] != "ADMITTED"
                or len(actual) != len(possible)
            ):
                _refuse("predecessor advancement has no conclusive complete outcome")
            if inputs.get("publisher_completed_at") is not None and not (
                max(
                    _review_timestamp(attempt["recorded_at"], "publisher Attempt time"),
                    _review_timestamp(classification["classified_at"], "prior classification time"),
                )
                <= _review_timestamp(result_record["observed_at"], "publisher advancement time")
                <= _review_timestamp(derived["classified_at"], "publisher classification time")
                <= inputs["publisher_completed_at"]
            ):
                _refuse("publisher advancement is outside its actual retained chronology")
            observation_reference, observation, classification = (
                result_reference,
                result_record,
                derived,
            )
            classification_reference = result_classification_reference
        else:
            last_results = possible
            break
    if classification["state"] == "draft_complete":
        possible = _promotion_possible_operations(
            attempt, classification, observation, observation_reference
        )
        fragment_reference = possible[0]["target"]["pre_publication_verification"]
        if _resolve_stage_record(
            fragment_reference, inputs["binding"]["record_resolver"]
        ) != _promotion_fragment(inputs, observation):
            _refuse("predecessor pre-publication fragment differs from retained draft")
        inputs["predecessor_fragment_reference"] = fragment_reference
    return (
        observation_reference,
        classification_reference,
        observation,
        classification,
        last_results,
    )


def _promotion_discover_predecessor(
    inputs: MutableMapping[str, Any],
) -> tuple[Mapping[str, Any] | None, list[Any]]:
    context = inputs["context"]
    runs = _promotion_collection(
        inputs,
        f"actions/workflows/stateless-preview.yml/runs?head_sha={context['source']['commit']}&event=workflow_dispatch&branch=main",
        "workflow_runs",
    )
    current = [run for run in runs if str(run.get("id")) == context["run_id"]]
    if len(current) != 1:
        _refuse("complete predecessor discovery does not contain the current run")
    anchor = current[0]
    source = context["source"]
    if (
        str(_expect_positive_integer(anchor.get("id"), "history anchor run ID"))
        != context["run_id"]
        or anchor.get("event") != _TRUSTED_BUILD_EVENT
        or anchor.get("path") != _TRUSTED_WORKFLOW_PATH
        or anchor.get("head_branch") != "main"
        or anchor.get("head_sha") != source["commit"]
    ):
        _refuse("publisher history anchor does not bind the exact canonical source")
    for field in ("repository", "head_repository"):
        repository = anchor.get(field)
        if not isinstance(repository, Mapping):
            _refuse("publisher history anchor repository is unavailable")
        if (
            str(_expect_positive_integer(repository.get("id"), "history anchor repository ID"))
            != context["repository_id"]
            or repository.get("full_name") != source["repository"]
        ):
            _refuse("publisher history anchor belongs to a different repository or fork")
    current_time = _review_timestamp(anchor.get("created_at"), "current run creation")
    predecessors = []
    for run in runs:
        run_id = str(_expect_positive_integer(run.get("id"), "discovered run ID"))
        created = _review_timestamp(run.get("created_at"), "discovered run creation")
        if (created, int(run_id)) > (current_time, int(context["run_id"])):
            continue
        latest = _expect_positive_integer(run.get("run_attempt"), "discovered run attempt")
        maximum = context["attempt"] - 1 if run_id == context["run_id"] else latest
        if maximum > 100:
            _refuse("predecessor run attempts exceed their discovery bound")
        artifacts = (
            _promotion_collection(inputs, f"actions/runs/{run_id}/artifacts", "artifacts")
            if maximum
            else []
        )
        for number in range(1, maximum + 1):
            producer = _promotion_api(inputs, f"actions/runs/{run_id}/attempts/{number}")
            _validate_cli_run(
                producer, context, run_id, number, successful_build=False, completed_run=True
            )
            jobs = _promotion_collection(
                inputs, f"actions/runs/{run_id}/attempts/{number}/jobs", "jobs"
            )
            matches = [job for job in jobs if job.get("name") == _PROMOTION_JOB_NAME]
            if not matches:
                if any(
                    item.get("name", "").startswith(
                        f"stateless-preview-promote-attempt-{run_id}-{number}-"
                    )
                    for item in artifacts
                ):
                    _refuse("retained predecessor Attempt lacks its producing job")
                # Complete exact-source job discovery establishes this route never ran.
                continue
            if len(matches) != 1:
                _refuse("predecessor promote job is ambiguous")
            job = matches[0]
            job_id = str(_expect_positive_integer(job.get("id"), "predecessor job ID"))
            if (
                job.get("run_id") != int(run_id)
                or job.get("run_attempt") != number
                or job.get("head_sha") != context["source"]["commit"]
                or job.get("url")
                != f"https://api.github.com/repos/{context['source']['repository']}/actions/jobs/{job_id}"
                or job.get("status") != "completed"
            ):
                _refuse("predecessor job does not bind a finished exact-source attempt")
            steps = job.get("steps")
            if not isinstance(steps, list) or any(not isinstance(step, Mapping) for step in steps):
                _refuse("predecessor step evidence is incomplete")
            suffix = f"{run_id}-{number}-{job_id}"
            records = [
                item
                for item in artifacts
                if item.get("name") == f"stateless-preview-promote-attempt-{suffix}"
            ]
            if job.get("conclusion") == "skipped":
                if records:
                    _refuse("skipped predecessor promote job has a retained Attempt")
                if not steps:
                    continue
            advancement = []
            for name in _PROMOTION_ADVANCEMENT_STEPS:
                selected = [step for step in steps if step.get("name") == name]
                if len(selected) != 1:
                    _refuse("predecessor advancement evidence is incomplete or ambiguous")
                advancement.append(selected[0])
            if not records:
                upload = [
                    step for step in steps if step.get("name") == "Retain the immutable Attempt (A)"
                ]
                if (
                    len(upload) == 1
                    and upload[0].get("status") == "completed"
                    and upload[0].get("conclusion") == "skipped"
                    and all(
                        step.get("status") == "completed" and step.get("conclusion") == "skipped"
                        for step in advancement
                    )
                ):
                    continue
                _refuse("possibly admitted/sent predecessor lacks its immutable Attempt")
            if len(records) != 1:
                _refuse("predecessor Attempt artifact is ambiguous")
            reference, attempt = _promotion_member(
                inputs, records[0], run_id, "promotion-attempt.json"
            )
            if (
                attempt.get("candidate", {}).get("destination")
                != inputs["candidate"]["destination"]
            ):
                continue
            if attempt.get("candidate") != inputs["candidate"]:
                _refuse("predecessor destination belongs to a different Candidate")
            predecessors.append(
                (
                    created,
                    int(run_id),
                    number,
                    job_id,
                    reference,
                    attempt,
                    producer,
                    artifacts,
                    advancement,
                )
            )
    predecessors.sort(key=lambda item: item[:3])
    previous_reference, previous_results = None, []
    for (
        _,
        run_number,
        number,
        job_id,
        reference,
        attempt,
        producer,
        artifacts,
        advancement,
    ) in predecessors:
        run_id = str(run_number)
        current_context = {
            "current_attempt_id": f"a1-promote-{run_id}-{number}-{job_id}",
            "workflow_run": {
                "repository": context["source"]["repository"],
                "workflow_path": _TRUSTED_WORKFLOW_PATH,
                "mode": "promote",
                "event": _TRUSTED_BUILD_EVENT,
                "run_id": run_id,
                "run_attempt": number,
                "ref": _CANONICAL_SOURCE_REF,
                "commit": context["source"]["commit"],
            },
            "job_id": job_id,
            "job_key": "promote",
            "workflow_sha": context["source"]["commit"],
            "checkout_head": context["source"]["commit"],
            "origin_main_target": context["source"]["commit"],
            "current_github_actor": {
                "id": str(producer["actor"]["id"]),
                "login": producer["actor"]["login"],
            },
            "current_github_triggering_actor": {
                "id": str(producer["triggering_actor"]["id"]),
                "login": producer["triggering_actor"]["login"],
            },
            "predecessor_attempt": previous_reference,
            "predecessor_operation_results": previous_results,
        }
        attempt = seal_promotion_attempt(
            attempt, **inputs["binding"], current_promotion_context=current_context
        )
        *_, last_results = _promotion_revalidate_advancements(
            inputs, attempt, advancement, artifacts
        )
        previous_reference, previous_results = reference, last_results
    return previous_reference, previous_results


def _promotion_inputs(
    root: Path,
    environment: Mapping[str, str],
    event: Mapping[str, Any],
    command: str,
    step: int = 0,
) -> dict[str, Any]:
    request = _review_record_reference(
        _load_json_object(event["inputs"]["promote_request"].encode("utf-8"), "promotion request"),
        "promotion request",
    )
    if request["path"] != "promotion-decision.json":
        _refuse("promotion request must name the retained Decision")
    context = _promotion_context(root, environment, event)
    locations = _promotion_locations(environment, context, step)
    directory = Path(locations["stage_path"]).parent / f"{command}-{step}"
    directory.mkdir(parents=True, exist_ok=False)
    inputs: dict[str, Any] = {
        "root": root,
        "environment": environment,
        "event": event,
        "context": context,
        "locations": locations,
        "directory": directory,
        "resolved": {},
        "producers": {},
        "diagnostics": [],
        "downloader": ArtifactDownloader(directory, environment=environment),
        "decision_reference": request,
    }

    def resolve(reference: Mapping[str, Any]) -> bytes:
        return _promotion_resolve(inputs, reference)

    decision = _load_json_object(resolve(request), "retained Decision")
    identity_compact = _review_record_reference(
        decision.get("candidate_identity_record"), "Decision Candidate"
    )
    if identity_compact["path"] != _CANDIDATE_IDENTITY_FILENAME:
        _refuse("Decision Candidate has an unexpected path")
    identity_raw = resolve(identity_compact)
    identity = _validate_candidate_identity(_load_json_object(identity_raw, "retained Candidate"))
    candidate = identity["candidate"]
    retention = candidate["build"]["artifact"]["retention"]["configured_days"]
    _expect_retention_days({"A1_RETENTION_DAYS": str(retention)})
    locations["retention_days"] = str(retention)
    artifact = _promotion_artifact(inputs, identity_compact)
    artifact = {**artifact, "retention": {**artifact["retention"], "configured_days": retention}}
    identity_reference = {
        **{key: identity_compact[key] for key in ("repository", "run_id", "path", "sha256")},
        "artifact": artifact,
    }
    _verified_review_candidate(identity, identity_raw, identity_reference)
    names = _artifact_names(
        candidate["preview_manifest"]["contents"]["version"],
        candidate["build"]["run_id"],
        candidate["build"]["run_attempt"],
    )
    if (
        any(candidate["source"][key] != value for key, value in context["source"].items())
        or identity_compact["run_id"] != candidate["build"]["run_id"]
        or artifact["name"] != names["candidate_identity"]
        or candidate["build"]["artifact"]["name"] != names["payload"]
        or event["inputs"]["preview_tag"] != candidate["destination"]["tag"]
    ):
        _refuse("promotion candidate/destination differs from the exact canonical source build")
    completed_at = _verified_evidence_build(inputs["downloader"], context, candidate)
    stage_reference = (
        decision.get("evidence", {}).get("pre_decision_stage_evidence", {}).get("record_reference")
    )
    stage = _load_json_object(resolve(stage_reference), "pre-decision Stage")
    catalog_reference = _review_record_reference(stage.get("release_gate_catalog"), "Stage Catalog")
    if (
        catalog_reference["path"] != _RELEASE_GATE_CATALOG_FILENAME
        or catalog_reference["run_id"] != candidate["build"]["run_id"]
        or catalog_reference["artifact_id"] == identity_compact["artifact_id"]
        or _promotion_artifact(inputs, catalog_reference)["name"] != names["release_gate_catalog"]
    ):
        _refuse("promotion Catalog does not bind the distinct source-build artifact")
    catalog_raw = resolve(catalog_reference)
    binding = {
        "candidate_identity": identity,
        "candidate_identity_bytes": identity_raw,
        "candidate_identity_reference": identity_reference,
        "release_gate_catalog": _load_json_object(catalog_raw, "Catalog"),
        "release_gate_catalog_bytes": catalog_raw,
        "release_gate_catalog_reference": catalog_reference,
        "record_resolver": resolve,
        "repository_root": root,
    }
    decision = seal_promotion_decision(decision, **binding)
    if decision["decision"] != "APPROVE" or _review_timestamp(
        decision["decided_at"], "Decision time"
    ) > datetime.now(timezone.utc):
        _refuse("promotion requires an existing APPROVE, not future evidence")
    decision_artifact = _promotion_artifact(inputs, request)
    evidence_attempt = int(decision_artifact["name"].rsplit("-", 1)[-1])
    evidence_suffix = f"{request['run_id']}-{evidence_attempt}"
    if (
        decision_artifact["name"] != f"stateless-preview-evidence-decision-{evidence_suffix}"
        or stage_reference["run_id"] != request["run_id"]
        or _promotion_artifact(inputs, stage_reference)["name"]
        != f"stateless-preview-evidence-stage-{evidence_suffix}"
    ):
        _refuse("Decision/Stage do not belong to the exact retained evidence producer")
    producer = inputs["producers"][(request["run_id"], evidence_attempt)]
    author = decision["promotion_attempt"]["decision_author"]
    for role in ("actor", "triggering_actor"):
        observed = producer.get(role)
        if (
            not isinstance(observed, Mapping)
            or {"id": str(observed.get("id")), "login": observed.get("login")} != author
        ):
            _refuse("retained Decision author differs from its actual producer initiators")
    for role in ("current_github_actor", "current_github_triggering_actor"):
        if context["current"][role] != decision["promotion_attempt"]["authorized_dispatcher"]:
            _refuse("promotion initiators are not the Decision's authorized dispatcher")
    for entry in stage["gate_evidence"]:
        reference = _review_record_reference(entry["record_reference"], "pre-decision record")
        if (
            reference["run_id"] != request["run_id"]
            or _promotion_artifact(inputs, reference)["name"]
            != f"stateless-preview-evidence-records-{evidence_suffix}"
        ):
            _refuse("pre-decision proofs do not belong to the retained evidence producer")
        proof = _load_json_object(resolve(entry["record_reference"]), "pre-decision proof")
        times = [
            proof[
                "sealed_at"
                if entry["record_type"] == "s2-s3-seven-lens-aggregate"
                else "recorded_at"
            ]
        ]
        if entry["record_type"] == "s2-s3-seven-lens-aggregate":
            times.extend(lens["recorded_at"] for lens in proof["lens_results"])
        if any(
            not completed_at <= _review_timestamp(value, "proof time") <= datetime.now(timezone.utc)
            for value in times
        ):
            _refuse("promotion proof does not follow the completed source build")
    payload_artifact = candidate["build"]["artifact"]
    payload_paths = {}
    for kind in ("archive", "manifest"):
        reference = candidate["preview_manifest"][f"{kind}_reference"]
        metadata = _promotion_artifact(inputs, reference)
        if (
            metadata["name"] != payload_artifact["name"]
            or metadata["retention"]["expires_at"] != payload_artifact["retention"]["expires_at"]
        ):
            _refuse("retained payload metadata differs from Candidate")
        raw = resolve(reference)
        path = directory / "payload" / reference["path"]
        _write_bytes_once(path, raw, "retained payload")
        payload_paths[kind] = path
    verify_retained_artifact_inputs(identity, payload_paths["archive"], payload_paths["manifest"])
    archive_raw = _read_regular_bytes(payload_paths["archive"], "retained archive")
    _promotion_verify_executable(archive_raw, candidate)
    inputs.update(
        binding=binding, decision=decision, candidate=candidate, payload_paths=payload_paths
    )
    predecessor, results = _promotion_discover_predecessor(inputs)
    context["current"]["predecessor_attempt"] = predecessor
    context["current"]["predecessor_operation_results"] = results
    inputs["operation_results"] = _thaw(results) if command == "observe-promote" else []
    return inputs


def _promotion_verify_executable(archive: bytes, candidate: Mapping[str, Any]) -> None:
    descriptor = candidate["preview_manifest"]["contents"]["executable"]
    if descriptor["size_bytes"] > _PROMOTION_BINARY_LIMIT:
        _refuse("approved executable exceeds the publisher byte bound")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            entries = contents.infolist()
            if len(entries) != 1 or entries[0].file_size != descriptor["size_bytes"]:
                _refuse("preview archive member exceeds its exact approved identity")
    except (OSError, zipfile.BadZipFile):
        _refuse("preview archive is unreadable")
    executable = _read_exact_preview_archive_member(archive, _PREVIEW_EXECUTABLE)
    if (
        len(executable) != descriptor["size_bytes"]
        or _sha256_bytes(executable) != descriptor["sha256"]
    ):
        _refuse("preview executable differs from the approved manifest")


def _promotion_record(inputs: Mapping[str, Any], key: str, record: Mapping[str, Any]) -> None:
    _write_bytes_once(
        Path(inputs["locations"][f"{key}_path"]), _canonical_json_bytes(record, key), key
    )


def _promotion_stage(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _read_regular_bytes(Path(inputs["locations"]["stage_path"]), "pre-publication Stage")
    stage = seal_stage_gate_evidence(
        _load_json_object(raw, "pre-publication Stage"), **inputs["binding"]
    )
    if (
        stage["stage"] != "pre-publication"
        or stage["outcome"] != "PASS"
        or not (
            _review_timestamp(inputs["decision"]["decided_at"], "Decision time")
            <= _review_timestamp(stage["sealed_at"], "Stage time")
            <= datetime.now(timezone.utc)
        )
    ):
        _refuse("publisher Stage is outside current pre-publication admission")
    return stage


def _promotion_remote_binding(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: inputs["binding"][key]
            for key in (
                "candidate_identity",
                "candidate_identity_bytes",
                "candidate_identity_reference",
            )
        },
        "current_attempt_id": inputs["context"]["current"]["current_attempt_id"],
    }


def _promotion_remember(
    inputs: Mapping[str, Any], kind: str, step: int, reference: Mapping[str, Any]
) -> None:
    path = Path(inputs["locations"]["stage_path"]).parent / f"{kind}-reference-{step}.json"
    raw = _canonical_json_bytes(reference, "publisher readback reference")
    if path.exists():
        if _read_regular_bytes(path, "publisher readback reference") != raw:
            _refuse("publisher readback reference cannot change")
    else:
        _write_bytes_once(path, raw, "publisher readback reference")


def _promotion_uploaded(
    inputs: Mapping[str, Any],
    kind: str,
    step: int,
    filename: str,
    *,
    reference: Mapping[str, Any] | None = None,
    compare_local: bool = True,
) -> tuple[dict[str, str], bytes]:
    environment, context = inputs["environment"], inputs["context"]
    locations = _promotion_locations(environment, context, step)
    prefix = f"A1_PROMOTE_{kind.upper()}_ARTIFACT"
    if reference is None:
        artifact_id = _expect_environment_identifier(environment, prefix + "_ID")
        digest = _expect_sha256(
            _environment_value(environment, prefix + "_DIGEST"), "uploaded container digest"
        )
    else:
        reference = _review_record_reference(reference, "retained publisher predecessor")
        if (
            reference["repository"] != context["source"]["repository"]
            or reference["run_id"] != context["run_id"]
            or reference["path"] != filename
        ):
            _refuse("publisher predecessor has a different path/run/repository")
        artifact_id, digest = reference["artifact_id"], None
    artifact = _checked_evidence_artifact(
        inputs["downloader"],
        context,
        artifact_id,
        context["run_id"],
        locations[f"{kind}_artifact_name"],
        int(inputs["locations"]["retention_days"]),
        digest,
    )
    raw = inputs["downloader"]._member_bytes(
        context["source"]["repository"], context["run_id"], artifact, filename
    )
    actual = {
        "repository": context["source"]["repository"],
        "run_id": context["run_id"],
        "artifact_id": artifact_id,
        "path": filename,
        "sha256": _sha256_bytes(raw),
    }
    if reference is not None and reference != actual:
        _refuse("retained publisher predecessor raw bytes changed")
    if compare_local and raw != _read_regular_bytes(
        Path(locations["stage_path"]).parent / filename, "original publisher record"
    ):
        _refuse("publisher upload readback differs from the original bytes")
    inputs["resolved"][_evidence_reference_key(actual)] = raw
    return actual, raw


def _promotion_completed(
    inputs: Mapping[str, Any], step: int, observation_reference: Mapping[str, Any]
) -> None:
    if step == 0:
        return
    path = Path(inputs["locations"]["stage_path"]).parent / f"action-{step}-complete.json"
    record = _load_json_object(
        _read_regular_bytes(path, "completed action receipt"), "completed action receipt"
    )
    if record != {"step": step, "observation_sha256": observation_reference["sha256"]}:
        _refuse("diagnostic/unfinished action observations cannot authorize another action")


def _promotion_classification(
    inputs: Mapping[str, Any], step: int
) -> tuple[Mapping[str, Any], dict[str, str], Mapping[str, Any], dict[str, str]]:
    classification_reference, raw = _promotion_uploaded(
        inputs, "classification", step, f"remote-classification-{step}.json"
    )
    record = _load_json_object(raw, "retained classification")
    observation_reference = record.get("remote_observed_state_reference")
    kind = "admission" if step == 0 else "observation"
    observation_reference, observation_raw = _promotion_uploaded(
        inputs, kind, step, f"remote-observation-{step}.json", reference=observation_reference
    )
    _promotion_completed(inputs, step, observation_reference)
    derived = classify_remote_observation(
        observation_reference,
        classification_id=record.get("classification_id"),
        classified_at=record.get("classified_at"),
        current_workflow_run=inputs["context"]["current"]["workflow_run"],
        record_resolver=inputs["binding"]["record_resolver"],
        **_promotion_remote_binding(inputs),
    )
    if record != derived or _review_timestamp(
        record["classified_at"], "classification time"
    ) > datetime.now(timezone.utc):
        _refuse("retained classification differs from its actual observation")
    _promotion_remember(inputs, "classification", step, classification_reference)
    _promotion_remember(inputs, "observation", step, observation_reference)
    return (
        derived,
        classification_reference,
        _load_json_object(observation_raw, "observation"),
        observation_reference,
    )


def _promotion_attempt(inputs: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, str]]:
    reference, raw = _promotion_uploaded(inputs, "attempt", 0, "promotion-attempt.json")
    record = _load_json_object(raw, "retained Attempt")
    if record.get("promotion_decision") != inputs["decision_reference"]:
        _refuse("retained Attempt is bound to another Decision")
    # A remains immutable; its admission O0/C0 are not replaced by later records.
    for field, kind, filename in (
        ("remote_observation", "admission", "remote-observation-0.json"),
        ("remote_classification", "classification", "remote-classification-0.json"),
    ):
        _promotion_uploaded(inputs, kind, 0, filename, reference=record.get(field))
    stage_reference, _ = _promotion_uploaded(
        inputs,
        "admission",
        0,
        "pre-publication-stage.json",
        reference=record.get("pre_publication_stage_evidence", {}).get("record_reference"),
    )
    if (
        len(
            {
                stage_reference["artifact_id"],
                record["remote_observation"]["artifact_id"],
            }
        )
        != 1
    ):
        _refuse("initial Stage/observation must share the retained admission artifact")
    sealed = seal_promotion_attempt(
        record, **inputs["binding"], current_promotion_context=inputs["context"]["current"]
    )
    return sealed, reference


def _promotion_tag(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    tag = inputs["candidate"]["destination"]["tag"]
    ref = _promotion_api(inputs, f"git/ref/tags/{quote(tag, safe='')}", absent=True)
    if ref is None:
        # A 404 can hide inaccessible resources; only a complete successful listing proves absence.
        entries = _promotion_listing(inputs, f"git/matching-refs/tags/{quote(tag, safe='')}")
        prefix = f"refs/tags/{tag}"
        if any(
            not isinstance(item.get("ref"), str)
            or not item["ref"].startswith(prefix)
            or not isinstance(item.get("object"), Mapping)
            for item in entries
        ):
            _refuse("tag absence listing is malformed or outside the exact prefix")
        matches = [item for item in entries if item["ref"] == prefix]
        if not matches:
            return {"status": "ABSENT"}
        if len(matches) != 1:
            _refuse("exact tag listing is ambiguous")
        ref = matches[0]
    if (
        not isinstance(ref, Mapping)
        or ref.get("ref") != f"refs/tags/{tag}"
        or not isinstance(ref.get("object"), Mapping)
    ):
        _refuse("tag ref readback is incomplete")
    obj = ref["object"]
    oid = _expect_commit(obj.get("sha"), "tag object ID")
    if obj.get("type") == "commit":
        return {
            "status": "PRESENT",
            "tag": tag,
            "tag_type": "LIGHTWEIGHT",
            "object_id": oid,
            "peeled_commit": oid,
        }
    if obj.get("type") != "tag":
        _refuse("tag ref type is unsupported")
    annotated = _promotion_api(inputs, f"git/tags/{oid}")
    if (
        not isinstance(annotated, Mapping)
        or annotated.get("sha") != oid
        or annotated.get("tag") != tag
        or not isinstance(annotated.get("object"), Mapping)
        or annotated["object"].get("type") != "commit"
    ):
        _refuse("annotated tag does not point directly to a commit")
    return {
        "status": "PRESENT",
        "tag": tag,
        "tag_type": "ANNOTATED",
        "object_id": oid,
        "peeled_commit": _expect_commit(annotated["object"].get("sha"), "tag commit"),
    }


def _promotion_fragment(
    inputs: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, Any]:
    assets = observation["asset_observations"]
    return {
        "phase": "PRE_PUBLICATION",
        "expected_result": "EXPECT_SUCCESS",
        "observed_result": "SUCCESS",
        "outcome": "PASS",
        "release": {
            "repository": inputs["candidate"]["source"]["repository"],
            "release_id": observation["release_observation"]["release_id"],
            "tag": inputs["candidate"]["destination"]["tag"],
            "prerelease": True,
            **{
                f"{kind}_asset": {
                    "id": assets[kind]["metadata"]["id"],
                    **assets[kind]["downloaded_identity"],
                }
                for kind in ("archive", "manifest")
            },
        },
        "executable": inputs["candidate"]["preview_manifest"]["contents"]["executable"],
        "archive_matches_candidate": True,
        "manifest_matches_candidate": True,
        "executable_matches_manifest": True,
        "inherited_verifier_equations_pass": True,
    }


def _observe_promotion(
    inputs: MutableMapping[str, Any], *, step: int, label: str
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    candidate = inputs["candidate"]
    tag: Mapping[str, Any] = {"status": "UNREADABLE", "reason_code": "TAG_READBACK_UNAVAILABLE"}
    release: Mapping[str, Any] = {
        "status": "UNREADABLE",
        "reason_code": "RELEASE_READBACK_UNAVAILABLE",
    }
    assets: dict[str, Any] = {
        "archive": {"status": "ABSENT"},
        "manifest": {"status": "ABSENT"},
        "unexpected_asset_ids": [],
    }
    failed = False
    try:
        tag = _promotion_tag(inputs)
    except (ValueError, OSError, URLError):
        failed = True
    downloads = {}
    try:
        matches = [
            item
            for item in _promotion_listing(inputs, "releases")
            if item.get("tag_name") == candidate["destination"]["tag"]
        ]
        if len(matches) > 1:
            _refuse("multiple releases target the approved tag")
        release = {"status": "ABSENT"}
        if matches:
            release_id = str(_expect_positive_integer(matches[0].get("id"), "release ID"))
            metadata = _promotion_api(inputs, f"releases/{release_id}")
            if (
                not isinstance(metadata, Mapping)
                or str(metadata.get("id")) != release_id
                or any(
                    metadata.get(key) != matches[0].get(key)
                    for key in ("tag_name", "draft", "prerelease")
                )
            ):
                _refuse("release discovery differs from exact-ID readback")
            release = {
                "status": "PRESENT",
                "release_id": release_id,
                "tag": candidate["destination"]["tag"],
                "is_draft": _expect_boolean(metadata.get("draft"), "release draft"),
                "is_prerelease": _expect_boolean(metadata.get("prerelease"), "release prerelease"),
                "destination_metadata_status": "MATCHING",
            }
            entries = _promotion_listing(inputs, f"releases/{release_id}/assets")
            ids = [str(_expect_positive_integer(item.get("id"), "asset ID")) for item in entries]
            if len(ids) != len(set(ids)):
                _refuse("release assets contain duplicate IDs")
            expected_names = {
                candidate["preview_manifest"]["contents"]["archive"]["name"],
                candidate["preview_manifest"]["file"]["name"],
            }
            unexpected = [
                str(item["id"]) for item in entries if item.get("name") not in expected_names
            ]
            if len(unexpected) > 64:
                _refuse("release contains too many unexpected assets")
            assets["unexpected_asset_ids"] = unexpected
            for kind, descriptor in (
                ("archive", candidate["preview_manifest"]["contents"]["archive"]),
                ("manifest", candidate["preview_manifest"]["file"]),
            ):
                present = [item for item in entries if item.get("name") == descriptor["name"]]
                if len(present) > 1:
                    _refuse("release contains duplicate expected asset names")
                if not present:
                    continue
                item = present[0]
                asset_id = str(_expect_positive_integer(item.get("id"), "asset ID"))
                size = _expect_positive_integer(item.get("size"), "asset size")
                asset_metadata = {"id": asset_id, "name": descriptor["name"], "size_bytes": size}
                assets[kind] = {
                    "status": "PRESENT_UNREADABLE",
                    "metadata": asset_metadata,
                    "reason_code": kind.upper() + "_DOWNLOAD_INCOMPLETE",
                }
                if item.get("state") != "uploaded" or size > _PROMOTION_BINARY_LIMIT:
                    failed = True
                    continue
                try:
                    raw = _promotion_api(inputs, f"releases/assets/{asset_id}", binary=True)
                    identity = {
                        "name": descriptor["name"],
                        "size_bytes": len(raw),
                        "sha256": _sha256_bytes(raw),
                    }
                    if not raw:
                        _refuse("remote asset download is empty")
                    status = "MATCHES_CANDIDATE"
                    if size != descriptor["size_bytes"]:
                        status = "METADATA_SIZE_MISMATCH"
                    elif len(raw) != descriptor["size_bytes"]:
                        status = "DOWNLOADED_SIZE_MISMATCH"
                    elif identity["sha256"] != descriptor["sha256"] or item.get("digest") not in (
                        None,
                        f"sha256:{identity['sha256']}",
                    ):
                        status = "DOWNLOADED_SHA256_MISMATCH"
                    assets[kind] = {
                        "status": "PRESENT",
                        "metadata": asset_metadata,
                        "downloaded_identity": identity,
                        "observed_result": "SUCCESS"
                        if status == "MATCHES_CANDIDATE"
                        else "FAILURE",
                        "identity_status": status,
                    }
                    downloads[kind] = raw
                except (ValueError, OSError, URLError):
                    assets[kind]["reason_code"] = kind.upper() + "_DOWNLOAD_UNAVAILABLE"
                    failed = True
    except (ValueError, OSError, URLError):
        failed = True
        if release["status"] != "PRESENT":
            release = {"status": "UNREADABLE", "reason_code": "RELEASE_READBACK_INCOMPLETE"}
    observation = seal_remote_observation(
        _promotion_operation_readback(
            inputs,
            {
                "observation_schema_version": "2.0",
                "observation_id": f"{inputs['context']['current']['current_attempt_id']}-o{step}-{label}",
                "promotion_attempt_id": inputs["context"]["current"]["current_attempt_id"],
                "candidate_identity_record": _compact_evidence_reference(
                    ArtifactFileReference.from_mapping(
                        inputs["binding"]["candidate_identity_reference"], "Candidate"
                    )
                ),
                "candidate": candidate,
                "retained_artifact_availability": {
                    "status": "AVAILABLE_AND_VERIFIED",
                    "observed_result": "SUCCESS",
                },
                "tag_observation": tag,
                "release_observation": release,
                "asset_observations": assets,
                "readback_status": {
                    "expected_result": "EXPECT_SUCCESS",
                    "observed_result": "FAILURE" if failed else "SUCCESS",
                    "reason_code": "READBACK_INCOMPLETE" if failed else "READBACK_COMPLETE",
                },
                "observed_at": _utc_timestamp(),
            },
        ),
        record_resolver=inputs["binding"]["record_resolver"],
        **_promotion_remote_binding(inputs),
    )
    fragment = None
    if (
        _remote_collision_reason(observation, candidate, datetime.now(timezone.utc)) is None
        and release["status"] == "PRESENT"
        and all(assets[kind]["status"] == "PRESENT" for kind in ("archive", "manifest"))
    ):
        paths = {}
        for kind in ("archive", "manifest"):
            path = (
                inputs["directory"]
                / f"remote-{label}"
                / assets[kind]["downloaded_identity"]["name"]
            )
            _write_bytes_once(path, downloads[kind], "remote asset bytes")
            paths[kind] = path
        verify_retained_artifact_inputs(
            inputs["binding"]["candidate_identity"], paths["archive"], paths["manifest"]
        )
        _promotion_verify_executable(downloads["archive"], candidate)
        if release["is_draft"]:
            fragment = _promotion_fragment(inputs, observation)
    inputs["last_observation"] = observation
    inputs["last_fragment"] = fragment
    return observation, fragment


def _promotion_save_observation(
    inputs: Mapping[str, Any], observation: Mapping[str, Any], fragment: Mapping[str, Any] | None
) -> None:
    _promotion_record(inputs, "observation", observation)
    if fragment is not None:
        _promotion_record(inputs, "pre_publication_verification", fragment)
    _write_action_outputs(
        {
            **inputs["locations"],
            "pre_publication_verification_path": inputs["locations"][
                "pre_publication_verification_path"
            ]
            if fragment is not None
            else "",
        },
        inputs["environment"],
    )


def _admit_promote(inputs: Mapping[str, Any]) -> None:
    binding, candidate = inputs["binding"], inputs["candidate"]
    compact = _compact_evidence_reference(
        ArtifactFileReference.from_mapping(binding["candidate_identity_reference"], "Candidate")
    )
    stage = seal_stage_gate_evidence(
        {
            "evidence_schema_version": "1.0",
            "evidence_set_id": inputs["context"]["current"]["current_attempt_id"] + "-prepub",
            "producer": {
                "helper_path": "scripts/stateless_preview_artifact.py",
                "operation": "seal_stage_gate_evidence",
            },
            "release_gate_catalog": binding["release_gate_catalog_reference"],
            "candidate_identity_record": compact,
            "candidate_identity_record_sha256": compact["sha256"],
            "candidate": candidate,
            "source_ref": candidate["source"]["ref"],
            "source_commit": candidate["source"]["commit"],
            "stage": "pre-publication",
            "gate_evidence": [],
            "outcome": "PASS",
            "sealed_at": _utc_timestamp(),
        },
        **binding,
    )
    _promotion_record(inputs, "stage", stage)
    _write_action_outputs(inputs["locations"], inputs["environment"])


def _classify_promote(inputs: Mapping[str, Any], step: int) -> None:
    _promotion_stage(inputs)
    kind = "admission" if step == 0 else "observation"
    reference, _ = _promotion_uploaded(inputs, kind, step, f"remote-observation-{step}.json")
    _promotion_completed(inputs, step, reference)
    if step == 0:
        for name in ("pre-publication-stage.json",):
            predecessor = {
                **reference,
                "path": name,
                "sha256": _sha256_bytes(
                    _read_regular_bytes(
                        Path(inputs["locations"]["stage_path"]).parent / name, "admission record"
                    )
                ),
            }
            _promotion_uploaded(inputs, "admission", 0, name, reference=predecessor)
    else:
        _promotion_attempt(inputs)
    record = classify_remote_observation(
        reference,
        classification_id=f"{inputs['context']['current']['current_attempt_id']}-c{step}",
        classified_at=_utc_timestamp(),
        current_workflow_run=inputs["context"]["current"]["workflow_run"],
        record_resolver=inputs["binding"]["record_resolver"],
        **_promotion_remote_binding(inputs),
    )
    _promotion_remember(inputs, "observation", step, reference)
    _promotion_record(inputs, "classification", record)
    _write_action_outputs(
        {
            **inputs["locations"],
            "state": record["state"],
            "allowed_action": record["allowed_action"],
        },
        inputs["environment"],
    )


def _seal_promote_attempt(inputs: Mapping[str, Any]) -> None:
    _promotion_stage(inputs)
    classification, classification_reference, _, observation_reference = _promotion_classification(
        inputs, 0
    )
    if classification["admission"] != "ADMITTED":
        _refuse("collision cannot create an admitted Attempt")
    stage = _promotion_stage(inputs)

    def admission_reference(name: str) -> dict[str, str]:
        raw = _read_regular_bytes(
            Path(inputs["locations"]["stage_path"]).parent / name, "admission record"
        )
        reference = {**observation_reference, "path": name, "sha256": _sha256_bytes(raw)}
        return _promotion_uploaded(inputs, "admission", 0, name, reference=reference)[0]

    stage_reference = admission_reference("pre-publication-stage.json")
    current = inputs["context"]["current"]
    compact = _compact_evidence_reference(
        ArtifactFileReference.from_mapping(
            inputs["binding"]["candidate_identity_reference"], "Candidate"
        )
    )
    record = {
        "schema_version": "3.0",
        "attempt_id": current["current_attempt_id"],
        "candidate_identity_record": compact,
        "candidate_identity_record_sha256": compact["sha256"],
        "candidate": inputs["candidate"],
        "promotion_decision": inputs["decision_reference"],
        "promotion_decision_sha256": inputs["decision_reference"]["sha256"],
        "pre_decision_stage_evidence": inputs["decision"]["evidence"][
            "pre_decision_stage_evidence"
        ],
        "pre_publication_stage_evidence": {
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
        },
        "authorization": {
            "decision_authorization": inputs["decision"]["promotion_attempt"],
            **{
                role: current[role]
                for role in ("current_github_actor", "current_github_triggering_actor")
            },
            "execution_policy": _promotion_execution_policy(inputs["root"], current),
        },
        "workflow_run": current["workflow_run"],
        "remote_observation": observation_reference,
        "remote_classification": classification_reference,
        "predecessor_attempt": current["predecessor_attempt"],
        "admission_status": "PASS",
        "recorded_at": _utc_timestamp(),
    }
    sealed = seal_promotion_attempt(record, **inputs["binding"], current_promotion_context=current)
    _promotion_record(inputs, "attempt", sealed)
    _write_action_outputs(inputs["locations"], inputs["environment"])


class _PromotionNoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _promotion_guard(inputs: Mapping[str, Any], attempt: Mapping[str, Any]) -> None:
    fresh = _promotion_context(inputs["root"], inputs["environment"], inputs["event"])
    predecessor, results = _promotion_discover_predecessor(inputs)
    fresh["current"]["predecessor_attempt"] = predecessor
    fresh["current"]["predecessor_operation_results"] = results
    if fresh != inputs["context"]:
        _refuse("promotion runtime context changed before the protected write")
    inputs["resolved"].clear()
    inputs["producers"].clear()
    current, _ = _promotion_attempt(inputs)
    if current != attempt:
        _refuse("retained Attempt changed before the protected write")
    for kind in ("archive", "manifest"):
        reference = inputs["candidate"]["preview_manifest"][f"{kind}_reference"]
        metadata = _promotion_artifact(inputs, reference)
        if (
            metadata["retention"]["expires_at"]
            != inputs["candidate"]["build"]["artifact"]["retention"]["expires_at"]
        ):
            _refuse("payload retention changed before the protected write")
        if _promotion_resolve(inputs, reference) != _read_regular_bytes(
            inputs["payload_paths"][kind], "approved payload"
        ):
            _refuse("retained payload changed before the protected write")


def _promotion_same_remote(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(
        expected[key] == actual[key]
        for key in (
            "tag_observation",
            "release_observation",
            "asset_observations",
            "readback_status",
            "retained_artifact_availability",
        )
    )


def _promotion_require_state(
    inputs: Mapping[str, Any], observation: Mapping[str, Any], states: tuple[str, ...]
) -> None:
    if any(
        result["outcome"] not in ("CONFIRMED", "RECONCILED")
        for result in observation["operation_results"]
    ):
        _refuse("original operation is DENIED, COLLISION or unresolved UNKNOWN; sender stopped")
    if (
        _remote_collision_reason(observation, inputs["candidate"], datetime.now(timezone.utc))
        is not None
    ):
        _refuse("protected write postcondition is collision/unreadable")
    tag, release, assets = (
        observation["tag_observation"],
        observation["release_observation"],
        observation["asset_observations"],
    )
    if tag["status"] == "ABSENT":
        state = "unstarted"
    elif release["status"] == "ABSENT":
        state = "tag_only"
    elif not release["is_draft"]:
        state = "published_complete"
    else:
        count = sum(assets[kind]["status"] == "PRESENT" for kind in ("archive", "manifest"))
        state = ("draft_empty", "draft_partial", "draft_complete")[count]
    if state not in states:
        _refuse("protected write postcondition was not established; the write is not replayable")


def _promotion_intent(
    inputs: MutableMapping[str, Any], step: int, operation: str, details: Mapping[str, Any]
) -> MutableMapping[str, Any]:
    names = {
        "tag": "CREATE_TAG",
        "draft": "CREATE_DRAFT",
        "upload-archive": "UPLOAD_ARCHIVE",
        "upload-manifest": "UPLOAD_MANIFEST",
        "publish": "PUBLISH_RELEASE",
    }
    candidate = inputs["candidate"]
    target = {
        "repository": candidate["source"]["repository"],
        "candidate_identity_record": _compact_evidence_reference(
            ArtifactFileReference.from_mapping(
                inputs["binding"]["candidate_identity_reference"], "Candidate"
            )
        ),
        **details,
    }
    intent = {
        "origin_attempt_id": inputs["context"]["current"]["current_attempt_id"],
        "operation": names[operation],
        "target": target,
    }
    path = Path(inputs["locations"]["stage_path"]).parent / f"write-{step}-{operation}.json"
    raw = _canonical_json_bytes(intent, "write intent")
    # A pre-intent readback cannot describe this write's possible outcome.
    inputs.pop("last_observation", None)
    inputs.pop("last_fragment", None)
    _write_bytes_once(path, raw, "single-send write intent")
    output = {
        "tag": "tag",
        "draft": "draft",
        "upload-archive": "archive",
        "upload-manifest": "manifest",
        "publish": "publish",
    }[operation]
    _write_action_outputs({f"{output}_intent_path": str(path)}, inputs["environment"])
    result = {
        **intent,
        "intent_sha256": _sha256_bytes(raw),
        "sent_at": _utc_timestamp(),
        "response": {
            "kind": "UNKNOWN",
            "http_status": None,
            "git_returncode": None,
            "resource_id": None,
        },
        "postcondition": "UNREADABLE",
        "outcome": "UNKNOWN",
    }
    inputs.setdefault("operation_results", []).append(result)
    return result


def _promotion_http_response(status: int | None, resource_id: str | None = None) -> dict[str, Any]:
    kind = "UNKNOWN"
    if status in (409, 422):
        kind = "CONFLICT"
    elif status is not None and 400 <= status < 500:
        kind = "DENIED"
    elif status in (200, 201) and resource_id is not None:
        kind = "ACKNOWLEDGED"
    return {
        "kind": kind,
        "http_status": status or None,
        "git_returncode": None,
        "resource_id": resource_id,
    }


def _promotion_operation_readback(
    inputs: MutableMapping[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    results = _thaw(inputs.get("operation_results", []))
    for result in results:
        post = _operation_postcondition(
            result, observation, inputs["candidate"], inputs["binding"]["record_resolver"]
        )
        result["postcondition"] = post
        result["outcome"] = _operation_outcome(result["response"], post)
    observation["operation_results"] = results
    return observation


def _promotion_tag_bytes(attempt: Mapping[str, Any]) -> bytes:
    candidate = attempt["candidate"]
    epoch = int(_review_timestamp(attempt["recorded_at"], "Attempt time").timestamp())
    return (
        f"object {candidate['source']['commit']}\ntype commit\ntag {candidate['destination']['tag']}\n"
        "tagger github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com> "
        f"{epoch} +0000\n\nA1 opt-in preview\ncandidate-identity-sha256 {attempt['candidate_identity_record_sha256']}\n"
        f"promotion-decision-sha256 {attempt['promotion_decision_sha256']}\n"
    ).encode("utf-8")


def _promotion_git_path(path: Path, mode: int) -> Path:
    if not path.is_absolute():
        _refuse("native Git metadata path must be absolute")
    for current in (*reversed(path.parents), path):
        metadata = current.lstat()
        expected = mode if current == path else stat.S_IFDIR
        if stat.S_IFMT(metadata.st_mode) != expected or getattr(
            metadata, "st_file_attributes", 0
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            _refuse("native Git metadata path is redirected or has the wrong type")
    return path.resolve(strict=True)


def _promotion_source_objects(root: Path, child: Mapping[str, str]) -> Path:
    code, raw = _promotion_process(
        root,
        child,
        [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
        ],
    )
    paths = raw.decode("utf-8").splitlines()
    if code != 0 or len(paths) != 3:
        _refuse("native Git source metadata is unavailable")
    native_root, git_dir, common = (
        _promotion_git_path(Path(value), stat.S_IFDIR) for value in paths
    )
    if native_root != root:
        _refuse("native Git source metadata belongs to another checkout")

    def metadata_target(path: Path, prefix: str = "") -> Path:
        path = _promotion_git_path(path, stat.S_IFREG)
        with path.open("rb") as source:
            raw = source.read(32769)
        text = raw.decode("utf-8").rstrip("\r\n")
        if (
            len(raw) > 32768
            or not text.startswith(prefix)
            or any(character in text for character in ("\0", "\r", "\n"))
            or not text[len(prefix) :]
        ):
            _refuse("native Git source metadata pointer is invalid")
        return _promotion_git_path(path.parent / text[len(prefix) :], stat.S_IFDIR)

    marker = root / ".git"
    expected_git = (
        _promotion_git_path(marker, stat.S_IFDIR)
        if marker.is_dir()
        else metadata_target(marker, "gitdir: ")
    )
    try:
        (git_dir / "commondir").lstat()
    except FileNotFoundError:
        expected_common = git_dir
    else:
        expected_common = metadata_target(git_dir / "commondir")
    if git_dir != expected_git or common != expected_common:
        _refuse("native Git object store differs from the checkout metadata")
    objects = _promotion_git_path(common / "objects", stat.S_IFDIR)
    for name in ("info", "pack"):
        _promotion_git_path(objects / name, stat.S_IFDIR)
    for name in ("alternates", "http-alternates"):
        try:
            (objects / "info" / name).lstat()
        except FileNotFoundError:
            continue
        _refuse("native Git source object store must not redirect to other stores")
    return objects


def _promotion_create_tag(inputs: Mapping[str, Any], attempt: Mapping[str, Any], step: int) -> str:
    candidate = inputs["candidate"]
    raw = _promotion_tag_bytes(attempt)
    root = _promotion_git_path(Path(inputs["root"]), stat.S_IFDIR)
    temporary = _promotion_git_path(
        Path(_environment_value(inputs["environment"], "RUNNER_TEMP")), stat.S_IFDIR
    )
    if _path_within(temporary, root):
        _refuse("private Git transport must be outside the source checkout")
    child = _promotion_environment(inputs["environment"])
    token = child["GITHUB_TOKEN"]
    repository = candidate["source"]["repository"]
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode(
        "ascii"
    )
    source_objects = _promotion_source_objects(root, child)
    settings = [
        ("credential.helper", ""),
        ("credential.interactive", "false"),
        ("http.extraHeader", ""),
        ("http.https://github.com/.extraHeader", ""),
        (f"http.https://github.com/{repository}.git.extraHeader", header),
        ("http.followRedirects", "false"),
        ("http.proxy", ""),
        ("core.hooksPath", os.devnull),
        ("push.followTags", "false"),
        ("push.recurseSubmodules", "no"),
    ]
    child["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        child[f"GIT_CONFIG_KEY_{index}"], child[f"GIT_CONFIG_VALUE_{index}"] = key, value
    with tempfile.TemporaryDirectory(prefix="stateless-preview-git-", dir=temporary) as directory:
        private = _promotion_git_path(Path(directory), stat.S_IFDIR)
        code, _ = _promotion_process(
            private, child, ["git", "init", "--bare", "--template=", "--object-format=sha1"]
        )
        if code != 0:
            _refuse("native Git could not initialize the private tag object store")
        # Only object bytes are shared; checkout config, refs and hooks stay outside this Git.
        _write_bytes_once(
            private / "objects" / "info" / "alternates",
            (source_objects.as_posix() + "\n").encode("utf-8"),
            "private Git source object reference",
        )
        commit = _expect_commit(candidate["source"]["commit"], "native Git source commit")
        code, result = _promotion_process(private, child, ["git", "cat-file", "-t", commit])
        if code != 0 or result.strip() != b"commit":
            _refuse("private Git cannot resolve the exact source commit")
        code, result = _promotion_process(private, child, ["git", "mktag"], data=raw)
        if code != 0:
            _refuse("native Git could not construct the exact annotated tag")
        oid = _expect_commit(result.decode("ascii").strip(), "native annotated tag OID")
        expected = hashlib.sha1(b"tag " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if oid != expected:
            _refuse("native Git tag OID does not identify the exact approved bytes")
        result_record = _promotion_intent(
            inputs, step, "tag", {"tag": candidate["destination"]["tag"], "object_id": oid}
        )
        tag = candidate["destination"]["tag"]
        code, result = _promotion_process(
            private,
            child,
            [
                "git",
                "push",
                "--porcelain",
                "--no-follow-tags",
                f"--force-with-lease=refs/tags/{tag}:",
                f"https://github.com/{repository}.git",
                f"{oid}:refs/tags/{tag}",
            ],
        )
        lines = [
            line.split("\t")
            for line in result.decode("utf-8", errors="replace").splitlines()
            if "\t" in line
        ]
        inputs["diagnostics"].append(
            {
                "operation": "GIT_CREATE_TAG",
                "returncode": code,
                "porcelain_flags": [
                    line[0] for line in lines if line[0] in ("*", "!", "=", "+", "-", " ")
                ],
                "known_object_id": oid,
            }
        )
        kind = "UNKNOWN"
        if (
            len(lines) == 1
            and lines[0][0] == "*"
            and lines[0][1] == f"{oid}:refs/tags/{tag}"
            and code == 0
        ):
            kind = "ACKNOWLEDGED"
        elif any(line[0] == "!" for line in lines):
            kind = "DENIED"
        elif lines:
            kind = "CONFLICT"
        result_record["response"] = {
            "kind": kind,
            "http_status": None,
            "git_returncode": code,
            "resource_id": None,
        }
        return oid


def _promotion_write_json(
    inputs: MutableMapping[str, Any], step: int, operation: str, route: str, method: str, body: Any
) -> str | None:
    candidate = inputs["candidate"]
    details = (
        {"tag": candidate["destination"]["tag"], "source_commit": candidate["source"]["commit"]}
        if operation == "draft"
        else {
            "release_id": route.rsplit("/", 1)[1],
            "pre_publication_verification": inputs["publication_fragment_reference"],
        }
    )
    result = _promotion_intent(inputs, step, operation, details)
    url = f"https://api.github.com/repos/{candidate['source']['repository']}/{route}"
    raw = _canonical_json_bytes(body, "publisher request")
    request = Request(
        url,
        data=raw,
        method=method,
        headers={
            "Authorization": "Bearer " + _environment_value(inputs["environment"], "GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
        },
    )
    status, resource_id = None, None
    try:
        with build_opener(ProxyHandler({}), _PromotionNoRedirect()).open(
            request, timeout=60
        ) as response:
            status = response.status
            wire = response.read(_PROMOTION_JSON_LIMIT + 1)
            if len(wire) > _PROMOTION_JSON_LIMIT or response.geturl() != url:
                _refuse("mutation response exceeds bounds or changes destination")
            if status == (200 if operation == "publish" else 201):
                metadata = _load_json_object(wire, "mutation resource")
                resource_id = str(
                    _expect_positive_integer(metadata.get("id"), "mutation resource ID")
                )
    except HTTPError as error:
        status = error.code
    except (ValueError, OSError, URLError):
        pass
    result["response"] = _promotion_http_response(status, resource_id)
    inputs["diagnostics"].append({"operation": method, "endpoint": route, "http_status": status})
    return resource_id


def _promotion_upload(
    inputs: MutableMapping[str, Any], step: int, release_id: str, kind: str
) -> str | None:
    release = _promotion_api(inputs, f"releases/{release_id}")
    if (
        not isinstance(release, Mapping)
        or str(release.get("id")) != release_id
        or release.get("tag_name") != inputs["candidate"]["destination"]["tag"]
        or release.get("draft") is not True
        or release.get("prerelease") is not True
    ):
        _refuse("asset upload release is not the exact matching draft")
    descriptor = (
        inputs["candidate"]["preview_manifest"]["contents"]["archive"]
        if kind == "archive"
        else inputs["candidate"]["preview_manifest"]["file"]
    )
    entries = _promotion_listing(inputs, f"releases/{release_id}/assets")
    if any(item.get("name") == descriptor["name"] for item in entries):
        _refuse("asset upload would overwrite an existing name")
    expected_url = f"https://uploads.github.com/repos/{inputs['candidate']['source']['repository']}/releases/{release_id}/assets"
    upload_url = release.get("upload_url")
    if upload_url not in (expected_url, expected_url + "{?name,label}"):
        _refuse("asset upload URL does not bind the exact HTTPS repository/release ID")
    parsed = urlsplit(expected_url)
    if (
        parsed.hostname != "uploads.github.com"
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        _refuse("asset upload target is not trusted")
    raw = _read_regular_bytes(inputs["payload_paths"][kind], "approved upload payload")
    _verify_recorded_file(inputs["payload_paths"][kind], raw, descriptor, "approved upload payload")
    url = expected_url + "?" + urlencode({"name": descriptor["name"]})
    request = Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": "Bearer " + _environment_value(inputs["environment"], "GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": "application/zip" if kind == "archive" else "application/json",
            "Content-Length": str(len(raw)),
        },
    )
    result = _promotion_intent(
        inputs,
        step,
        f"upload-{kind}",
        {
            "release_id": release_id,
            "descriptor": descriptor,
        },
    )
    status, resource_id = None, None
    try:
        with build_opener(ProxyHandler({}), _PromotionNoRedirect()).open(
            request, timeout=60
        ) as response:
            status = response.status
            wire = response.read(_PROMOTION_JSON_LIMIT + 1)
            if len(wire) > _PROMOTION_JSON_LIMIT or response.geturl() != url:
                _refuse("asset upload response exceeds bounds or changes destination")
            if status == 201:
                metadata = _load_json_object(wire, "asset upload response")
                if (
                    metadata.get("name") != descriptor["name"]
                    or metadata.get("size") != len(raw)
                    or metadata.get("state") != "uploaded"
                ):
                    _refuse("asset upload response identity is incomplete")
                resource_id = str(_expect_positive_integer(metadata.get("id"), "uploaded asset ID"))
    except HTTPError as error:
        status = error.code
    except (URLError, OSError, ValueError):
        pass
    result["response"] = _promotion_http_response(status, resource_id)
    inputs["diagnostics"].append(
        {"operation": "UPLOAD", "release_id": release_id, "kind": kind, "http_status": status}
    )
    return resource_id


def _promotion_retained_fragment(
    inputs: Mapping[str, Any],
    step: int,
    observation: Mapping[str, Any],
    observation_reference: Mapping[str, Any],
) -> tuple[Mapping[str, Any], dict[str, str]]:
    filename = f"pre-publication-verification-{step}.json"
    local = Path(inputs["locations"]["stage_path"]).parent / filename
    reference = {
        **observation_reference,
        "path": filename,
        "sha256": _sha256_bytes(_read_regular_bytes(local, "pre-publication fragment")),
    }
    reference, raw = _promotion_uploaded(
        inputs, "admission" if step == 0 else "observation", step, filename, reference=reference
    )
    fragment = _load_json_object(raw, "retained pre-publication fragment")
    if (
        fragment != _promotion_fragment(inputs, observation)
        or observation["release_observation"].get("is_draft") is not True
    ):
        _refuse("retained fragment does not prove these exact pre-publication bytes/IDs")
    return fragment, reference


def _advance_promote(inputs: MutableMapping[str, Any], step: int) -> None:
    attempt, _ = _promotion_attempt(inputs)
    classification, _, previous, previous_reference = _promotion_classification(inputs, step - 1)
    if classification["admission"] != "ADMITTED" or classification["state"] in (
        "collision",
        "published_complete",
    ):
        _refuse("this classification authorizes no publisher action block")
    _promotion_guard(inputs, attempt)
    observed, fragment = _observe_promotion(inputs, step=step, label="before")
    if not _promotion_same_remote(previous, observed):
        _refuse("remote state changed; a different action cannot be selected implicitly")
    state = classification["state"]
    if state in ("unstarted", "tag_only"):
        known_oid = None
        if state == "unstarted":
            known_oid = _promotion_create_tag(inputs, attempt, step)
            observed, _ = _observe_promotion(inputs, step=step, label="tag")
            _promotion_require_state(inputs, observed, ("tag_only",))
            if observed["tag_observation"]["object_id"] != known_oid:
                _refuse("tag object changed after create-only push")
        _promotion_guard(inputs, attempt)
        before_draft, _ = _observe_promotion(inputs, step=step, label="before-draft")
        if not _promotion_same_remote(observed, before_draft):
            _refuse("tag/release changed immediately before draft creation")
        candidate = inputs["candidate"]
        release_id = _promotion_write_json(
            inputs,
            step,
            "draft",
            "releases",
            "POST",
            {
                "tag_name": candidate["destination"]["tag"],
                "target_commitish": candidate["source"]["commit"],
                "name": candidate["destination"]["tag"],
                "body": "",
                "draft": True,
                "prerelease": True,
                "generate_release_notes": False,
                "make_latest": "false",
            },
        )
        observed, fragment = _observe_promotion(inputs, step=step, label="draft")
        _promotion_require_state(inputs, observed, ("draft_empty",))
        if observed["tag_observation"] != before_draft["tag_observation"] or (
            release_id is not None and observed["release_observation"]["release_id"] != release_id
        ):
            _refuse("draft write postcondition does not bind its exact resource/tag")
    elif state in ("draft_empty", "draft_partial"):
        release_id = observed["release_observation"]["release_id"]
        for kind in ("archive", "manifest"):
            if previous["asset_observations"][kind]["status"] != "ABSENT":
                continue
            _promotion_guard(inputs, attempt)
            fresh, _ = _observe_promotion(inputs, step=step, label=f"before-{kind}")
            if not _promotion_same_remote(observed, fresh):
                _refuse("draft/assets changed before the exact missing-asset upload")
            asset_id = _promotion_upload(inputs, step, release_id, kind)
            after, fragment = _observe_promotion(inputs, step=step, label=f"after-{kind}")
            _promotion_require_state(inputs, after, ("draft_partial", "draft_complete"))
            asset = after["asset_observations"][kind]
            if (
                after["release_observation"] != observed["release_observation"]
                or after["tag_observation"] != observed["tag_observation"]
                or asset["status"] != "PRESENT"
                or (asset_id is not None and asset["metadata"]["id"] != asset_id)
            ):
                _refuse(
                    "asset write UNKNOWN: exact approved identity/ID postcondition is unavailable"
                )
            other = "manifest" if kind == "archive" else "archive"
            if after["asset_observations"][other] != observed["asset_observations"][other]:
                _refuse("another asset changed during the upload")
            observed = after
        _promotion_require_state(inputs, observed, ("draft_complete",))
    elif state == "draft_complete":
        retained, retained_reference = _promotion_retained_fragment(
            inputs, step - 1, previous, previous_reference
        )
        if fragment != retained:
            _refuse("fresh draft verification differs from retained pre-publication evidence")
        _promotion_guard(inputs, attempt)
        fresh, fresh_fragment = _observe_promotion(inputs, step=step, label="publish-check")
        if not _promotion_same_remote(observed, fresh) or fresh_fragment != retained:
            _refuse("draft changed immediately before publication")
        release_id = fresh["release_observation"]["release_id"]
        inputs["publication_fragment_reference"] = retained_reference
        response_id = _promotion_write_json(
            inputs,
            step,
            "publish",
            f"releases/{release_id}",
            "PATCH",
            {"draft": False, "prerelease": True, "make_latest": "false"},
        )
        observed, fragment = _observe_promotion(inputs, step=step, label="published")
        _promotion_require_state(inputs, observed, ("published_complete",))
        expected_release = {**fresh["release_observation"], "is_draft": False}
        if (
            (response_id is not None and response_id != release_id)
            or observed["release_observation"] != expected_release
            or observed["tag_observation"] != fresh["tag_observation"]
            or observed["asset_observations"] != fresh["asset_observations"]
        ):
            _refuse("publication postcondition changed exact tag/release/assets")
    else:
        _refuse("classification has no legal publisher action")
    _promotion_save_observation(inputs, observed, fragment)
    path = Path(inputs["locations"]["stage_path"]).parent / f"action-{step}-complete.json"
    _write_bytes_once(
        path,
        _canonical_json_bytes(
            {
                "step": step,
                "observation_sha256": _sha256_bytes(
                    _read_regular_bytes(
                        Path(inputs["locations"]["observation_path"]),
                        "completed action observation",
                    )
                ),
            },
            "completed action receipt",
        ),
        "completed action receipt",
    )


def _finish_promote(inputs: MutableMapping[str, Any], step: int) -> None:
    attempt, attempt_reference = _promotion_attempt(inputs)
    classification, classification_reference, observation, observation_reference = (
        _promotion_classification(inputs, step)
    )
    if classification["state"] != "published_complete" or classification["admission"] != "ADMITTED":
        _refuse("publisher handoff requires retained published_complete")
    fresh, _ = _observe_promotion(inputs, step=step, label="finish")
    if not _promotion_same_remote(observation, fresh):
        _refuse("published resources changed before the publisher handoff")
    fragment_reference = None
    for predecessor in range(step - 1, -1, -1):
        path = (
            Path(inputs["locations"]["stage_path"]).parent
            / f"observation-reference-{predecessor}.json"
        )
        reference = _load_json_object(
            _read_regular_bytes(path, "retained predecessor locator"),
            "retained predecessor locator",
        )
        _, raw = _promotion_uploaded(
            inputs,
            "admission" if predecessor == 0 else "observation",
            predecessor,
            f"remote-observation-{predecessor}.json",
            reference=reference,
        )
        previous = seal_remote_observation(
            _load_json_object(raw, "retained predecessor observation"),
            **_promotion_remote_binding(inputs),
        )
        if previous["release_observation"].get("is_draft") is True and all(
            previous["asset_observations"][kind]["status"] == "PRESENT"
            for kind in ("archive", "manifest")
        ):
            _promotion_require_state(inputs, previous, ("draft_complete",))
            fragment, fragment_reference = _promotion_retained_fragment(
                inputs, predecessor, previous, reference
            )
            if fragment["release"] != _promotion_fragment(inputs, observation)["release"]:
                _refuse("published identities differ from predecessor pre-publication evidence")
            break
    if fragment_reference is None:
        fragment_reference = inputs.get("predecessor_fragment_reference")
        if fragment_reference is not None and _resolve_stage_record(
            fragment_reference, inputs["binding"]["record_resolver"]
        ) != _promotion_fragment(inputs, observation):
            _refuse("predecessor pre-publication fragment does not match published identities")
    record = {
        "candidate_identity_record": attempt["candidate_identity_record"],
        "release_gate_catalog": inputs["binding"]["release_gate_catalog_reference"],
        "promotion_decision": inputs["decision_reference"],
        "pre_decision_stage_evidence": attempt["pre_decision_stage_evidence"]["record_reference"],
        "pre_publication_stage_evidence": attempt["pre_publication_stage_evidence"][
            "record_reference"
        ],
        "promotion_attempt": attempt_reference,
        "remote_observation": observation_reference,
        "remote_classification": classification_reference,
        "pre_publication_verification": fragment_reference,
    }
    _promotion_record(inputs, "handoff", record)
    _write_action_outputs(
        {**inputs["locations"], "state": "published_complete"}, inputs["environment"]
    )


def _run_promote(
    root: Path, environment: Mapping[str, str], event: Mapping[str, Any], command: str, step: int
) -> None:
    inputs = _promotion_inputs(root, environment, event, command, step)
    try:
        if command == "admit-promote":
            _admit_promote(inputs)
        elif command == "observe-promote":
            _promotion_stage(inputs)
            observation, fragment = _observe_promotion(inputs, step=0, label="initial")
            _promotion_save_observation(inputs, observation, fragment)
        elif command == "classify-promote":
            _classify_promote(inputs, step)
        elif command == "seal-promote-attempt":
            _seal_promote_attempt(inputs)
        elif command == "advance-promote":
            _advance_promote(inputs, step)
        elif command == "finish-promote":
            _finish_promote(inputs, step)
        else:
            _refuse("unknown publisher command")
    finally:
        if (
            command == "advance-promote"
            and "last_observation" in inputs
            and not Path(inputs["locations"]["observation_path"]).exists()
        ):
            _promotion_save_observation(inputs, inputs["last_observation"], inputs["last_fragment"])
        path = inputs["directory"] / "diagnostics.json"
        raw = _canonical_json_bytes(inputs["diagnostics"], "publisher HTTP diagnostics")
        if len(raw) > _PROMOTION_JSON_LIMIT:
            _refuse("publisher diagnostics exceed their byte bound")
        _write_bytes_once(path, raw, "publisher diagnostics")
        _write_action_outputs({"diagnostics_path": str(path)}, environment)


def _write_action_outputs(values: Mapping[str, str], environment: Mapping[str, str]) -> None:
    if any("\r" in value or "\n" in value for value in values.values()):
        _refuse("GitHub Actions output contains unsupported characters")
    try:
        with Path(_environment_value(environment, "GITHUB_OUTPUT")).open(
            "a", encoding="utf-8", newline="\n"
        ) as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
    except OSError:
        _refuse("GitHub Actions output file is unavailable")


def _write_admission_outputs(admission: Mapping[str, Any], environment: Mapping[str, str]) -> None:
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
    _write_action_outputs(values, environment)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "admit-build",
        "prepare-payload",
        "seal-build-records",
        "admit-evidence",
        "seal-evidence-stage",
        "seal-evidence-decision",
        "verify-evidence-retention",
        *_CLOSEOUT_COMMANDS,
        "admit-promote",
        "observe-promote",
        "seal-promote-attempt",
    ):
        commands.add_parser(command)
    for command in ("classify-promote", "advance-promote", "finish-promote"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument(
            "--step",
            type=int,
            required=True,
            choices=range(1, 4) if command == "advance-promote" else range(4),
        )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        event = _read_cli_dispatch(arguments.command, os.environ)
        if arguments.command == "admit-build":
            _write_admission_outputs(admit_build(Path.cwd(), os.environ), os.environ)
        elif arguments.command == "prepare-payload":
            prepare_preview_payload(Path.cwd(), os.environ)
        elif arguments.command == "seal-build-records":
            seal_build_records(Path.cwd(), os.environ)
        elif arguments.command == "admit-evidence":
            _admit_evidence(Path.cwd(), os.environ, event)
        elif arguments.command == "seal-evidence-stage":
            _seal_evidence_stage(Path.cwd(), os.environ, event)
        elif arguments.command == "seal-evidence-decision":
            _seal_evidence_decision(Path.cwd(), os.environ, event)
        elif arguments.command == "verify-evidence-retention":
            _verify_evidence_retention(Path.cwd(), os.environ, event)
        elif arguments.command in _CLOSEOUT_COMMANDS:
            _run_closeout(Path.cwd(), os.environ, event, arguments.command)
        elif arguments.command in (
            "admit-promote",
            "observe-promote",
            "classify-promote",
            "seal-promote-attempt",
            "advance-promote",
            "finish-promote",
        ):
            _run_promote(
                Path.cwd(), os.environ, event, arguments.command, getattr(arguments, "step", 0)
            )
        else:
            raise AssertionError("unreachable")
    except (
        ValueError,
        OSError,
        UnicodeError,
        RecursionError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        message = "STATELESS_PREVIEW_ARTIFACT_REFUSED"
        if arguments.command.endswith("promote") or arguments.command == "seal-promote-attempt":
            reason = str(error).replace("\r", " ").replace("\n", " ")
            for key in (
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GH_ENTERPRISE_TOKEN",
            ):
                secret = os.environ.get(key)
                if secret:
                    reason = reason.replace(secret, "[REDACTED]")
            message += ": " + reason[:512]
        print(message, file=sys.stderr)
        return 1
    print("STATELESS_PREVIEW_ARTIFACT_READY")
    return 0


__all__ = [
    "ArtifactDownloader",
    "ArtifactFileReference",
    "admit_build",
    "assemble_candidate_identity",
    "classify_remote_observation",
    "consumer_proof_scenario_catalog",
    "decode_evidence_bundle",
    "main",
    "parse_args",
    "prepare_preview_payload",
    "resolve_release_gate_catalog",
    "seal_artifact_consumer_proof",
    "seal_build_records",
    "seal_independent_pr_review",
    "seal_promotion_attempt",
    "seal_promotion_decision",
    "seal_program_b_handoff",
    "seal_remote_observation",
    "seal_remote_verification",
    "seal_s2_s3_lens_result",
    "seal_s2_s3_review_aggregate",
    "seal_stage_gate_evidence",
    "validate_artifact_consumer_proof_reference",
    "verify_and_extract_retained_artifact",
    "verify_retained_artifact",
    "verify_retained_artifact_inputs",
]


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
