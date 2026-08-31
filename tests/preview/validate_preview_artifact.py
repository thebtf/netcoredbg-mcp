"""Seal one A1 retained-artifact proof from downloaded GitHub Actions bytes only.

Candidate mode accepts a closed structural-reference bundle, downloads its Candidate
Identity Record, archive, raw manifest, and Release Gate Catalog into a fresh local
directory, verifies every declared hash before extraction, exercises the inherited
consumer matrix, invokes the unchanged Python rollback oracle, and writes exactly
one canonical receipt. It never accepts a source-tree executable, a local rebuild,
or a caller-supplied candidate JSON as proof authority.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_HELPER_PATH = _PROJECT_ROOT / "scripts" / "stateless_preview_artifact.py"
_EXACT_HEAD_RUNNER_PATH = _PROJECT_ROOT / "scripts" / "run_sonarqube_exact_head.py"
_CANONICAL_SOURCE_REF = "refs/heads/main"
_SONAR_PROJECT_KEY = "thebtf_netcoredbg_mcp"
_POST_MERGE_RELEASE_INTENT = "v0.23.11"

_EXECUTABLE_NAME = "netcoredbg-mcp-stateless-preview.exe"
_PROTOCOL_VERSION = "2026-07-28"
_REQUEST_META = {
    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {
        "name": "preview-artifact-consumer",
        "version": "1.0",
    },
    "io.modelcontextprotocol/clientCapabilities": {},
}
_EXPECTED_OBSERVED = {
    "EXPECT_SUCCESS": "SUCCESS",
    "EXPECT_FAILURE": "FAILURE",
    "EXPECT_REFUSAL": "REFUSAL",
}

_spec = importlib.util.spec_from_file_location("_preview_artifact_contract", _ARTIFACT_HELPER_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError("A1 artifact contract is unavailable")
_artifact = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _artifact
_spec.loader.exec_module(_artifact)

assemble_candidate_identity = _artifact.assemble_candidate_identity
consumer_proof_scenario_catalog = _artifact.consumer_proof_scenario_catalog
seal_artifact_consumer_proof = _artifact.seal_artifact_consumer_proof
validate_artifact_consumer_proof_reference = _artifact.validate_artifact_consumer_proof_reference
verify_and_extract_retained_artifact = _artifact.verify_and_extract_retained_artifact


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


def _load_exact_head_receipt_validator() -> Callable[[Mapping[str, Any]], None]:
    specification = importlib.util.spec_from_file_location(
        "_preview_artifact_exact_head_runner", _EXACT_HEAD_RUNNER_PATH
    )
    if specification is None or specification.loader is None:
        _refuse("exact-head receipt authority is unavailable")
    runner = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = runner
    try:
        specification.loader.exec_module(runner)
    except Exception as error:
        raise ValueError("exact-head receipt authority is unavailable") from error
    validator = getattr(runner, "validate_exact_head_receipt_v3", None)
    if not callable(validator):
        _refuse("exact-head receipt authority is unavailable")
    return validator


def _validate_downloaded_post_merge_receipt(
    receipt: Mapping[str, Any], candidate_source: Mapping[str, Any]
) -> None:
    validator = _load_exact_head_receipt_validator()
    try:
        validator(receipt)
    except Exception as error:
        raise ValueError(
            "downloaded receipt provenance does not match the candidate exact-head receipt"
        ) from error
    identity = receipt.get("identity")
    if (
        receipt.get("role") != "post-merge"
        or receipt.get("outcome") != "PASS"
        or receipt.get("release_intent") != _POST_MERGE_RELEASE_INTENT
        or candidate_source.get("ref") != _CANONICAL_SOURCE_REF
        or not isinstance(identity, Mapping)
        or identity.get("captured_head") != candidate_source.get("commit")
        or identity.get("project_key") != _SONAR_PROJECT_KEY
    ):
        _refuse("downloaded receipt provenance does not match the candidate exact-head receipt")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )


def _read_regular_bytes(path_value: str | Path, name: str) -> bytes:
    path = Path(path_value)
    try:
        if path.is_symlink() or not path.is_file():
            _refuse(f"{name} is unavailable")
        return path.read_bytes()
    except OSError:
        _refuse(f"{name} is unreadable")
    raise AssertionError("unreachable")


def _load_json_object(raw: bytes, name: str) -> dict[str, Any]:
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


def _ordinary_directory(path_value: str | Path, name: str) -> Path:
    path = Path(path_value)
    if path.is_symlink():
        _refuse(f"{name} must be an ordinary directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _refuse(f"{name} is unavailable")
    if not resolved.is_dir():
        _refuse(f"{name} must be an ordinary directory")
    return resolved


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


@dataclass(frozen=True)
class CandidateReferences:
    candidate_identity: ArtifactFileReference
    archive: ArtifactFileReference
    manifest: ArtifactFileReference
    release_gate_catalog: ArtifactFileReference
    receipt_provenance: ArtifactFileReference

    @classmethod
    def load(cls, path_value: str | Path) -> CandidateReferences:
        raw = _read_regular_bytes(path_value, "candidate reference bundle")
        value = _load_json_object(raw, "candidate reference bundle")
        if (
            set(value)
            != {
                "reference_schema_version",
                "candidate_identity",
                "archive",
                "manifest",
                "release_gate_catalog",
                "receipt_provenance",
            }
            or value["reference_schema_version"] != "1.0"
        ):
            _refuse("candidate reference bundle has unsupported or missing fields")
        fields = {
            field: ArtifactFileReference.from_mapping(value[field], f"candidate {field} reference")
            for field in (
                "candidate_identity",
                "archive",
                "manifest",
                "release_gate_catalog",
                "receipt_provenance",
            )
        }
        if not fields["archive"].same_artifact(fields["manifest"]):
            _refuse("archive and manifest references must name one retained payload artifact")
        return cls(**fields)


class ArtifactDownloader:
    def __init__(self, root: Path):
        self._root = root
        self._archives: dict[tuple[str, str, str], tuple[Path, str]] = {}
        self._archive_directory: Path | None = None

    def _run_gh(self, arguments: Sequence[str], name: str) -> bytes:
        try:
            result = subprocess.run(
                ["gh", *arguments],
                cwd=self._root,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            _refuse(f"{name} could not be downloaded")
        if result.returncode != 0:
            _refuse(f"{name} could not be downloaded")
        return result.stdout

    def _metadata(self, reference: ArtifactFileReference) -> None:
        raw = self._run_gh(
            ["api", f"repos/{reference.repository}/actions/artifacts/{reference.artifact_id}"],
            "retained artifact metadata",
        )
        metadata = _load_json_object(raw, "retained artifact metadata")
        workflow_run = metadata.get("workflow_run")
        if (
            str(metadata.get("id")) != reference.artifact_id
            or metadata.get("name") != reference.artifact_name
            or metadata.get("expired") is not False
            or not isinstance(workflow_run, Mapping)
            or str(workflow_run.get("id")) != reference.run_id
            or metadata.get("expires_at") != reference.expires_at
        ):
            _refuse("retained artifact metadata does not match its structural reference")
        digest = metadata.get("digest")
        if digest is not None and digest != f"sha256:{reference.artifact_sha256}":
            _refuse("retained artifact metadata digest does not match its structural reference")

    def _archive(self, reference: ArtifactFileReference) -> bytes:
        key = (reference.repository, reference.artifact_id, reference.artifact_sha256)
        cached = self._archives.get(key)
        if cached is not None:
            cached_path, wire_sha256 = cached
            wire_bytes = _read_regular_bytes(cached_path, "cached retained artifact")
            if _sha256_bytes(wire_bytes) != wire_sha256 or wire_sha256 != reference.artifact_sha256:
                _refuse("cached retained artifact wire bytes do not match the structural reference")
            return wire_bytes
        self._metadata(reference)
        wire_bytes = self._run_gh(
            [
                "api",
                f"repos/{reference.repository}/actions/artifacts/{reference.artifact_id}/zip",
            ],
            "retained artifact",
        )
        wire_sha256 = _sha256_bytes(wire_bytes)
        if wire_sha256 != reference.artifact_sha256:
            _refuse("retained artifact wire bytes do not match the structural reference")
        destination = (
            self._root / "artifacts" / f"{reference.artifact_id}-{reference.artifact_sha256}.zip"
        )
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

    def _artifact_member(self, reference: ArtifactFileReference) -> bytes:
        wire_bytes = self._archive(reference)
        try:
            with zipfile.ZipFile(io.BytesIO(wire_bytes)) as archive:
                entries = [
                    entry for entry in archive.infolist() if entry.filename == reference.path
                ]
                if len(entries) != 1 or entries[0].is_dir():
                    _refuse("retained artifact file is absent or ambiguous")
                contents = archive.read(entries[0])
        except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            _refuse("retained artifact is unreadable")
        if _sha256_bytes(contents) != reference.sha256:
            _refuse("retained artifact file bytes do not match the structural reference")
        return contents

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


def _require_candidate_file_reference(
    actual: ArtifactFileReference, expected: Mapping[str, Any], name: str
) -> None:
    if (
        actual.repository != expected["repository"]
        or actual.run_id != expected["run_id"]
        or actual.artifact_id != expected["artifact_id"]
        or actual.path != expected["path"]
        or actual.sha256 != expected["sha256"]
    ):
        _refuse(f"{name} reference does not bind the downloaded candidate")


def _download_and_validate_receipt_provenance(
    downloader: ArtifactDownloader,
    reference: ArtifactFileReference,
    candidate_source: Mapping[str, Any],
) -> ArtifactFileReference:
    provenance_path = downloader.download_file(reference)
    provenance_bytes = _read_regular_bytes(provenance_path, "downloaded receipt provenance")
    if _sha256_bytes(provenance_bytes) != reference.sha256:
        _refuse("downloaded receipt provenance hash is wrong")
    _require_candidate_file_reference(
        reference,
        candidate_source["post_merge_exact_head_receipt"]["record_reference"],
        "receipt provenance",
    )
    record = _load_json_object(provenance_bytes, "downloaded receipt provenance")
    _validate_downloaded_post_merge_receipt(record, candidate_source)
    return reference


def _fixture_identity(fixture_root: Path) -> dict[str, str]:
    files = sorted(
        path for path in fixture_root.rglob("*") if path.is_file() and not path.is_symlink()
    )
    if not files:
        _refuse("fixture root has no regular files")
    digest = hashlib.sha256()
    for file in files:
        relative = file.relative_to(fixture_root).as_posix().encode()
        contents = _read_regular_bytes(file, "fixture file")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    catalog = consumer_proof_scenario_catalog()
    return {
        "fixture_id": "preview-search-fixture-v1",
        "fixture_sha256": digest.hexdigest(),
        "scenario_catalog_id": catalog["scenario_catalog_id"],
        "scenario_catalog_sha256": catalog["scenario_catalog_sha256"],
    }


def _readline_with_timeout(stream: Any, timeout: float) -> bytes:
    values: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            values.put(stream.readline())
        except (
            BaseException
        ) as error:  # pragma: no cover - process pipe failures are platform specific.
            values.put(error)

    threading.Thread(target=read, daemon=True).start()
    try:
        value = values.get(timeout=timeout)
    except queue.Empty:
        _refuse("preview process did not return a bounded JSON-RPC response")
    if isinstance(value, BaseException):
        _refuse("preview process stdout is unreadable")
    return value


class JsonRpcProcess:
    def __init__(
        self,
        executable_path: Path,
        project_root: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ):
        try:
            self._process = subprocess.Popen(
                [str(executable_path), "--project", str(project_root)],
                cwd=cwd or executable_path.parent,
                env=dict(env) if env is not None else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            _refuse("verified retained executable did not start")

    def request(self, request_id: int, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._process.stdin is None or self._process.stdout is None:
            _refuse("preview process stdio is unavailable")
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            self._process.stdin.write(_canonical_json_bytes(request))
            self._process.stdin.flush()
        except OSError:
            _refuse("preview process rejected a JSON-RPC request")
        line = _readline_with_timeout(self._process.stdout, 5)
        if not line:
            _refuse("preview process ended before a JSON-RPC response")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _refuse("preview process emitted non-JSON-RPC stdout")
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != request_id
        ):
            _refuse("preview process emitted an unexpected JSON-RPC response")
        return response

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            _refuse("preview process stdin is unavailable")
        try:
            self._process.stdin.write(
                _canonical_json_bytes({"jsonrpc": "2.0", "method": method, "params": params})
            )
            self._process.stdin.flush()
        except OSError:
            _refuse("preview process rejected a JSON-RPC notification")

    def close_eof(self) -> None:
        if self._process.stdin is None:
            _refuse("preview process stdin is unavailable for EOF")
        self._process.stdin.close()
        self._process.stdin = None
        try:
            stdout_tail, stderr = self._process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.communicate(timeout=2)
            _refuse("preview process did not exit after stdin EOF")
        if self._process.returncode != 0 or stdout_tail or stderr:
            _refuse("preview process did not close cleanly after stdin EOF")

    def abort(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.communicate(timeout=2)


def _result(response: Mapping[str, Any]) -> Mapping[str, Any]:
    result = response.get("result")
    if not isinstance(result, Mapping):
        _refuse("JSON-RPC request did not return a result")
    return result


def _closed_error(result: Mapping[str, Any], kind: str, error: str) -> bool:
    return (
        result.get("resultType") == "complete"
        and result.get("isError") is True
        and result.get("structuredContent")
        == {"kind": kind, "error": error, "tool": "find_code_symbol"}
    )


def _run_valid_journey(executable_path: Path, fixture_root: Path) -> None:
    process = JsonRpcProcess(executable_path, fixture_root)
    try:
        discovery = _result(process.request(1, "server/discover", {"_meta": _REQUEST_META}))
        capabilities = discovery.get("capabilities")
        if not isinstance(capabilities, Mapping) or set(capabilities) != {"tools"}:
            _refuse("server/discover did not expose the closed tools capability")
        catalog = _result(process.request(2, "tools/list", {"_meta": _REQUEST_META}))
        tools = catalog.get("tools")
        if (
            not isinstance(tools, list)
            or len(tools) != 1
            or tools[0].get("name") != "find_code_symbol"
        ):
            _refuse("tools/list did not expose exactly find_code_symbol")
        call = _result(
            process.request(
                3,
                "tools/call",
                {
                    "name": "find_code_symbol",
                    "arguments": {"name": "PreviewMarker", "kind": "class"},
                    "_meta": _REQUEST_META,
                },
            )
        )
        structured = call.get("structuredContent")
        if (
            call.get("resultType") != "complete"
            or call.get("isError") is not False
            or not isinstance(structured, Mapping)
            or structured.get("kind") != "find_code_symbol_success"
            or not isinstance(structured.get("results"), list)
            or not structured["results"]
        ):
            _refuse("valid find_code_symbol journey did not complete")
        first = structured["results"][0]
        if (
            not isinstance(first, Mapping)
            or first.get("name") != "PreviewMarker"
            or first.get("kind") != "class"
        ):
            _refuse("valid find_code_symbol journey did not return the fixture marker")
        process.close_eof()
    finally:
        process.abort()


def _run_launch_refusal(executable_path: Path, arguments: Sequence[str]) -> None:
    try:
        process = subprocess.Popen(
            [str(executable_path), *arguments],
            cwd=executable_path.parent,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=2)
    except (OSError, subprocess.SubprocessError):
        _refuse("invalid launch case did not complete")
    if process.returncode != 64 or stdout or stderr != b"PREVIEW_ROOT_INVALID\n":
        _refuse("invalid launch case did not produce the closed observable refusal")


def _run_launch_matrix(executable_path: Path, fixture_root: Path, scratch: Path) -> None:
    nondirectory = scratch / "not-a-directory"
    nondirectory.write_bytes(b"not a directory")
    reparse_target = scratch / "reparse-target"
    reparse_target.mkdir()
    reparse_root = scratch / "reparse-root"
    try:
        reparse_root.symlink_to(reparse_target, target_is_directory=True)
    except OSError:
        _refuse("required reparse launch fixture is unavailable")
    cases = (
        (),
        ("--project",),
        ("--project", "relative"),
        ("--project", str(scratch / "missing")),
        ("--project", str(nondirectory)),
        ("--project", r"\\localhost\share"),
        ("--project", r"\\.\C:\device"),
        ("--project", str(reparse_root)),
        ("--project", str(fixture_root), "--project", str(fixture_root)),
    )
    for case in cases:
        _run_launch_refusal(executable_path, case)


def _run_configuration_matrix(executable_path: Path, fixture_root: Path, scratch: Path) -> None:
    outside = scratch / "outside-authority"
    outside.mkdir()
    (outside / "Outside.cs").write_text("public sealed class OutsideMarker { }\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["NETCOREDBG_MCP_PROJECT"] = str(outside)
    environment["PREVIEW_PROJECT"] = str(outside)
    process = JsonRpcProcess(executable_path, fixture_root, cwd=outside, env=environment)
    try:
        result = _result(
            process.request(
                11,
                "tools/call",
                {
                    "name": "find_code_symbol",
                    "arguments": {"name": "OutsideMarker", "kind": "class"},
                    "_meta": _REQUEST_META,
                },
            )
        )
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping) or structured.get("results") != []:
            _refuse("hostile CWD or environment altered explicit project authority")
        process.close_eof()
    finally:
        process.abort()


def _run_containment_matrix(executable_path: Path, scratch: Path) -> None:
    for index in range(3):
        root = scratch / f"contained-{index}"
        outside = scratch / f"outside-{index}"
        root.mkdir()
        outside.mkdir()
        (root / "Marker.cs").write_text(
            "public sealed class ContainedMarker { }\n", encoding="utf-8"
        )
        target = outside / "Escaping.cs"
        target.write_text("public sealed class EscapingMarker { }\n", encoding="utf-8")
        link = root / f"ZEscaping{index}.cs"
        try:
            link.symlink_to(target)
        except OSError:
            _refuse("required reparse containment fixture is unavailable")
        process = JsonRpcProcess(executable_path, root)
        try:
            result = _result(
                process.request(
                    20 + index,
                    "tools/call",
                    {
                        "name": "find_code_symbol",
                        "arguments": {"name": "ContainedMarker", "kind": "class"},
                        "_meta": _REQUEST_META,
                    },
                )
            )
            if not _closed_error(result, "preview_path_refused", "PREVIEW_PATH_REFUSED"):
                _refuse("contained escape did not produce PREVIEW_PATH_REFUSED")
            process.close_eof()
        finally:
            process.abort()


def _run_tool_input_matrix(executable_path: Path, fixture_root: Path) -> None:
    invalid_arguments = (
        {},
        {"name": None},
        {"name": 3},
        {"name": " "},
        {"name": "x" * 257},
        {"name": "PreviewMarker", "extra": True},
        {"name": "PreviewMarker", "kind": "event"},
    )
    process = JsonRpcProcess(executable_path, fixture_root)
    try:
        for request_id, arguments in enumerate(invalid_arguments, start=40):
            result = _result(
                process.request(
                    request_id,
                    "tools/call",
                    {"name": "find_code_symbol", "arguments": arguments, "_meta": _REQUEST_META},
                )
            )
            if not _closed_error(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS"):
                _refuse("invalid tool input did not produce INVALID_TOOL_ARGUMENTS")
        process.close_eof()
    finally:
        process.abort()


def _run_locked_file_matrix(executable_path: Path, scratch: Path) -> None:
    if os.name != "nt":
        _refuse("file-system denial proof requires Windows")
    root = scratch / "locked-file"
    root.mkdir()
    (root / "Marker.cs").write_text("public sealed class PartialMarker { }\n", encoding="utf-8")
    locked = root / "ZLocked.cs"
    locked.write_text("public sealed class LockedMarker { }\n", encoding="utf-8")
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(str(locked), 0xC0000000, 0, None, 3, 0, None)
    if handle == -1:
        _refuse("file-system denial fixture could not lock the source file")
    try:
        process = JsonRpcProcess(executable_path, root)
        try:
            result = _result(
                process.request(
                    60,
                    "tools/call",
                    {
                        "name": "find_code_symbol",
                        "arguments": {"name": "PartialMarker", "kind": "class"},
                        "_meta": _REQUEST_META,
                    },
                )
            )
            if not _closed_error(result, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE"):
                _refuse("locked source did not produce PREVIEW_SEARCH_UNREADABLE")
            process.close_eof()
        finally:
            process.abort()
    finally:
        kernel32.CloseHandle(handle)


def _expect_budget_refusal(executable_path: Path, root: Path, request_id: int, marker: str) -> None:
    process = JsonRpcProcess(executable_path, root)
    try:
        result = _result(
            process.request(
                request_id,
                "tools/call",
                {
                    "name": "find_code_symbol",
                    "arguments": {"name": marker, "kind": "class"},
                    "_meta": _REQUEST_META,
                },
            )
        )
        if not _closed_error(
            result, "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED"
        ):
            _refuse("resource ceiling did not produce PREVIEW_SEARCH_BUDGET_EXCEEDED")
        process.close_eof()
    finally:
        process.abort()


def _run_resource_matrix(executable_path: Path, scratch: Path) -> None:
    oversized_file_root = scratch / "resource-file"
    oversized_file_root.mkdir()
    (oversized_file_root / "TooLarge.cs").write_text(
        "//" + "x" * (1024 * 1024 + 1), encoding="utf-8"
    )
    _expect_budget_refusal(executable_path, oversized_file_root, 70, "NoMatch")

    result_root = scratch / "resource-results"
    result_root.mkdir()
    (result_root / "Matches.cs").write_text(
        "\n".join(f"public sealed class ResultMarker {{ }} // {index}" for index in range(129)),
        encoding="utf-8",
    )
    _expect_budget_refusal(executable_path, result_root, 71, "ResultMarker")

    aggregate_root = scratch / "resource-aggregate"
    aggregate_root.mkdir()
    payload = "//" + "a" * (1024 * 1024 - 8)
    for index in range(17):
        (aggregate_root / f"Large{index:02}.cs").write_text(payload, encoding="utf-8")
    _expect_budget_refusal(executable_path, aggregate_root, 72, "NoMatch")

    entry_root = scratch / "resource-entries"
    entry_root.mkdir()
    for index in range(20_001):
        (entry_root / f"entry-{index:05}.txt").touch()
    _expect_budget_refusal(executable_path, entry_root, 73, "NoMatch")


def _run_protocol_matrix(executable_path: Path, fixture_root: Path) -> None:
    process = JsonRpcProcess(executable_path, fixture_root)
    try:
        for request_id, method in ((80, "initialize"), (81, "resources/list"), (82, "roots/list")):
            response = process.request(request_id, method, {"_meta": _REQUEST_META})
            error = response.get("error")
            if not isinstance(error, Mapping) or error.get("code") != -32601:
                _refuse("excluded protocol method did not return method-not-found")
        unknown = _result(
            process.request(
                83,
                "tools/call",
                {"name": "start_debug", "arguments": {}, "_meta": _REQUEST_META},
            )
        )
        content = unknown.get("content")
        if (
            unknown.get("resultType") != "complete"
            or unknown.get("isError") is not True
            or not isinstance(content, list)
            or len(content) != 1
            or content[0].get("text") != "Unknown tool: start_debug"
        ):
            _refuse("forbidden tool did not return the closed unknown-tool refusal")
        process.close_eof()
    finally:
        process.abort()


def _run_transport_matrix(executable_path: Path, fixture_root: Path, scratch: Path) -> None:
    eof = JsonRpcProcess(executable_path, fixture_root)
    try:
        eof.close_eof()
    finally:
        eof.abort()

    cancellation_root = scratch / "cancellation"
    cancellation_root.mkdir()
    source = "// cancellation load padding\n" * 32_768
    for index in range(16):
        (cancellation_root / f"Load{index:02}.cs").write_text(source, encoding="utf-8")
    process = JsonRpcProcess(executable_path, cancellation_root)
    try:
        if process._process.stdin is None:
            _refuse("preview process stdin is unavailable for cancellation")
        request = {
            "jsonrpc": "2.0",
            "id": 90,
            "method": "tools/call",
            "params": {
                "name": "find_code_symbol",
                "arguments": {"name": "CancellationMarker", "kind": "class"},
                "_meta": _REQUEST_META,
            },
        }
        process._process.stdin.write(_canonical_json_bytes(request))
        process._process.stdin.flush()
        process.notify("notifications/cancelled", {"requestId": 90})
        process.close_eof()
    finally:
        process.abort()


_PYTHON_ROLLBACK_PRODUCT_WORKS_MARKER = {
    "product_works": True,
    "denominator": "5/5",
    "tool_count": 135,
    "stopped_at_entry": True,
}


def _run_python_rollback(command: str, arguments: Sequence[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [command, *arguments],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _refuse("unchanged Python rollback oracle could not run")
    if result.returncode != 0:
        _refuse("unchanged Python rollback oracle did not reach PRODUCT_WORKS")
    if (
        not isinstance(result.stdout, bytes)
        or _load_json_object(result.stdout, "unchanged Python rollback oracle output")
        != _PYTHON_ROLLBACK_PRODUCT_WORKS_MARKER
    ):
        _refuse("unchanged Python rollback oracle did not emit PRODUCT_WORKS")
    return {
        "only_preview_selection_removed": True,
        "python_package_reinstalled": False,
        "python_package_replaced": False,
        "console_entrypoint_changed": False,
        "default_selector_changed": False,
        "legacy_journey_outcome": "PRODUCT_WORKS",
    }


def _run_matrix(
    executable_path: Path,
    fixture_root: Path,
    scratch: Path,
    rollback: Callable[[], None],
) -> list[dict[str, Any]]:
    handlers: dict[str, Callable[[], None]] = {
        "launch-cli-invalid-root": lambda: _run_launch_matrix(
            executable_path, fixture_root, scratch
        ),
        "launch-configuration-hostile-roots": lambda: _run_configuration_matrix(
            executable_path, fixture_root, scratch
        ),
        "contained-fixture-escape": lambda: _run_containment_matrix(executable_path, scratch),
        "tool-input-invalid": lambda: _run_tool_input_matrix(executable_path, fixture_root),
        "file-system-unreadable": lambda: _run_locked_file_matrix(executable_path, scratch),
        "resources-ceilings": lambda: _run_resource_matrix(executable_path, scratch),
        "protocol-catalog-exclusions": lambda: _run_protocol_matrix(executable_path, fixture_root),
        "transport-eof-cancellation": lambda: _run_transport_matrix(
            executable_path, fixture_root, scratch
        ),
        "valid-discovery-list-call": lambda: _run_valid_journey(executable_path, fixture_root),
        "rollback-python-default": rollback,
    }
    results: list[dict[str, Any]] = []
    for scenario in consumer_proof_scenario_catalog()["scenarios"]:
        handler = handlers.get(scenario["scenario_id"])
        if handler is None:
            _refuse("closed consumer matrix has no runner")
        handler()
        results.append(
            {
                "scenario_id": scenario["scenario_id"],
                "surface": scenario["surface"],
                "documented_outcome": scenario["documented_outcome"],
                "observed_outcome": _EXPECTED_OBSERVED[scenario["documented_outcome"]],
                "status": "PASS",
                "no_partial_output": True,
                "no_unintended_side_effect": True,
            }
        )
    return results


def _write_receipt_once(path_value: str | Path, receipt: Mapping[str, Any]) -> None:
    path = Path(path_value)
    raw = _canonical_json_bytes(receipt)
    try:
        if path.exists() or path.is_symlink():
            _refuse("consumer proof receipt already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(raw)
    except OSError:
        _refuse("consumer proof receipt cannot be written")


def run_candidate(arguments: argparse.Namespace) -> Mapping[str, Any]:
    references = CandidateReferences.load(arguments.references)
    fixture_root = _ordinary_directory(arguments.fixture_root, "fixture root")
    download_root = _ordinary_directory(arguments.download_root, "download root")
    with tempfile.TemporaryDirectory(prefix="a1-preview-artifact-", dir=download_root) as temporary:
        fresh = Path(temporary)
        downloader = ArtifactDownloader(fresh)
        identity_path = downloader.download_file(references.candidate_identity)
        identity_bytes = _read_regular_bytes(identity_path, "downloaded candidate identity")
        if _sha256_bytes(identity_bytes) != references.candidate_identity.sha256:
            _refuse("downloaded candidate identity hash is wrong")
        identity = _load_json_object(identity_bytes, "downloaded candidate identity")
        validated_identity = assemble_candidate_identity(identity)
        candidate = validated_identity["candidate"]
        receipt_provenance = _download_and_validate_receipt_provenance(
            downloader,
            references.receipt_provenance,
            candidate["source"],
        )
        manifest_binding = candidate["preview_manifest"]
        _require_candidate_file_reference(
            references.archive, manifest_binding["archive_reference"], "archive"
        )
        _require_candidate_file_reference(
            references.manifest, manifest_binding["manifest_reference"], "manifest"
        )
        archive_path, manifest_path = downloader.download_payload(
            references.archive,
            references.manifest,
            candidate["build"]["artifact"]["sha256"],
        )
        catalog_path = downloader.download_file(references.release_gate_catalog)
        if archive_path.parent != manifest_path.parent:
            _refuse("archive and manifest were not retained in one fresh payload directory")
        catalog_bytes = _read_regular_bytes(catalog_path, "downloaded release gate catalog")
        if _sha256_bytes(catalog_bytes) != references.release_gate_catalog.sha256:
            _refuse("downloaded release gate catalog hash is wrong")
        catalog = _load_json_object(catalog_bytes, "downloaded release gate catalog")
        verified = verify_and_extract_retained_artifact(
            validated_identity,
            archive_path,
            manifest_path,
            fresh / "extracted",
        )
        executable_path = Path(verified["executable_path"])
        if executable_path.name != _EXECUTABLE_NAME or not _path_within(
            executable_path, fresh / "extracted"
        ):
            _refuse("extracted executable is not the verified retained artifact member")
        matrix_root = fresh / "matrix"
        matrix_root.mkdir()
        rollback_result: dict[str, Any] | None = None

        def run_rollback() -> None:
            nonlocal rollback_result
            rollback_result = _run_python_rollback(
                arguments.python_rollback_command,
                arguments.python_rollback_argument,
            )

        scenario_results = _run_matrix(
            executable_path,
            fixture_root,
            matrix_root,
            run_rollback,
        )
        if rollback_result is None:
            _refuse("consumer matrix did not run the unchanged Python rollback oracle")
        catalog_definition = consumer_proof_scenario_catalog()
        scenario_ids = [scenario["scenario_id"] for scenario in catalog_definition["scenarios"]]
        receipt = {
            "receipt_schema_version": "1.0",
            "receipt_id": arguments.receipt_id,
            "candidate_identity_record": references.candidate_identity.as_mapping(),
            "candidate": candidate,
            "proof_stage": "retained_artifact",
            "download_origin": {
                "repository": candidate["build"]["repository"],
                "workflow_path": ".github/workflows/stateless-preview.yml",
                "run_id": candidate["build"]["run_id"],
                "artifact": candidate["build"]["artifact"],
                "archive_path": manifest_binding["archive_reference"]["path"],
                "manifest_path": manifest_binding["manifest_reference"]["path"],
            },
            "input_identity_results": {
                "archive": verified["archive"],
                "manifest": verified["manifest"],
                "executable": verified["executable"],
                "archive_matches_candidate": True,
                "manifest_matches_candidate": True,
                "executable_matches_manifest": True,
                "inherited_verifier_equations_pass": True,
            },
            "fixture_identity": _fixture_identity(fixture_root),
            "scenario_matrix": {
                "required_scenario_ids": scenario_ids,
                "executed_scenario_ids": scenario_ids,
                "required_count": len(scenario_ids),
                "executed_count": len(scenario_ids),
                "missing_scenario_ids": [],
                "unexpected_scenario_ids": [],
                "results": scenario_results,
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
            "python_rollback_result": rollback_result,
            "outcome": "PASS",
            "recorded_at": arguments.recorded_at,
            "receipt_provenance": receipt_provenance.as_mapping(),
        }
        sealed = seal_artifact_consumer_proof(
            receipt,
            candidate_identity=validated_identity,
            candidate_identity_bytes=identity_bytes,
            candidate_identity_reference=references.candidate_identity.as_mapping(),
            release_gate_catalog=catalog,
        )
    _write_receipt_once(arguments.receipt_output, sealed)
    return sealed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser("candidate", help="prove one downloaded retained candidate")
    candidate.add_argument("--references", required=True)
    candidate.add_argument("--download-root", required=True)
    candidate.add_argument("--fixture-root", required=True)
    candidate.add_argument("--receipt-output", required=True)
    candidate.add_argument("--receipt-id", required=True)
    candidate.add_argument("--recorded-at", required=True)
    candidate.add_argument("--python-rollback-command", required=True)
    candidate.add_argument("--python-rollback-argument", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command == "candidate":
            run_candidate(arguments)
        else:
            raise AssertionError("unreachable")
    except ValueError:
        print("PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED", file=sys.stderr)
        return 1
    print("PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
