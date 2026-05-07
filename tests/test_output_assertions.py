"""Runtime smoke output checkpoint and assertion tests."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.output_assertions import OutputAssertionService
from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState, OutputEntry
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class FakeOutputSession:
    def __init__(self, max_entries: int | None = None) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=deque(maxlen=max_entries),
        )

    def append_output(self, text: str, category: str = "stdout") -> None:
        self.state.output_buffer.append(OutputEntry(text=text, category=category))


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    pass


def test_checkpoint_then_assert_since_returns_bounded_match_evidence() -> None:
    session = FakeOutputSession()
    service = OutputAssertionService(session)
    session.append_output("boot\nready\n")

    checkpoint = service.create_checkpoint("after_boot").to_dict()
    session.append_output("selected row 1\nselected row 2\nwarning: slow\n")

    result = service.assert_since(
        "after_boot",
        required=["selected row", "warning"],
        forbidden=["fatal"],
        max_matches=2,
    ).to_dict()

    assert checkpoint["status"] == "PASS"
    assert result["status"] == "PASS"
    assert result["summary"] == {
        "checkpoint": "after_boot",
        "matched_line_count": 2,
        "missing_count": 0,
        "forbidden_count": 0,
    }
    assert result["searched_range"]["start_entry"] == 1
    assert result["searched_range"]["end_entry"] == 2
    assert result["searched_range"]["line_count"] == 3
    assert len(result["matches"]) == 2


def test_missing_required_and_matched_forbidden_patterns_are_both_listed() -> None:
    session = FakeOutputSession()
    service = OutputAssertionService(session)
    session.append_output("boot\n")
    service.create_checkpoint("start")
    session.append_output("selected row 1\nfatal error\n")

    result = service.assert_since(
        "start",
        required=["missing text"],
        forbidden=["fatal"],
    ).to_dict()

    assert result["status"] == "FAIL"
    assert result["missing_required"] == ["missing text"]
    assert result["forbidden_matches"] == [
        {"pattern": "fatal", "line": 2, "text": "fatal error"}
    ]


def test_invalid_regex_fails_and_skips_later_assertions() -> None:
    session = FakeOutputSession()
    service = OutputAssertionService(session)
    service.create_checkpoint("start")
    session.append_output("selected row 1\n")

    result = service.assert_since(
        "start",
        required=["["],
        forbidden=["selected"],
        regex=True,
    ).to_dict()

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid regex"
    assert result["invalid_pattern"] == "["
    assert result["skipped_assertions"] is True
    assert result["forbidden_matches"] == []


def test_duplicate_checkpoint_name_fails_without_overwriting_existing_range() -> None:
    session = FakeOutputSession()
    service = OutputAssertionService(session)
    session.append_output("boot\n")

    first = service.create_checkpoint("start").to_dict()
    session.append_output("ready\n")
    duplicate = service.create_checkpoint("start").to_dict()
    result = service.assert_since("start", required=["ready"]).to_dict()

    assert first["status"] == "PASS"
    assert duplicate["status"] == "FAIL"
    assert duplicate["reason"] == "output checkpoint already exists"
    assert result["status"] == "PASS"


def test_missing_checkpoint_and_trimmed_range_fail_with_named_reasons() -> None:
    missing_session = FakeOutputSession()
    missing = OutputAssertionService(missing_session).assert_since(
        "missing",
        required=["anything"],
    ).to_dict()
    assert missing["status"] == "FAIL"
    assert missing["reason"] == "output checkpoint not found"

    trimmed_session = FakeOutputSession(max_entries=2)
    trimmed_service = OutputAssertionService(trimmed_session)
    trimmed_session.append_output("first\n")
    trimmed_service.create_checkpoint("start")
    trimmed_session.append_output("second\n")
    trimmed_session.append_output("third\n")
    trimmed_session.append_output("fourth\n")

    trimmed = trimmed_service.assert_since("start", required=["fourth"]).to_dict()

    assert trimmed["status"] == "FAIL"
    assert trimmed["reason"] == "output checkpoint range trimmed"


@pytest.mark.asyncio
async def test_output_tools_return_standard_failures_for_missing_checkpoint() -> None:
    mcp = CapturingMCP()
    session = FakeOutputSession()
    register_runtime_smoke_tools(
        mcp=mcp,
        session=session,
        check_session_access=lambda ctx: None,
        resolve_project_root=_noop_resolve_project_root,
    )

    assert "output_checkpoint" in mcp.tools
    assert "output_assert_since" in mcp.tools

    response = await mcp.tools["output_assert_since"](
        ctx=None,
        checkpoint="missing",
        required=["ready"],
    )

    assert response["state"] == "stopped"
    assert response["data"]["status"] == "FAIL"
    assert response["data"]["reason"] == "output checkpoint not found"
