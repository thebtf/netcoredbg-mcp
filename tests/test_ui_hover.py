from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _valid_hover_evidence() -> dict[str, object]:
    return {
        "status": "PASS",
        "resolvedSelector": {
            "criterion": "automationId",
            "automationId": "hoverTrigger",
        },
        "target": {
            "automationId": "hoverTrigger",
            "name": "Hover trigger",
            "controlType": "Custom",
        },
        "matchCount": 1,
        "targetRootHwnd": 101,
        "targetProcessId": 42,
        "foregroundHwndBefore": 101,
        "foregroundHwndAfter": 101,
        "foregroundVerified": True,
        "focusBefore": {
            "automationId": "hoverFocusSentinel",
            "name": "Arm",
            "controlType": "Button",
        },
        "focusAfter": {
            "automationId": "hoverFocusSentinel",
            "name": "Arm",
            "controlType": "Button",
        },
        "focusUnchanged": True,
        "targetRect": {"x": 10, "y": 20, "width": 100, "height": 40},
        "requestedPoint": {"x": 60, "y": 40},
        "actualPointer": {"x": 60, "y": 40},
        "hitElement": {
            "automationId": "hoverTriggerText",
            "name": "Hover trigger",
            "controlType": "Text",
        },
        "hitRelation": "descendant",
        "underPointer": True,
        "hovered": True,
        "click": False,
        "button": "none",
        "timeoutMs": 1250,
        "elapsedMs": 12,
        "pointerMutationState": "moved",
    }


@pytest.mark.asyncio
async def test_flaui_hover_forwards_exact_operation_timeout_with_transport_slack() -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock(return_value=_valid_hover_evidence())
    backend._element_cache = {}
    backend._process_id = 42

    result = await backend.hover_element(
        automation_id="hoverTrigger",
        name="Hover trigger",
        control_type="Custom",
        root_id="hoverRegion",
        xpath="//Custom[@AutomationId='hoverTrigger']",
        timeout_ms=1250,
    )

    backend._client.call.assert_awaited_once_with(
        "hover",
        {
            "automationId": "hoverTrigger",
            "name": "Hover trigger",
            "controlType": "Custom",
            "rootAutomationId": "hoverRegion",
            "xpath": "//Custom[@AutomationId='hoverTrigger']",
            "timeoutMs": 1250,
        },
        timeout=2.25,
    )
    assert result["status"] == "PASS"
    assert result["hovered"] is True


@pytest.mark.parametrize("timeout_ms", [True, False, 0, 30001, 1.5, "5000"])
@pytest.mark.asyncio
async def test_flaui_hover_rejects_invalid_timeout_before_bridge_call(
    timeout_ms: object,
) -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock()
    backend._element_cache = {}
    backend._process_id = 42

    with pytest.raises(ValueError, match="timeout_ms must be an integer from 1 to 30000"):
        await backend.hover_element(
            automation_id="hoverTrigger",
            timeout_ms=timeout_ms,  # type: ignore[arg-type]
        )

    backend._client.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_flaui_hover_timeout_is_structured_with_unknown_mutation_state() -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock(side_effect=asyncio.TimeoutError())
    backend._element_cache = {}
    backend._process_id = 42

    result = await backend.hover_element(
        automation_id="hoverTrigger",
        root_id="hoverRegion",
        timeout_ms=750,
    )

    assert result["status"] == "BLOCKED"
    assert result["phase"] == "bridge_timeout"
    assert result["timeoutMs"] == 750
    assert result["pointerMutationState"] == "unknown"
    assert result["requested"] == {
        "selector": {"automation_id": "hoverTrigger", "root_id": "hoverRegion"},
        "timeout_ms": 750,
    }
    assert result["next_step"]


@pytest.mark.asyncio
async def test_flaui_hover_blocks_malformed_success_evidence() -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    malformed = _valid_hover_evidence()
    malformed.pop("hitRelation")
    malformed.pop("pointerMutationState")
    malformed["full_tree"] = {"should": "not leak"}
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock(return_value=malformed)
    backend._element_cache = {}
    backend._process_id = 42

    result = await backend.hover_element(automation_id="hoverTrigger", timeout_ms=1250)

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "hover backend returned malformed success evidence"
    assert "hitRelation" in result["missing"]
    assert "pointerMutationState" in result["missing"]
    assert "full_tree" not in repr(result)


@pytest.mark.parametrize("field", ["focusBefore", "focusAfter", "hitElement"])
def test_hover_validation_rejects_null_required_element_evidence(field: str) -> None:
    from netcoredbg_mcp.ui.hover import validate_hover_evidence

    evidence = _valid_hover_evidence()
    evidence[field] = None

    result = validate_hover_evidence(evidence)

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "hover backend returned malformed success evidence"
    assert field in result["malformed"]


def test_hover_validation_preserves_bounded_evidence_across_layers() -> None:
    from netcoredbg_mcp.ui.hover import validate_hover_evidence

    malformed = _valid_hover_evidence()
    malformed.pop("hitRelation")
    first = validate_hover_evidence(malformed)
    second = validate_hover_evidence(first)

    assert first["evidence"]
    assert second["evidence"] == first["evidence"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("matchCount", 2),
        ("foregroundVerified", False),
        ("focusUnchanged", False),
        ("underPointer", False),
        ("hovered", False),
        ("click", True),
        ("button", "left"),
        ("hitRelation", "unrelated"),
        ("foregroundHwndAfter", 999),
        ("pointerMutationState", "not_started"),
        ("timeoutMs", 0),
        ("elapsedMs", 1251),
    ],
)
@pytest.mark.asyncio
async def test_flaui_hover_fails_contradictory_success_evidence(
    field: str,
    value: object,
) -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    evidence = deepcopy(_valid_hover_evidence())
    evidence[field] = value
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock(return_value=evidence)
    backend._element_cache = {}
    backend._process_id = 42

    result = await backend.hover_element(automation_id="hoverTrigger", timeout_ms=1250)

    assert result["status"] == "FAIL"
    assert result["reason"] == "hover evidence contradicted the required contract"
    assert result["contradictions"]
    assert "runner_input" not in result


@pytest.mark.asyncio
async def test_flaui_hover_fails_changed_focus_identity_despite_true_flag() -> None:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    evidence = deepcopy(_valid_hover_evidence())
    evidence["focusAfter"] = {
        "automationId": "differentFocus",
        "name": "Other",
        "controlType": "Button",
    }
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = AsyncMock()
    backend._client.call = AsyncMock(return_value=evidence)
    backend._element_cache = {}
    backend._process_id = 42

    result = await backend.hover_element(automation_id="hoverTrigger", timeout_ms=1250)

    assert result["status"] == "FAIL"
    assert "focusBefore and focusAfter identities must match" in result["contradictions"]


@pytest.mark.asyncio
async def test_pywinauto_hover_returns_explicit_bounded_unsupported_evidence() -> None:
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    backend = PywinautoBackend.__new__(PywinautoBackend)
    backend._ui = SimpleNamespace(process_id=42, _element_cache={})

    result = await backend.hover_element(
        automation_id="hoverTrigger",
        root_id="hoverRegion",
        timeout_ms=5000,
    )

    assert result == {
        "status": "BLOCKED",
        "backend": "pywinauto",
        "reason": "selector-scoped pointer hover requires the FlaUI bridge backend",
        "capability": "selector-scoped pointer hover",
        "requested": {
            "selector": {"automation_id": "hoverTrigger", "root_id": "hoverRegion"},
            "timeout_ms": 5000,
        },
        "accepted": {"backend": "FlaUI", "capability": "selector-scoped pointer hover"},
        "next_step": "Install or build FlaUIBridge.exe and retry ui_hover.",
    }


@pytest.mark.asyncio
async def test_ui_hover_tool_returns_standard_envelope_and_forwards_exact_request(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(
        process_id=42,
        hover_element=AsyncMock(return_value=_valid_hover_evidence()),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(capturing_mcp, session, check_session_access=lambda ctx: None)
        response = await capturing_mcp.tools["ui_hover"](
            SimpleNamespace(),
            automation_id="hoverTrigger",
            name="Hover trigger",
            control_type="Custom",
            root_id="hoverRegion",
            xpath=None,
            timeout_ms=1250,
        )

    backend.hover_element.assert_awaited_once_with(
        automation_id="hoverTrigger",
        name="Hover trigger",
        control_type="Custom",
        root_id="hoverRegion",
        xpath=None,
        timeout_ms=1250,
    )
    assert response["state"] == DebugState.RUNNING.value
    assert response["data"]["status"] == "PASS"
    assert response["data"]["button"] == "none"


@pytest.mark.parametrize("timeout_ms", [True, 0, 30001, 2.5])
@pytest.mark.asyncio
async def test_ui_hover_tool_rejects_invalid_timeout_before_backend_use(
    capturing_mcp,
    timeout_ms: object,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(process_id=42, hover_element=AsyncMock())
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend) as create_backend:
        register_ui_tools(capturing_mcp, session, check_session_access=lambda ctx: None)
        response = await capturing_mcp.tools["ui_hover"](
            SimpleNamespace(),
            automation_id="hoverTrigger",
            timeout_ms=timeout_ms,
        )

    assert response["error"] == "timeout_ms must be an integer from 1 to 30000"
    create_backend.assert_not_called()
    backend.hover_element.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_hover_operation_adapter_forwards_selector_and_timeout() -> None:
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters

    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def hover_element(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return _valid_hover_evidence()

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.hover"](
        selector={
            "automation_id": "hoverTrigger",
            "root_id": "hoverRegion",
        },
        timeout_ms=1250,
    )

    assert result["status"] == "PASS"
    assert backend.calls == [
        {
            "automation_id": "hoverTrigger",
            "name": None,
            "control_type": None,
            "root_id": "hoverRegion",
            "xpath": None,
            "timeout_ms": 1250,
        }
    ]


@pytest.mark.asyncio
async def test_ui_set_focus_operation_adapter_uses_flat_bridge_selector() -> None:
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call(self, method: str, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append((method, payload))
            return {"status": "PASS", "focused": True}

    backend = SimpleNamespace(client=FakeClient())

    async def backend_provider() -> object:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.set_focus"](
        selector={"automation_id": "hoverFocusSentinel"},
    )

    assert result["status"] == "PASS"
    assert backend.client.calls == [("set_focus", {"automationId": "hoverFocusSentinel"})]
