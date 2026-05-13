"""Project-scoped source discovery for code navigation tools."""

from __future__ import annotations

import fnmatch
import multiprocessing
import os
import queue
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_EXTENSIONS = frozenset(
    {
        ".cs",
        ".xaml",
        ".axaml",
        ".csproj",
        ".json",
        ".config",
    }
)
ALWAYS_IGNORED_DIRS = frozenset({".git", ".hg", ".svn"})
CodeSymbolResult = dict[str, str | int]
CodeReferenceResult = dict[str, str | int]
CodeContextResult = dict[str, object]
CodeSearchResult = dict[str, str | int]
SUPPORTED_SYMBOL_KINDS = frozenset({"class", "method", "property", "field"})
MAX_REFERENCE_RESULTS = 1000
MAX_SEARCH_RESULTS = 1000
DEFAULT_SEARCH_TIMEOUT_SECONDS = 5.0
_CSHARP_MODIFIERS = (
    "public",
    "private",
    "protected",
    "internal",
    "static",
    "abstract",
    "sealed",
    "partial",
    "virtual",
    "override",
    "async",
    "extern",
    "readonly",
    "const",
    "volatile",
    "required",
    "new",
)
_MODIFIER_PATTERN = "|".join(_CSHARP_MODIFIERS)
_TYPE_PATTERN = r"[\w.<>,\[\]?]+"
_CSHARP_SYMBOL_PATTERNS = {
    "class": rf"^\s*(?:(?:{_MODIFIER_PATTERN})\s+)*(?:class|record|struct|interface)\s+__NAME__\b",
    "method": rf"^\s*(?:(?:{_MODIFIER_PATTERN})\s+)*(?:{_TYPE_PATTERN}\s+)+__NAME__\s*\(",
    "property": rf"^\s*(?:(?:{_MODIFIER_PATTERN})\s+)*(?:{_TYPE_PATTERN}\s+)+__NAME__\s*\{{",
    "field": rf"^\s*(?:(?:{_MODIFIER_PATTERN})\s+)*(?:{_TYPE_PATTERN}\s+)+__NAME__\s*(?:=|;)",
}


@dataclass(frozen=True)
class _GitIgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool
    has_slash: bool

    @classmethod
    def parse(cls, line: str) -> _GitIgnoreRule | None:
        value = line.strip()
        if not value or value.startswith("#"):
            return None

        negated = value.startswith("!")
        if negated:
            value = value[1:].strip()
            if not value:
                return None

        anchored = value.startswith("/")
        if anchored:
            value = value.lstrip("/")

        directory_only = value.endswith("/")
        value = value.rstrip("/")
        if not value:
            return None

        return cls(
            pattern=value,
            negated=negated,
            directory_only=directory_only,
            anchored=anchored,
            has_slash="/" in value,
        )

    def matches(self, relative_path: str, *, is_dir: bool) -> bool:
        path = relative_path.replace("\\", "/").strip("/")
        if not path:
            return False

        if self.directory_only and not is_dir:
            return self._matches_directory_parent(path)

        if self.anchored or self.has_slash:
            return fnmatch.fnmatchcase(path, self.pattern)

        if self.directory_only:
            return any(fnmatch.fnmatchcase(part, self.pattern) for part in path.split("/"))

        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], self.pattern)

    def _matches_directory_parent(self, relative_path: str) -> bool:
        if self.anchored or self.has_slash:
            return relative_path == self.pattern or relative_path.startswith(f"{self.pattern}/")

        return any(
            fnmatch.fnmatchcase(part, self.pattern)
            for part in relative_path.split("/")[:-1]
        )


class CodeSearchEngine:
    """Discover source files below one validated project root."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        extensions: Iterable[str] | None = None,
    ) -> None:
        root = Path(project_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(f"Project root is not a directory: {root}")

        self.project_root = root
        self.extensions = frozenset(
            _normalize_extension(extension)
            for extension in (extensions or DEFAULT_SOURCE_EXTENSIONS)
        )
        self._ignore_rules = tuple(_load_gitignore_rules(root))

    def iter_source_files(self, file_glob: str | None = None) -> Iterator[Path]:
        """Yield supported, non-ignored project files in deterministic order."""
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            current_dir = Path(dirpath)
            dirnames[:] = sorted(
                dirname
                for dirname in dirnames
                if not self._is_ignored(current_dir / dirname, is_dir=True)
            )

            for filename in sorted(filenames):
                path = current_dir / filename
                if not self._is_supported_file(path):
                    continue
                if self._is_ignored(path, is_dir=False):
                    continue
                if file_glob is not None and not self._matches_file_glob(path, file_glob):
                    continue
                yield path

    def find_code_symbol(self, name: str, kind: str | None = None) -> list[CodeSymbolResult]:
        """Find C# symbol definitions by name."""
        if not name.strip():
            raise ValueError("Symbol name must not be empty")
        if kind is not None and kind not in SUPPORTED_SYMBOL_KINDS:
            supported = ", ".join(sorted(SUPPORTED_SYMBOL_KINDS))
            raise ValueError(f"Unsupported symbol kind '{kind}'. Supported kinds: {supported}")

        escaped_name = re.escape(name)
        kinds = (kind,) if kind is not None else ("class", "method", "property", "field")
        patterns = tuple(
            (
                symbol_kind,
                re.compile(_CSHARP_SYMBOL_PATTERNS[symbol_kind].replace("__NAME__", escaped_name)),
            )
            for symbol_kind in kinds
        )

        results: list[CodeSymbolResult] = []
        for path in self.iter_source_files("*.cs"):
            relative_file = path.relative_to(self.project_root).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, start=1):
                stripped = line.strip()
                for symbol_kind, pattern in patterns:
                    if pattern.search(line):
                        results.append(
                            {
                                "file": relative_file,
                                "line": line_number,
                                "name": name,
                                "kind": symbol_kind,
                                "context": stripped,
                            }
                        )
                        break

        return results

    def find_code_references(
        self,
        name: str,
        *,
        max_results: int = MAX_REFERENCE_RESULTS,
    ) -> list[CodeReferenceResult]:
        """Find literal symbol references across supported project files."""
        if not name.strip():
            raise ValueError("Reference name must not be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        limit = min(max_results, MAX_REFERENCE_RESULTS)
        results: list[CodeReferenceResult] = []
        for path in self.iter_source_files():
            relative_file = path.relative_to(self.project_root).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if name not in line:
                    continue
                results.append(
                    {
                        "file": relative_file,
                        "line": line_number,
                        "context": line.strip(),
                    }
                )
                if len(results) >= limit:
                    return results

        return results

    def get_source_context(
        self,
        file_path: str | os.PathLike[str],
        *,
        line: int,
        radius: int = 10,
    ) -> CodeContextResult:
        """Read source lines around a 1-based line number."""
        if line < 1:
            raise ValueError("line must be at least 1")
        if radius < 0:
            raise ValueError("radius must be non-negative")

        path = self._resolve_project_file(file_path)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line > len(lines):
            raise ValueError(f"line {line} is outside file range 1..{len(lines)}")

        start_line = max(1, line - radius)
        end_line = min(len(lines), line + radius)
        return {
            "file": path.relative_to(self.project_root).as_posix(),
            "start_line": start_line,
            "end_line": end_line,
            "lines": [
                {"line": line_number, "text": lines[line_number - 1]}
                for line_number in range(start_line, end_line + 1)
            ],
        }

    def search_source(
        self,
        pattern: str,
        *,
        file_glob: str | None = None,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> list[CodeSearchResult]:
        """Search project source files with a process-enforced timeout."""
        if not pattern:
            raise ValueError("Search pattern must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        re.compile(pattern)
        file_paths = [str(path) for path in self.iter_source_files(file_glob)]
        if not file_paths:
            return []

        limit = min(max_results, MAX_SEARCH_RESULTS)
        context = multiprocessing.get_context("spawn")
        output = context.Queue(maxsize=1)
        process = context.Process(
            target=_search_source_worker,
            args=(str(self.project_root), file_paths, pattern, limit, output),
            daemon=True,
        )
        process.start()
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
            raise TimeoutError(f"search_source exceeded {timeout_seconds:.2f}s timeout")

        try:
            payload = output.get_nowait()
        except queue.Empty as exc:
            raise RuntimeError(f"search_source worker exited with code {process.exitcode}") from exc
        finally:
            output.close()
            output.join_thread()

        if not payload["ok"]:
            raise RuntimeError(payload["error"])
        return payload["results"]

    def _is_supported_file(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def _is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        relative_path = path.relative_to(self.project_root).as_posix()
        if is_dir and path.name in ALWAYS_IGNORED_DIRS:
            return True

        ignored = False
        for rule in self._ignore_rules:
            if rule.matches(relative_path, is_dir=is_dir):
                ignored = not rule.negated
        return ignored

    def _matches_file_glob(self, path: Path, file_glob: str) -> bool:
        relative_path = path.relative_to(self.project_root).as_posix()
        return fnmatch.fnmatchcase(relative_path, file_glob) or fnmatch.fnmatchcase(
            path.name,
            file_glob,
        )

    def _resolve_project_file(self, file_path: str | os.PathLike[str]) -> Path:
        raw_path = Path(file_path)
        candidate = (
            raw_path.expanduser()
            if raw_path.is_absolute()
            else self.project_root / raw_path
        )
        resolved = candidate.resolve(strict=False)
        if not _is_relative_to(resolved, self.project_root):
            raise ValueError(f"Path is outside project root: {file_path}")

        if resolved.exists():
            if not resolved.is_file():
                raise IsADirectoryError(f"Path is not a file: {file_path}")
            return resolved

        if len(raw_path.parts) == 1:
            return self._resolve_unique_basename(raw_path.name)

        raise FileNotFoundError(f"Source file not found: {file_path}")

    def _resolve_unique_basename(self, filename: str) -> Path:
        matches = [path for path in self.iter_source_files() if path.name == filename]
        if not matches:
            raise FileNotFoundError(f"Source file not found: {filename}")
        if len(matches) > 1:
            raise ValueError(f"Source file basename is ambiguous: {filename}")
        return matches[0]


def _normalize_extension(extension: str) -> str:
    normalized = extension.lower()
    return normalized if normalized.startswith(".") else f".{normalized}"


def _load_gitignore_rules(project_root: Path) -> list[_GitIgnoreRule]:
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return []

    rules: list[_GitIgnoreRule] = []
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        rule = _GitIgnoreRule.parse(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _search_source_worker(
    project_root: str,
    file_paths: list[str],
    pattern: str,
    max_results: int,
    output: multiprocessing.Queue,
) -> None:
    try:
        root = Path(project_root)
        compiled = re.compile(pattern)
        results: list[CodeSearchResult] = []
        for file_path in file_paths:
            path = Path(file_path)
            relative_file = path.relative_to(root).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if compiled.search(line) is None:
                    continue
                results.append(
                    {
                        "file": relative_file,
                        "line": line_number,
                        "context": line.strip(),
                    }
                )
                if len(results) >= max_results:
                    output.put({"ok": True, "results": results})
                    return

        output.put({"ok": True, "results": results})
    except Exception as exc:
        output.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
