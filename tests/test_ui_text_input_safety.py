"""Public text replacement must not fall back to unadmitted desktop input."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.ui_evidence import register_ui_evidence_tools


_INPUT_METHODS = frozenset(
    {
        "set_focus",
        "send_keys",
        "send_keys_focused",
        "send_keys_batch",
        "scoped_key_sequence",
        "hold_modifiers",
        "release_modifiers",
        "send_system_event",
        "click_at",
        "right_click_at",
        "double_click_at",
        "hover_element",
        "invoke_element",
        "drag",
        "drag_path",
    }
)


class RecordingBridgeClient:
    def __init__(self, backend: NoSafeTextSetterBackend) -> None:
        self.backend = backend

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method in _INPUT_METHODS:
            return self.backend.record_input(f"bridge.{method}", dict(payload))
        return {
            "status": "UNSUPPORTED",
            "unsupported": True,
            "reason": "The in-memory backend does not implement this bridge operation.",
        }


class NoSafeTextSetterBackend:
    """Read-only fixture state plus input recorders; no text setter capability.

    Input calls only append to a Python list. They never use a real backend,
    change fixture text, access a process, or send desktop input.
    """

    def __init__(self, *, found: bool = True) -> None:
        self.process_id = 42
        self.found = found
        self.input_calls: list[tuple[str, Any]] = []
        self.read_calls: list[tuple[str, Any]] = []
        self.client = RecordingBridgeClient(self)
        self.initial_text = "Original fixture text"

    def record_input(self, method: str, payload: Any) -> dict[str, Any]:
        self.input_calls.append((method, payload))
        return {"status": "PASS", "focused": True}

    def send_keys(self, keys: str) -> dict[str, Any]:
        return self.record_input("send_keys", keys)

    def __getattr__(self, name: str) -> Any:
        if name not in _INPUT_METHODS:
            raise AttributeError(name)

        async def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return self.record_input(name, {"args": args, "kwargs": kwargs})

        return record

    async def find_element(self, **selector: Any) -> dict[str, Any]:
        self.read_calls.append(("find_element", dict(selector)))
        return {"status": "PASS", "found": self.found}

    async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.read_calls.append(("textbox_state", dict(selector)))
        return {
            "status": "PASS",
            "text": self.initial_text,
            "selection": {
                "start": 0,
                "end": len(self.initial_text),
                "length": len(self.initial_text),
            },
            "focus_within": True,
            "source": "TextPattern",
        }

    async def extract_text(self, **selector: Any) -> dict[str, Any]:
        self.read_calls.append(("extract_text", dict(selector)))
        return {"status": "PASS", "text": self.initial_text, "source": "TextPattern"}


def assert_no_hidden_input(backend: NoSafeTextSetterBackend) -> None:
    assert backend.input_calls == [], f"Hidden desktop input attempted: {backend.input_calls!r}"


def assert_honest_refusal(response: dict[str, Any]) -> None:
    data = response["data"]
    assert data["status"] in {"BLOCKED", "UNSUPPORTED"}, response
    assert isinstance(data.get("reason"), str) and data["reason"].strip(), response
    assert data.get("verified") is not True, response


@pytest.fixture
def public_text_tool(capturing_mcp, monkeypatch) -> Callable[..., Any]:
    """Register the real public tool; replace only its external backend factory."""
    session = SimpleNamespace(
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        process_registry=None,
        cancel_pending_stealth_foreground_restore=AsyncMock(return_value=False),
        wait_for_pending_stealth_foreground_restore=AsyncMock(return_value=False),
    )

    def register(backend: NoSafeTextSetterBackend) -> Callable[..., Any]:
        monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
        register_ui_evidence_tools(
            mcp=capturing_mcp,
            session=session,
            check_session_access=lambda _ctx: None,
        )
        return capturing_mcp.tools["ui_text"]

    return register


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "Replacement text"], ids=["empty", "nonempty"])
@pytest.mark.parametrize(
    "selector",
    [
        {"automation_id": "CueTextBox", "control_type": "TextBox"},
        {"name": "Cue", "control_type": "TextBox", "root_id": "Editor"},
        {"xpath": "//TextBox[@AutomationId='CueTextBox']"},
    ],
    ids=["automation-id", "scoped-name", "xpath"],
)
async def test_public_set_text_without_safe_setter_refuses_before_input(
    public_text_tool,
    selector: dict[str, str],
    text: str,
) -> None:
    backend = NoSafeTextSetterBackend()
    ui_text = public_text_tool(backend)

    response = await ui_text(ctx=None, action="set_text", text=text, **selector)

    assert_no_hidden_input(backend)
    assert_honest_refusal(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "Replacement text"], ids=["empty", "nonempty"])
async def test_public_set_text_without_selector_rejects_before_backend(
    public_text_tool,
    text: str,
) -> None:
    backend = NoSafeTextSetterBackend()
    ui_text = public_text_tool(backend)

    response = await ui_text(ctx=None, action="set_text", text=text)

    assert_no_hidden_input(backend)
    assert backend.read_calls == []
    assert response["data"]["status"] == "FAIL"
    assert response["data"].get("reason")


@pytest.mark.asyncio
async def test_public_set_text_without_text_rejects_before_backend(public_text_tool) -> None:
    backend = NoSafeTextSetterBackend()
    ui_text = public_text_tool(backend)

    response = await ui_text(ctx=None, action="set_text", automation_id="CueTextBox")

    assert_no_hidden_input(backend)
    assert backend.read_calls == []
    assert response["data"]["status"] == "FAIL"
    assert response["data"].get("reason")


@pytest.mark.asyncio
async def test_public_set_text_selector_miss_never_attempts_input(public_text_tool) -> None:
    backend = NoSafeTextSetterBackend(found=False)
    ui_text = public_text_tool(backend)

    response = await ui_text(
        ctx=None,
        action="set_text",
        automation_id="MissingTextBox",
        text="Replacement text",
    )

    assert_no_hidden_input(backend)
    assert_honest_refusal(response)


@pytest.mark.parametrize("keys", ["^a", "Replacement text"], ids=["ctrl-a", "typing"])
def test_hidden_input_oracle_detects_ctrl_a_and_typing_control(keys: str) -> None:
    backend = NoSafeTextSetterBackend()

    backend.send_keys(keys)

    with pytest.raises(AssertionError, match="Hidden desktop input attempted"):
        assert_no_hidden_input(backend)
