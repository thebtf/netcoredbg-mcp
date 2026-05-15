"""Python wrapper for the Roslyn Edit-and-Continue delta compiler."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceEdit:
    start_line: int
    end_line: int
    new_text: str


@dataclass(frozen=True)
class DeltaResult:
    success: bool
    il_delta_path: str | None
    metadata_delta_path: str | None
    pdb_delta_path: str | None
    rude_edits: tuple[str, ...]
    diagnostics: tuple[str, ...]


def compile_delta(
    project_path: str | os.PathLike[str],
    file_path: str | os.PathLike[str],
    edits: Sequence[SourceEdit],
    *,
    module_path: str | os.PathLike[str] | None = None,
    compiler_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    timeout: float = 30.0,
) -> DeltaResult:
    """Compile source edits into IL, metadata, and PDB deltas."""
    command = _compiler_command(compiler_path)
    payload: dict[str, object] = {
        "project_path": str(project_path),
        "file_path": str(file_path),
        "edits": [
            {
                "start_line": edit.start_line,
                "end_line": edit.end_line,
                "new_text": edit.new_text,
            }
            for edit in edits
        ],
    }
    if module_path is not None:
        payload["module_path"] = str(module_path)
    if output_dir is not None:
        payload["output_dir"] = str(output_dir)

    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return _failure(f"EnC compiler executable not found: {exc.filename}")
    except subprocess.TimeoutExpired:
        return _failure(f"EnC compiler timed out after {timeout:g}s.")

    stdout = completed.stdout.strip()
    if not stdout:
        stderr = completed.stderr.strip()
        return _failure(stderr or f"EnC compiler exited with code {completed.returncode}.")

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _failure(f"EnC compiler returned invalid JSON: {exc}")

    result = _parse_result(parsed)
    if completed.returncode != 0 and result.success:
        return _failure(
            f"EnC compiler returned success JSON with exit code {completed.returncode}."
        )
    return result


def _compiler_command(compiler_path: str | os.PathLike[str] | None) -> list[str]:
    configured = Path(compiler_path) if compiler_path is not None else _default_compiler_project()
    suffix = configured.suffix.lower()
    if suffix == ".csproj":
        return ["dotnet", "run", "--project", str(configured), "--"]
    if suffix == ".dll":
        return ["dotnet", str(configured)]
    return [str(configured)]


def _default_compiler_project() -> Path:
    env_path = os.environ.get("NETCOREDBG_MCP_ENC_COMPILER")
    if env_path:
        return Path(env_path)

    package_root = Path(__file__).resolve().parents[1]
    candidates = [
        package_root / "tools" / "enc_compiler" / "EncCompiler.csproj",
        package_root.parent.parent / "tools" / "enc_compiler" / "EncCompiler.csproj",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _parse_result(parsed: object) -> DeltaResult:
    if not isinstance(parsed, dict):
        return _failure("EnC compiler JSON response must be an object.")

    return DeltaResult(
        success=bool(parsed.get("success", False)),
        il_delta_path=_optional_string(parsed.get("il_delta_path")),
        metadata_delta_path=_optional_string(parsed.get("metadata_delta_path")),
        pdb_delta_path=_optional_string(parsed.get("pdb_delta_path")),
        rude_edits=tuple(str(item) for item in parsed.get("rude_edits", [])),
        diagnostics=tuple(str(item) for item in parsed.get("diagnostics", [])),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _failure(message: str) -> DeltaResult:
    return DeltaResult(
        success=False,
        il_delta_path=None,
        metadata_delta_path=None,
        pdb_delta_path=None,
        rude_edits=(),
        diagnostics=(message,),
    )
