"""Source code context utilities for debug responses."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def read_source_context(
    file_path: str | None,
    line: int,
    context_lines: int = 5,
) -> list[dict[str, Any]] | None:
    """Read surrounding source lines for a stopped location.

    Args:
        file_path: Absolute path to source file (None if unavailable)
        line: Current line number (1-based)
        context_lines: Number of lines before and after to include

    Returns:
        List of {line, text, current} dicts, or None if source unavailable.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        logger.debug(f"Cannot read source file {file_path}: {e}")
        return None

    if line < 1 or line > len(all_lines):
        return None

    start = max(0, line - 1 - context_lines)
    end = min(len(all_lines), line + context_lines)

    result = []
    for i in range(start, end):
        result.append({
            "line": i + 1,
            "text": all_lines[i].rstrip("\n\r"),
            "current": (i + 1) == line,
        })

    return result
