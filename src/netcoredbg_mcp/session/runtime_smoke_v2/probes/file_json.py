from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._common import attach_expected_and_status, blocked_probe, probe_name


async def handle_file_json(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "file.json"
    raw_path = str(probe.get("path") or "")
    jsonpath = str(probe.get("jsonpath") or probe.get("path_expr") or "")
    if not raw_path:
        return blocked_probe(
            probe,
            kind=kind,
            reason="missing required path",
            requested={"path": raw_path},
            accepted={"path": "non-empty file path"},
            next_step="Provide a file path for the file.json probe.",
        )
    if not jsonpath:
        return blocked_probe(
            probe,
            kind=kind,
            reason="missing required jsonpath",
            requested={"jsonpath": jsonpath},
            accepted={"jsonpath": "non-empty JSONPath expression"},
            next_step="Provide jsonpath or path_expr for the file.json probe.",
        )
    session = getattr(context, "session", None)
    validate_path = getattr(session, "validate_path", None)
    try:
        resolved_path = (
            str(validate_path(raw_path, must_exist=False))
            if callable(validate_path)
            else str(Path(raw_path).resolve())
        )
    except ValueError as exc:
        return blocked_probe(
            probe,
            kind=kind,
            reason="path outside project scope",
            requested={"path": raw_path},
            accepted={"path": "project-relative or NETCOREDBG_ALLOWED_PATHS path"},
            next_step=(
                "Use project-relative paths or add the directory to NETCOREDBG_ALLOWED_PATHS."
            ),
        ) | {"validation_error": str(exc)}

    path = Path(resolved_path)
    if not path.exists():
        return {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": "FAIL",
            "reason": "json file missing",
            "value": None,
            "resolved_path": str(path),
            "missing_side": "file",
        }
    if not path.is_file():
        return {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": "FAIL",
            "reason": "path is not a file",
            "value": None,
            "resolved_path": str(path),
            "missing_side": "file",
        }

    try:
        from jsonpath_ng import parse  # type: ignore[import-untyped]
        from jsonpath_ng.exceptions import (  # type: ignore[import-untyped]
            JsonPathLexerError,
            JsonPathParserError,
        )
    except ImportError:
        return blocked_probe(
            probe,
            kind=kind,
            reason="jsonpath-ng is not installed",
            requested={"jsonpath": jsonpath},
            accepted={"dependency": "jsonpath-ng>=1.8.0,<2.0.0"},
            next_step="Install jsonpath-ng before running file.json probes.",
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        matches = [match.value for match in parse(jsonpath).find(data)]
    except (json.JSONDecodeError, JsonPathLexerError, JsonPathParserError) as exc:
        return {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": "FAIL",
            "reason": "jsonpath evaluation failed",
            "value": None,
            "resolved_path": str(path),
            "jsonpath": jsonpath,
            "error": str(exc),
        }

    value: Any
    if not matches:
        value = None
    elif len(matches) == 1:
        value = matches[0]
    else:
        value = matches
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "PASS",
        "value": value,
        "resolved_path": str(path),
        "jsonpath": jsonpath,
        "evidence_ref": f"file:{path.name}:{jsonpath}",
    }
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)
