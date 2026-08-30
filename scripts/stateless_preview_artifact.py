"""Fail-closed identity and retained-byte helpers for the A1 preview artifact."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import io
import json
from os import PathLike
from pathlib import Path
import re
import subprocess
from typing import Any
import zipfile


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


def _refuse(message: str) -> None:
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
        ("repository", "ref", "commit", "origin_main_target", "post_merge_exact_head_receipt"),
    )
    source_repository = _expect_repository(source["repository"], "candidate.source.repository")
    if source["ref"] != _CANONICAL_SOURCE_REF:
        _refuse("candidate.source.ref is not canonical main")
    source_commit = _expect_commit(source["commit"], "candidate.source.commit")
    origin_main_target = _expect_commit(
        source["origin_main_target"], "candidate.source.origin_main_target"
    )

    post_merge_receipt = _expect_mapping(
        source["post_merge_exact_head_receipt"],
        "candidate.source.post_merge_exact_head_receipt",
        ("record_reference", "stage", "record_type", "scanned_commit", "tag_target", "outcome"),
    )
    post_merge_reference = _validate_artifact_file_reference(
        post_merge_receipt["record_reference"],
        "candidate.source.post_merge_exact_head_receipt.record_reference",
    )
    if post_merge_reference["repository"] != source_repository:
        _refuse("candidate post-merge receipt has a different repository")
    if (
        post_merge_receipt["stage"] != "post-merge"
        or post_merge_receipt["record_type"] != "sonarqube-exact-head"
        or post_merge_receipt["outcome"] != "PASS"
    ):
        _refuse("candidate post-merge receipt is not a passing exact-head receipt")
    scanned_commit = _expect_commit(
        post_merge_receipt["scanned_commit"],
        "candidate.source.post_merge_exact_head_receipt.scanned_commit",
    )
    tag_target = _expect_commit(
        post_merge_receipt["tag_target"],
        "candidate.source.post_merge_exact_head_receipt.tag_target",
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

    if len({source_commit, origin_main_target, build_commit, scanned_commit, tag_target}) != 1:
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


def verify_retained_artifact(
    identity: Mapping[str, Any],
    archive_path: str | PathLike[str],
    manifest_path: str | PathLike[str],
    executable_path: str | PathLike[str],
) -> Mapping[str, Any]:
    """Replay every candidate byte equation before an extracted executable is used."""

    validated_identity = _validate_candidate_identity(identity)
    candidate = validated_identity["candidate"]
    manifest_binding = candidate["preview_manifest"]
    manifest_contents = manifest_binding["contents"]

    archive_file, archive_bytes = _read_artifact_bytes(archive_path, "archive")
    manifest_file, manifest_bytes = _read_artifact_bytes(manifest_path, "manifest")
    executable_file, executable_bytes = _read_artifact_bytes(executable_path, "executable")

    _verify_recorded_file(archive_file, archive_bytes, manifest_contents["archive"], "archive")
    _verify_recorded_file(manifest_file, manifest_bytes, manifest_binding["file"], "manifest")
    _verify_recorded_file(
        executable_file, executable_bytes, manifest_contents["executable"], "executable"
    )

    observed_manifest = _load_manifest(manifest_bytes)
    _validate_preview_manifest(observed_manifest, candidate["source"]["commit"])
    if observed_manifest != manifest_contents:
        _refuse("manifest contents do not match the candidate")

    payload_hash = hashlib.sha256()
    payload_hash.update(archive_bytes)
    payload_hash.update(manifest_bytes)
    if payload_hash.hexdigest() != candidate["build"]["artifact"]["sha256"]:
        _refuse("retained payload digest does not match the candidate")

    _verify_archive_member(archive_bytes, manifest_contents["executable"]["name"], executable_bytes)

    return _freeze(
        {
            "source_ref": candidate["source"]["ref"],
            "source_commit": candidate["source"]["commit"],
            "archive": _thaw(manifest_contents["archive"]),
            "manifest": _thaw(manifest_binding["file"]),
            "executable": _thaw(manifest_contents["executable"]),
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
    local_main = _git_text(repository_root, "rev-parse", "--verify", "refs/heads/main^{commit}")
    origin_main = _git_text(
        repository_root,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main^{commit}",
    )
    if (
        _COMMIT_PATTERN.fullmatch(local_main) is None
        or _COMMIT_PATTERN.fullmatch(origin_main) is None
        or local_main != source_commit
        or origin_main != source_commit
    ):
        _refuse("candidate is not the exact canonical main target")


def _read_tracked_authority(repository_root: Path, source_commit: str, relative_path: str) -> bytes:
    object_name = f"{source_commit}:{relative_path}"
    object_type = _git_text(repository_root, "cat-file", "-t", object_name)
    if object_type != "blob":
        _refuse("policy authority is not a tracked file")
    return _git_bytes(repository_root, "cat-file", "--filters", object_name)


def _fixed_gate_descriptors() -> list[dict[str, Any]]:
    requirements = {
        "retained-downloaded-consumer-proof": "RETAINED_ARTIFACT_PROOF",
        "s2-s3-seven-lens-evidence": "S2_S3_SEVEN_LENS_AGGREGATE",
        "independent-review": "INDEPENDENT_PR_REVIEW",
        "candidate-exact-head-sonar": "CANDIDATE_EXACT_HEAD_SONAR",
        "post-merge-exact-head-sonar": "POST_MERGE_MAIN_TARGET_SONAR",
        "remote-downloaded-consumer-proof": "REMOTE_CONSUMER_PROOF",
    }
    rows = (
        ("pre-decision", "retained-downloaded-consumer-proof", "artifact-consumer-proof"),
        ("pre-decision", "s2-s3-seven-lens-evidence", "s2-s3-seven-lens-aggregate"),
        ("pre-decision", "independent-review", "independent-pr-review"),
        ("pre-decision", "candidate-exact-head-sonar", "sonarqube-exact-head"),
        ("pre-publication", "post-merge-exact-head-sonar", "sonarqube-exact-head"),
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


__all__ = [
    "assemble_candidate_identity",
    "resolve_release_gate_catalog",
    "verify_retained_artifact",
]
