"""Seal candidate-bound A1 or A2 retained/public artifact consumer proof.

Both modes resolve Candidate Identity and Release Gate Catalog metadata from a
closed Actions reference bundle. Candidate mode downloads the retained payload;
remote mode downloads payload bytes only from the exact public GitHub prerelease.
Both verify the archive, raw manifest, and extracted executable, run the complete
consumer matrix and unchanged Python rollback, then write one canonical receipt.
Neither accepts a source-tree executable, rebuild, or caller-supplied candidate.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import stateless_preview_artifact as _artifact
from scripts.stateless_preview_public_release import download_public_release

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
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

assemble_candidate_identity = _artifact.assemble_candidate_identity
consumer_proof_scenario_catalog = _artifact.consumer_proof_scenario_catalog
seal_artifact_consumer_proof = _artifact.seal_artifact_consumer_proof
verify_and_extract_retained_artifact = _artifact.verify_and_extract_retained_artifact


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


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


@dataclass(frozen=True)
class CandidateReferences:
    candidate_identity: _artifact.ArtifactFileReference
    archive: _artifact.ArtifactFileReference
    manifest: _artifact.ArtifactFileReference
    release_gate_catalog: _artifact.ArtifactFileReference

    @classmethod
    def load(cls, path_value: str | Path) -> CandidateReferences:
        raw = _read_regular_bytes(path_value, "candidate reference bundle")
        value = _artifact._load_retained_json_object(raw, "candidate reference bundle")
        if (
            set(value)
            != {
                "reference_schema_version",
                "candidate_identity",
                "archive",
                "manifest",
                "release_gate_catalog",
            }
            or value["reference_schema_version"] != "1.0"
        ):
            _refuse("candidate reference bundle has unsupported or missing fields")
        fields = {
            field: _artifact.ArtifactFileReference.from_mapping(
                value[field], f"candidate {field} reference"
            )
            for field in (
                "candidate_identity",
                "archive",
                "manifest",
                "release_gate_catalog",
            )
        }
        if not fields["archive"].same_artifact(fields["manifest"]):
            _refuse("archive and manifest references must name one retained payload artifact")
        return cls(**fields)


def _require_candidate_file_reference(
    actual: _artifact.ArtifactFileReference, expected: Mapping[str, Any], name: str
) -> None:
    if (
        actual.repository != expected["repository"]
        or actual.run_id != expected["run_id"]
        or actual.artifact_id != expected["artifact_id"]
        or actual.path != expected["path"]
        or actual.sha256 != expected["sha256"]
    ):
        _refuse(f"{name} reference does not bind the downloaded candidate")


def _fixture_identity(fixture_root: Path, contract_id: str) -> dict[str, str]:
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
    catalog = consumer_proof_scenario_catalog(contract_id)
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

    def request(
        self, request_id: int | str, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
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
        if len(line.rstrip(b"\r\n")) > 256 * 1024:
            _refuse("preview process emitted an oversized complete response frame")
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


def _closed_error(
    result: Mapping[str, Any], kind: str, error: str, tool: str = "find_code_symbol"
) -> bool:
    return (
        result.get("resultType") == "complete"
        and result.get("isError") is True
        and result.get("structuredContent") == {"kind": kind, "error": error, "tool": tool}
    )


def _load_a2_tool_schemas() -> Mapping[str, Any]:
    frozen = _artifact._load_json_object(
        _read_regular_bytes(
            Path(__file__).with_name("a2-tool-schemas.json"), "frozen A2 tool schemas"
        ),
        "frozen A2 tool schemas",
    )
    _artifact._expect_mapping(frozen, "frozen A2 tool schemas", _artifact._A2_TOOLS)
    return frozen


def _validate_a2_catalog_schemas(tools: Sequence[Mapping[str, Any]]) -> None:
    frozen = _load_a2_tool_schemas()
    for tool in tools:
        annotations = tool.get("annotations")
        if (
            not isinstance(annotations, Mapping)
            or annotations.get("readOnlyHint") is not True
            or annotations.get("idempotentHint") is not True
            or annotations.get("openWorldHint") is not False
        ):
            _refuse("read-only-family catalog is not closed and read-only")
        for field in ("inputSchema", "outputSchema"):
            if _canonical_json_bytes(tool.get(field)) != _canonical_json_bytes(
                frozen[tool["name"]][field]
            ):
                _refuse("A2 advertised tool schema differs from the frozen consumer contract")


def _validate_schema_value(value: Any, schema: Mapping[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, SchemaError, ValidationError
    except ImportError:
        _refuse("A2 consumer proof requires jsonschema from the installed project dependencies")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except (SchemaError, ValidationError):
        _refuse("native read-only value violates the advertised tool schema")


def _run_valid_journey(
    executable_path: Path, fixture_root: Path, expected_tools: Sequence[str] = _artifact._A1_TOOLS
) -> None:
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
            or any(not isinstance(tool, Mapping) for tool in tools)
            or [tool.get("name") for tool in tools] != list(expected_tools)
        ):
            _refuse("tools/list did not expose the candidate's exact tool catalog")
        a2_journey = tuple(expected_tools) == _artifact._A2_TOOLS
        if a2_journey:
            _validate_a2_catalog_schemas(tools)
            _validate_schema_value(
                {"name": "PreviewMarker", "kind": "class"}, tools[0]["inputSchema"]
            )
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
        if a2_journey:
            _expect_success(
                call,
                {
                    "kind": "find_code_symbol_success",
                    "results": [
                        {
                            "file": "Markers.cs",
                            "line": 3,
                            "name": "PreviewMarker",
                            "kind": "class",
                            "context": "public sealed class PreviewMarker { }",
                        }
                    ],
                },
                output_schema=tools[0]["outputSchema"],
            )
            reference_arguments = {"name": "PreviewMarker"}
            _validate_schema_value(reference_arguments, tools[1]["inputSchema"])
            _expect_success(
                _call(process, 4, "find_code_references", reference_arguments),
                {
                    "kind": "find_code_references_success",
                    "results": [
                        {
                            "file": "Markers.cs",
                            "line": 3,
                            "context": "public sealed class PreviewMarker { }",
                        },
                        {
                            "file": "PreviewReferences.cs",
                            "line": 5,
                            "context": "public PreviewMarker Value { get; } = new PreviewMarker();",
                        },
                    ],
                },
                output_schema=tools[1]["outputSchema"],
            )
            context_arguments = {"file": "PreviewReferences.cs", "line": 5, "radius": 1}
            _validate_schema_value(context_arguments, tools[2]["inputSchema"])
            _expect_success(
                _call(
                    process,
                    5,
                    "get_source_context",
                    context_arguments,
                ),
                {
                    "kind": "get_source_context_success",
                    "file": "PreviewReferences.cs",
                    "start_line": 4,
                    "end_line": 6,
                    "lines": [
                        {"line": 4, "text": "{"},
                        {
                            "line": 5,
                            "text": (
                                "    public PreviewMarker Value { get; } = new PreviewMarker();"
                            ),
                        },
                        {"line": 6, "text": "}"},
                    ],
                },
                output_schema=tools[2]["outputSchema"],
            )
        process.close_eof()
    finally:
        process.abort()


def _call(
    process: JsonRpcProcess, request_id: int | str, tool: str, arguments: Any
) -> Mapping[str, Any]:
    return _result(
        process.request(
            request_id,
            "tools/call",
            {
                "name": tool,
                "arguments": arguments,
                "_meta": _REQUEST_META,
            },
        )
    )


def _expect_success(
    result: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    output_schema: Mapping[str, Any] | None = None,
) -> None:
    content = result.get("content")
    if (
        result.get("resultType") != "complete"
        or result.get("isError") is not False
        or result.get("structuredContent") != expected
        or not isinstance(content, list)
        or len(content) != 1
        or content[0].get("type") != "text"
        or json.loads(content[0].get("text", "null")) != expected
    ):
        _refuse("native read-only result values or text/structured parity are incorrect")
    if output_schema is None:
        tool = expected.get("kind", "").removesuffix("_success")
        if tool not in _artifact._A2_TOOLS:
            _refuse("native read-only success has no frozen output schema")
        output_schema = _load_a2_tool_schemas()[tool]["outputSchema"]
    if not isinstance(output_schema, Mapping):
        _refuse("native read-only output schema must be an object")
    _validate_schema_value(result["structuredContent"], output_schema)


def _expect_failure(result: Mapping[str, Any], tool: str, error: str) -> None:
    content = result.get("content")
    if (
        not _closed_error(result, error.lower(), error, tool)
        or not isinstance(content, list)
        or len(content) != 1
        or content[0].get("type") != "text"
        or json.loads(content[0].get("text", "null")) != result["structuredContent"]
    ):
        _refuse("native read-only refusal leaked partial output or named the wrong route")


def _run_family_values(executable_path: Path, scratch: Path) -> None:
    root = scratch / "family-values"
    root.mkdir()
    lines = [
        "namespace Family;",
        "public sealed class PreviewMarker { }",
        "public sealed class Consumer {",
        "    private PreviewMarker _marker = new();",
        "    // PreviewMarkerSuffix is not an identifier match.",
        "}",
    ]
    (root / "Family.cs").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "Only.cs").write_text("    // unique basename\n", encoding="utf-8")
    window_lines = [f"    // window {line:02}" for line in range(1, 31)]
    (root / "Window.cs").write_text("\n".join(window_lines) + "\n", encoding="utf-8")
    process = JsonRpcProcess(executable_path, root)
    try:
        reference_rows = [
            {"file": "Family.cs", "line": line, "context": lines[line - 1].strip()}
            for line in (2, 4)
        ]
        for index, arguments in enumerate(
            (
                {"name": "PreviewMarker"},
                {"name": "PreviewMarker", "max_results": 1000},
                {"name": "PreviewMarker", "max_results": 1},
                {"name": "private PreviewMarker"},
            )
        ):
            expected = (
                reference_rows[:1]
                if index == 2
                else reference_rows[1:]
                if index == 3
                else reference_rows
            )
            _expect_success(
                _call(process, 110 + index, "find_code_references", arguments),
                {
                    "kind": "find_code_references_success",
                    "results": expected,
                },
            )
        for index, (arguments, start, end) in enumerate(
            (
                ({"file": "Family.cs", "line": 4}, 1, 6),
                ({"file": "Family.cs", "line": 4, "radius": 0}, 4, 4),
                ({"file": "Family.cs", "line": 1, "radius": 2147483647}, 1, 6),
                ({"file": str(root / "Family.cs"), "line": 6, "radius": 1}, 5, 6),
            )
        ):
            _expect_success(
                _call(process, 120 + index, "get_source_context", arguments),
                {
                    "kind": "get_source_context_success",
                    "file": "Family.cs",
                    "start_line": start,
                    "end_line": end,
                    "lines": [
                        {"line": line, "text": lines[line - 1]} for line in range(start, end + 1)
                    ],
                },
            )
        _expect_success(
            _call(process, 124, "get_source_context", {"file": "Only.cs", "line": 1}),
            {
                "kind": "get_source_context_success",
                "file": "nested/Only.cs",
                "start_line": 1,
                "end_line": 1,
                "lines": [{"line": 1, "text": "    // unique basename"}],
            },
        )
        _expect_failure(
            _call(process, 125, "get_source_context", {"file": "Family.cs", "line": 7}),
            "get_source_context",
            "INVALID_TOOL_ARGUMENTS",
        )
        _expect_success(
            _call(process, 126, "get_source_context", {"file": "Window.cs", "line": 15}),
            {
                "kind": "get_source_context_success",
                "file": "Window.cs",
                "start_line": 5,
                "end_line": 25,
                "lines": [{"line": line, "text": window_lines[line - 1]} for line in range(5, 26)],
            },
        )
        process.close_eof()
    finally:
        process.abort()


def _run_family_invalid_inputs(executable_path: Path, fixture_root: Path) -> None:
    cases = {
        "find_code_references": [
            None,
            [],
            {},
            {"name": None},
            {"name": True},
            {"name": " "},
            {"name": "x" * 257},
            {"name": "😀" * 129},
            {"name": "PreviewMarker", "extra": True},
            *(
                {"name": "PreviewMarker", "max_results": value}
                for value in (None, False, 0, -1, 1.5, "2", 2147483648)
            ),
        ],
        "get_source_context": [
            None,
            [],
            {},
            {"file": None, "line": 1},
            {"file": " ", "line": 1},
            {"file": True, "line": 1},
            {"file": "Markers.cs"},
            *(
                {"file": "Markers.cs", "line": value}
                for value in (None, True, 0, -1, 1.5, "1", 2147483648)
            ),
            *(
                {"file": "Markers.cs", "line": 1, "radius": value}
                for value in (None, False, -1, 1.5, "1", 2147483648)
            ),
            {"file": "Markers.cs", "line": 1, "extra": True},
        ],
    }
    process = JsonRpcProcess(executable_path, fixture_root)
    try:
        request_id = 140
        for tool, invalid_arguments in cases.items():
            for arguments in invalid_arguments:
                _expect_failure(
                    _call(process, request_id, tool, arguments), tool, "INVALID_TOOL_ARGUMENTS"
                )
                request_id += 1
        _expect_success(
            _call(process, request_id, "find_code_references", {"name": "NotPresent"}),
            {
                "kind": "find_code_references_success",
                "results": [],
            },
        )
        process.close_eof()
    finally:
        process.abort()


def _run_family_path_refusals(executable_path: Path, scratch: Path) -> None:
    root = scratch / "family-paths"
    root.mkdir()
    (root / "Allowed.cs").write_text("// allowed\n", encoding="utf-8")
    (root / "Unsupported.txt").write_text("// not source\n", encoding="utf-8")
    (root / ".gitignore").write_text("Ignored.cs\n", encoding="utf-8")
    (root / "Ignored.cs").write_text("// ignored\n", encoding="utf-8")
    for directory in ("bin", "obj", ".git", ".hg", ".svn", "left", "right"):
        (root / directory).mkdir()
        (root / directory / "Hidden.cs").write_text("// must not leak\n", encoding="utf-8")
    process = JsonRpcProcess(executable_path, root)
    try:
        paths = ["Unsupported.txt", "Ignored.cs", "Missing.cs"]
        for directory in ("bin", "obj", ".git", ".hg", ".svn"):
            paths.extend((f"{directory}/Hidden.cs", str(root / directory / "Hidden.cs")))
        for index, path in enumerate(paths):
            _expect_failure(
                _call(process, 200 + index, "get_source_context", {"file": path, "line": 1}),
                "get_source_context",
                "PREVIEW_SEARCH_UNREADABLE",
            )
        _expect_failure(
            _call(process, 220, "get_source_context", {"file": "Hidden.cs", "line": 1}),
            "get_source_context",
            "INVALID_TOOL_ARGUMENTS",
        )
        outside = scratch / "Outside.cs"
        outside.write_text("// private outside marker\n", encoding="utf-8")
        for index, path in enumerate(("../Outside.cs", str(outside))):
            _expect_failure(
                _call(process, 221 + index, "get_source_context", {"file": path, "line": 1}),
                "get_source_context",
                "PREVIEW_PATH_REFUSED",
            )
        process.close_eof()
    finally:
        process.abort()
    try:
        (root / "Escape.cs").symlink_to(outside)
    except OSError:
        _refuse("required read-only-family reparse fixture is unavailable")
    process = JsonRpcProcess(executable_path, root)
    try:
        for index, (tool, arguments) in enumerate(
            (
                ("find_code_references", {"name": "allowed"}),
                ("get_source_context", {"file": "Escape.cs", "line": 1}),
            )
        ):
            _expect_failure(
                _call(process, 225 + index, tool, arguments), tool, "PREVIEW_PATH_REFUSED"
            )
        process.close_eof()
    finally:
        process.abort()
    _run_locked_file_matrix(executable_path, scratch, tools=_artifact._A2_TOOLS)


def _run_family_budgets(executable_path: Path, scratch: Path) -> None:
    for count in (128, 129):
        root = scratch / f"family-budget-{count}"
        root.mkdir()
        (root / "Matches.cs").write_text("// BudgetMarker\n" * count, encoding="utf-8")
        rows = [
            {"file": "Matches.cs", "line": line, "context": "// BudgetMarker"}
            for line in range(1, count + 1)
        ]
        process = JsonRpcProcess(executable_path, root)
        try:
            for index, limit in enumerate((None, 128, 1000, 2147483647, 127)):
                arguments: dict[str, Any] = {"name": "BudgetMarker"}
                if limit is not None:
                    arguments["max_results"] = limit
                result = _call(process, 240 + index, "find_code_references", arguments)
                if count == 129 and limit != 127:
                    _expect_failure(
                        result, "find_code_references", "PREVIEW_SEARCH_BUDGET_EXCEEDED"
                    )
                else:
                    _expect_success(
                        result,
                        {
                            "kind": "find_code_references_success",
                            "results": rows[:127] if limit == 127 else rows,
                        },
                    )
            process.close_eof()
        finally:
            process.abort()
    root = scratch / "family-context-budget"
    root.mkdir()
    (root / "Oversized.cs").write_text("//" + "x" * (1024 * 1024), encoding="utf-8")
    process = JsonRpcProcess(executable_path, root)
    try:
        for index, (tool, arguments) in enumerate(
            (
                ("find_code_references", {"name": "NoMatch"}),
                ("get_source_context", {"file": "Oversized.cs", "line": 1}),
            )
        ):
            _expect_failure(
                _call(process, 250 + index, tool, arguments), tool, "PREVIEW_SEARCH_BUDGET_EXCEEDED"
            )
        process.close_eof()
    finally:
        process.abort()


def _run_family_frames(executable_path: Path, scratch: Path) -> None:
    for tool, count, line, arguments in (
        ("find_code_references", 128, "// FrameMarker " + "\x01" * 490, {"name": "FrameMarker"}),
        (
            "get_source_context",
            400,
            "//" + "x" * 510,
            {"file": "Frame.cs", "line": 1, "radius": 399},
        ),
    ):
        root = scratch / f"family-frame-{tool}"
        root.mkdir()
        (root / "Frame.cs").write_text((line + "\n") * count, encoding="utf-8")
        process = JsonRpcProcess(executable_path, root)
        try:
            _expect_failure(
                _call(process, "frame-" + "i" * 8192, tool, arguments),
                tool,
                "PREVIEW_SEARCH_BUDGET_EXCEEDED",
            )
            _expect_failure(_call(process, 260, tool, {}), tool, "INVALID_TOOL_ARGUMENTS")
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


def _run_locked_file_matrix(
    executable_path: Path, scratch: Path, *, tools: Sequence[str] = _artifact._A1_TOOLS
) -> None:
    if os.name != "nt":
        _refuse("file-system denial proof requires Windows")
    root = scratch / (
        "locked-file" if tuple(tools) == _artifact._A1_TOOLS else "family-locked-file"
    )
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
            for index, tool in enumerate(tools):
                arguments = (
                    {"file": "ZLocked.cs", "line": 1}
                    if tool == "get_source_context"
                    else {
                        "name": "PartialMarker",
                        **({"kind": "class"} if tool == "find_code_symbol" else {}),
                    }
                )
                result = _call(process, 60 + index, tool, arguments)
                _expect_failure(result, tool, "PREVIEW_SEARCH_UNREADABLE")
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
        resolved_command = shutil.which(command)
        if resolved_command is None:
            _refuse("unchanged Python rollback oracle could not run")
        resolved_command = str(Path(resolved_command).absolute())
        environment = os.environ.copy()
        environment["PATH"] = (
            str(Path(resolved_command).parent) + os.pathsep + environment.get("PATH", "")
        )
        result = subprocess.run(
            [resolved_command, *arguments],
            cwd=_PROJECT_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        _refuse("unchanged Python rollback oracle could not run")
    if result.returncode != 0:
        _refuse("unchanged Python rollback oracle did not reach PRODUCT_WORKS")
    if (
        not isinstance(result.stdout, bytes)
        or _artifact._load_retained_json_object(
            result.stdout, "unchanged Python rollback oracle output"
        )
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
    contract_id: str = _artifact._A1_CONSUMER_CONTRACT_ID,
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
        "valid-discovery-list-call": lambda: _run_valid_journey(
            executable_path, fixture_root, _artifact._consumer_contract(contract_id)["tools"]
        ),
        "readonly-family-values": lambda: _run_family_values(executable_path, scratch),
        "readonly-family-invalid-input": lambda: _run_family_invalid_inputs(
            executable_path, fixture_root
        ),
        "readonly-family-path-read-refusals": lambda: _run_family_path_refusals(
            executable_path, scratch
        ),
        "readonly-family-result-budgets": lambda: _run_family_budgets(executable_path, scratch),
        "readonly-family-frame-refusals": lambda: _run_family_frames(executable_path, scratch),
        "rollback-python-default": rollback,
    }
    results: list[dict[str, Any]] = []
    for scenario in consumer_proof_scenario_catalog(contract_id)["scenarios"]:
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
    remote = arguments.command == "remote"
    receipt_path = Path(arguments.receipt_output)
    if receipt_path.exists() or receipt_path.is_symlink():
        _refuse("consumer proof receipt already exists")
    references = CandidateReferences.load(arguments.references)
    fixture_root = _ordinary_directory(arguments.fixture_root, "fixture root")
    download_root = _ordinary_directory(arguments.download_root, "download root")
    with tempfile.TemporaryDirectory(prefix="a1-preview-artifact-", dir=download_root) as temporary:
        fresh = Path(temporary)
        downloader = _artifact.ArtifactDownloader(fresh)
        identity_path = downloader.download_file(references.candidate_identity)
        identity_bytes = _read_regular_bytes(identity_path, "downloaded candidate identity")
        if _sha256_bytes(identity_bytes) != references.candidate_identity.sha256:
            _refuse("downloaded candidate identity hash is wrong")
        identity = _artifact._load_retained_json_object(
            identity_bytes, "downloaded candidate identity"
        )
        validated_identity = assemble_candidate_identity(identity)
        candidate = validated_identity["candidate"]
        consumer_contract = _artifact._candidate_consumer_contract(candidate)
        contract_id = consumer_contract["scenario_catalog_id"]
        manifest_binding = candidate["preview_manifest"]
        _require_candidate_file_reference(
            references.archive, manifest_binding["archive_reference"], "archive"
        )
        _require_candidate_file_reference(
            references.manifest, manifest_binding["manifest_reference"], "manifest"
        )
        if remote:
            _artifact._verified_raw_candidate(
                validated_identity, identity_bytes, references.candidate_identity.as_mapping()
            )
        catalog_path = downloader.download_file(references.release_gate_catalog)
        catalog_bytes = _read_regular_bytes(catalog_path, "downloaded release gate catalog")
        if _sha256_bytes(catalog_bytes) != references.release_gate_catalog.sha256:
            _refuse("downloaded release gate catalog hash is wrong")
        catalog = _artifact._load_retained_json_object(
            catalog_bytes, "downloaded release gate catalog"
        )
        if remote:
            _artifact._validate_release_gate_catalog_for_consumer_proof(catalog, candidate)
            archive_path, manifest_path, download_origin = download_public_release(
                candidate, arguments.release_id, fresh / "public-assets"
            )
        else:
            archive_path, manifest_path = downloader.download_payload(
                references.archive,
                references.manifest,
                candidate["build"]["artifact"]["sha256"],
            )
            download_origin = {
                "repository": candidate["build"]["repository"],
                "workflow_path": ".github/workflows/stateless-preview.yml",
                "run_id": candidate["build"]["run_id"],
                "artifact": candidate["build"]["artifact"],
                "archive_path": manifest_binding["archive_reference"]["path"],
                "manifest_path": manifest_binding["manifest_reference"]["path"],
            }
        if archive_path.parent != manifest_path.parent:
            _refuse("archive and manifest were not downloaded into one fresh payload directory")
        verified = verify_and_extract_retained_artifact(
            validated_identity,
            archive_path,
            manifest_path,
            fresh / "extracted",
        )
        executable_path = Path(verified["executable_path"])
        if executable_path.name != _EXECUTABLE_NAME or not _artifact._path_within(
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
            contract_id,
        )
        if rollback_result is None:
            _refuse("consumer matrix did not run the unchanged Python rollback oracle")
        catalog_definition = consumer_proof_scenario_catalog(contract_id)
        scenario_ids = [scenario["scenario_id"] for scenario in catalog_definition["scenarios"]]
        receipt = {
            "receipt_schema_version": "1.0",
            "receipt_id": arguments.receipt_id,
            "candidate_identity_record": references.candidate_identity.as_mapping(),
            "candidate": candidate,
            "proof_stage": "remote_release" if remote else "retained_artifact",
            "download_origin": download_origin,
            "input_identity_results": {
                "archive": verified["archive"],
                "manifest": verified["manifest"],
                "executable": verified["executable"],
                "archive_matches_candidate": True,
                "manifest_matches_candidate": True,
                "executable_matches_manifest": True,
                "inherited_verifier_equations_pass": True,
            },
            "fixture_identity": _fixture_identity(fixture_root, contract_id),
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
                "catalog": consumer_contract["tools"],
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
        }
        binding = {
            "candidate_identity": validated_identity,
            "candidate_identity_bytes": identity_bytes,
            "candidate_identity_reference": references.candidate_identity.as_mapping(),
            "release_gate_catalog": catalog,
        }
        if remote:
            sealed = _artifact._freeze(
                _artifact._validate_stage_consumer_proof(receipt, remote=True, **binding)
            )
        else:
            sealed = seal_artifact_consumer_proof(receipt, **binding)
    _write_receipt_once(arguments.receipt_output, sealed)
    return sealed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, description in (
        ("candidate", "prove one downloaded retained candidate"),
        ("remote", "prove freshly downloaded public prerelease assets"),
    ):
        consumer = commands.add_parser(command, help=description)
        consumer.add_argument("--references", required=True)
        consumer.add_argument("--download-root", required=True)
        consumer.add_argument("--fixture-root", required=True)
        consumer.add_argument("--receipt-output", required=True)
        consumer.add_argument("--receipt-id", required=True)
        consumer.add_argument("--recorded-at", required=True)
        consumer.add_argument("--python-rollback-command", required=True)
        consumer.add_argument("--python-rollback-argument", action="append", default=[])
        if command == "remote":
            consumer.add_argument("--release-id", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command in ("candidate", "remote"):
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
