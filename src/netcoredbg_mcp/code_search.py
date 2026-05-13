"""Project-scoped source discovery for code navigation tools."""

from __future__ import annotations

import fnmatch
import os
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
