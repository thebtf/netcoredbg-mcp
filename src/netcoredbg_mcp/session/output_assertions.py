"""Runtime smoke output checkpoints and batch assertions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from re import Pattern
from typing import Any

from .runtime_smoke import RuntimeSmokeSession, compact_output_evidence
from .state import OutputEntry, TerminalStatus


@dataclass(frozen=True)
class OutputAssertionResult:
    """Serializable output assertion operation result."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class OutputAssertionService:
    """Manage output checkpoints and batch required/forbidden assertions."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def create_checkpoint(self, name: str) -> OutputAssertionResult:
        if name in self._checkpoints:
            return self._fail(
                "output checkpoint already exists",
                checkpoint=name,
                existing_checkpoint=name,
            )

        entries = self._entries()
        checkpoint = {
            "name": name,
            "entry_count": len(entries),
            "byte_offset": self._byte_length(entries),
            "first_entry_hash": self._entry_hash(entries[0]) if entries else None,
        }
        self._checkpoints[name] = checkpoint
        return self._pass(
            "output checkpoint created",
            checkpoint=name,
            entry_count=checkpoint["entry_count"],
            byte_offset=checkpoint["byte_offset"],
            evidence_refs=[{
                "kind": "output_checkpoint",
                "ref": f"output:{name}",
                "summary": "output checkpoint created",
            }],
        )

    def assert_since(
        self,
        checkpoint: str,
        *,
        required: list[str] | None = None,
        forbidden: list[str] | None = None,
        regex: bool = True,
        max_matches: int = 20,
    ) -> OutputAssertionResult:
        saved = self._checkpoints.get(checkpoint)
        if saved is None:
            return self._fail("output checkpoint not found", checkpoint=checkpoint)

        entries = self._entries()
        if self._is_trimmed(saved, entries):
            return self._fail("output checkpoint range trimmed", checkpoint=checkpoint)

        required_patterns = list(required or [])
        forbidden_patterns = list(forbidden or [])
        compiled = self._compile_patterns(required_patterns + forbidden_patterns, regex)
        if isinstance(compiled, OutputAssertionResult):
            return compiled

        searched_entries = entries[int(saved["entry_count"]):]
        searched_text = "".join(entry.text for entry in searched_entries)
        lines = searched_text.splitlines()

        required_compiled = compiled[:len(required_patterns)]
        forbidden_compiled = compiled[len(required_patterns):]
        matches, missing_required = self._match_required(
            required_patterns,
            required_compiled,
            lines,
            max_matches,
        )
        forbidden_matches = self._match_forbidden(
            forbidden_patterns,
            forbidden_compiled,
            lines,
            max_matches,
        )
        status = (
            TerminalStatus.PASS
            if not missing_required and not forbidden_matches
            else TerminalStatus.FAIL
        )
        summary = compact_output_evidence(
            checkpoint=checkpoint,
            matched_line_count=len(matches),
            missing_count=len(missing_required),
            forbidden_count=len(forbidden_matches),
        )
        return OutputAssertionResult({
            "status": status.value,
            "reason": (
                "output assertions passed"
                if status == TerminalStatus.PASS
                else "output assertions failed"
            ),
            "checkpoint": checkpoint,
            "summary": summary,
            "searched_range": {
                "start_entry": int(saved["entry_count"]),
                "end_entry": len(entries),
                "start_byte": int(saved["byte_offset"]),
                "end_byte": self._byte_length(entries),
                "line_count": len(lines),
            },
            "matches": matches,
            "missing_required": missing_required,
            "forbidden_matches": forbidden_matches,
            "evidence_refs": [{
                "kind": "output_assertion",
                "ref": f"output:{checkpoint}",
                "summary": (
                    f"matched={len(matches)} missing={len(missing_required)} "
                    f"forbidden={len(forbidden_matches)}"
                ),
            }],
        })

    @property
    def _runtime_smoke(self) -> RuntimeSmokeSession:
        return self._session.runtime_smoke

    @property
    def _checkpoints(self) -> dict[str, Any]:
        return self._runtime_smoke.output_checkpoints

    def _entries(self) -> list[OutputEntry]:
        return list(getattr(self._session.state, "output_buffer", []))

    @staticmethod
    def _byte_length(entries: list[OutputEntry]) -> int:
        return sum(len(entry.text.encode("utf-8")) for entry in entries)

    @staticmethod
    def _is_trimmed(saved: dict[str, Any], entries: list[OutputEntry]) -> bool:
        entry_count = int(saved["entry_count"])
        if entry_count > len(entries):
            return True
        first_entry_hash = saved.get("first_entry_hash")
        return bool(
            entry_count
            and entries
            and first_entry_hash != OutputAssertionService._entry_hash(entries[0])
        )

    @staticmethod
    def _entry_hash(entry: OutputEntry) -> str:
        digest = sha256()
        digest.update(entry.category.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.text.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.variables_reference).encode("ascii"))
        return digest.hexdigest()

    def _compile_patterns(
        self,
        patterns: list[str],
        regex: bool,
    ) -> list[Pattern[str]] | OutputAssertionResult:
        compiled: list[Pattern[str]] = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern if regex else re.escape(pattern)))
            except re.error as exc:
                return self._fail(
                    "invalid regex",
                    invalid_pattern=pattern,
                    regex_error=str(exc),
                    skipped_assertions=True,
                    forbidden_matches=[],
                )
        return compiled

    @staticmethod
    def _match_required(
        patterns: list[str],
        compiled: list[Pattern[str]],
        lines: list[str],
        max_matches: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        matches: list[dict[str, Any]] = []
        missing = []
        for pattern, compiled_pattern in zip(patterns, compiled, strict=True):
            pattern_found = False
            for line_number, line in enumerate(lines, 1):
                if compiled_pattern.search(line):
                    pattern_found = True
                    if len(matches) < max_matches:
                        matches.append({
                            "pattern": pattern,
                            "line": line_number,
                            "text": line,
                        })
            if not pattern_found:
                missing.append(pattern)
        return matches, missing

    @staticmethod
    def _match_forbidden(
        patterns: list[str],
        compiled: list[Pattern[str]],
        lines: list[str],
        max_matches: int,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for pattern, compiled_pattern in zip(patterns, compiled, strict=True):
            for line_number, line in enumerate(lines, 1):
                if compiled_pattern.search(line) and len(matches) < max_matches:
                    matches.append({
                        "pattern": pattern,
                        "line": line_number,
                        "text": line,
                    })
        return matches

    def _pass(self, reason: str, **payload: Any) -> OutputAssertionResult:
        return OutputAssertionResult({
            "status": TerminalStatus.PASS.value,
            "reason": reason,
            **payload,
        })

    def _fail(self, reason: str, **payload: Any) -> OutputAssertionResult:
        return OutputAssertionResult({
            "status": TerminalStatus.FAIL.value,
            "reason": reason,
            **payload,
        })
