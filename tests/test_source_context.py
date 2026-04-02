"""Tests for source context reading utility."""

import os

from netcoredbg_mcp.utils.source import read_source_context


class TestReadSourceContext:
    """Tests for read_source_context."""

    def test_normal_case(self, tmp_path):
        """Test reading source context from a normal file."""
        source_file = tmp_path / "Program.cs"
        lines = [
            "using System;",
            "",
            "class Program",
            "{",
            "    static void Main()",
            "    {",
            "        Console.WriteLine(\"Hello\");",
            "    }",
            "}",
        ]
        source_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = read_source_context(str(source_file), 7, context_lines=2)

        assert result is not None
        # Lines 5-9 (line 7 +/- 2)
        assert len(result) == 5
        # Check current line marker
        current_lines = [entry for entry in result if entry["current"]]
        assert len(current_lines) == 1
        assert current_lines[0]["line"] == 7
        assert "Console.WriteLine" in current_lines[0]["text"]

    def test_context_at_file_start(self, tmp_path):
        """Test reading context at the first line of a file."""
        source_file = tmp_path / "First.cs"
        source_file.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

        result = read_source_context(str(source_file), 1, context_lines=5)

        assert result is not None
        # Should start at line 1 (cannot go before it)
        assert result[0]["line"] == 1
        assert result[0]["current"] is True

    def test_context_at_file_end(self, tmp_path):
        """Test reading context at the last line of a file."""
        source_file = tmp_path / "Last.cs"
        lines = ["line1", "line2", "line3", "line4", "line5"]
        source_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = read_source_context(str(source_file), 5, context_lines=5)

        assert result is not None
        # Should end at line 5 (cannot go past it)
        assert result[-1]["line"] == 5
        assert result[-1]["current"] is True

    def test_missing_file_returns_none(self):
        """Test that a nonexistent file returns None."""
        result = read_source_context("/nonexistent/path/file.cs", 1)

        assert result is None

    def test_none_file_path_returns_none(self):
        """Test that None file_path returns None."""
        result = read_source_context(None, 1)

        assert result is None

    def test_out_of_range_line_returns_none(self, tmp_path):
        """Test that a line number beyond file length returns None."""
        source_file = tmp_path / "Short.cs"
        source_file.write_text("only one line\n", encoding="utf-8")

        result = read_source_context(str(source_file), 999)

        assert result is None

    def test_line_zero_returns_none(self, tmp_path):
        """Test that line 0 (invalid, 1-based) returns None."""
        source_file = tmp_path / "Zero.cs"
        source_file.write_text("content\n", encoding="utf-8")

        result = read_source_context(str(source_file), 0)

        assert result is None
