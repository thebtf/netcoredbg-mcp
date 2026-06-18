"""Tests for new UI tools: ui_invoke, ui_toggle, ui_file_dialog, root_id, xpath."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestFlaUIBackendInvoke:
    """Tests for FlaUIBackend.invoke_element."""

    @pytest.mark.asyncio
    async def test_invoke_delegates_to_bridge(self):
        """invoke_element calls bridge with correct params."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(
            return_value={
                "invoked": True,
                "method": "InvokePattern",
                "automationId": "btn1",
                "name": "Save",
                "controlType": "Button",
            }
        )
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.invoke_element(automation_id="btn1")
        backend._client.call.assert_called_once_with(
            "invoke_element",
            {"automationId": "btn1"},
        )
        assert result["invoked"] is True
        assert result["method"] == "InvokePattern"

    @pytest.mark.asyncio
    async def test_invoke_with_root_id(self):
        """invoke_element passes rootAutomationId to bridge."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"invoked": True, "method": "Click"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.invoke_element(automation_id="btn", root_id="panel1")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "panel1"

    @pytest.mark.asyncio
    async def test_invoke_with_xpath(self):
        """invoke_element passes xpath to bridge."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"invoked": True, "method": "InvokePattern"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.invoke_element(xpath="//Button[@Name='Save']")
        args = backend._client.call.call_args
        assert args[0][1]["xpath"] == "//Button[@Name='Save']"

    @pytest.mark.asyncio
    async def test_ui_invoke_blocks_mismatched_exact_automation_id(self, capturing_mcp) -> None:
        from netcoredbg_mcp.session.manager import DebugState
        from netcoredbg_mcp.tools.ui import register_ui_tools

        backend = SimpleNamespace(
            process_id=42,
            invoke_element=AsyncMock(
                return_value={
                    "invoked": True,
                    "method": "InvokePattern",
                    "automationId": "buttonCharlistRemove",
                    "name": "Remove",
                    "controlType": "Button",
                }
            ),
        )
        session = SimpleNamespace(
            process_registry=None,
            state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
            stealth_mode=False,
        )

        with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
            register_ui_tools(
                capturing_mcp,
                session,
                check_session_access=lambda ctx: None,
            )
            response = await capturing_mcp.tools["ui_invoke"](
                SimpleNamespace(),
                automation_id="playButton",
                control_type="Button",
            )

        backend.invoke_element.assert_awaited_once_with(
            automation_id="playButton",
            name=None,
            control_type="Button",
            root_id=None,
            xpath=None,
        )
        assert response["data"]["status"] == "BLOCKED"
        assert response["data"]["reason"] == "selector result did not match exact automation_id"

    @pytest.mark.asyncio
    async def test_ui_invoke_maps_exact_miss_exception_to_blocked(self, capturing_mcp) -> None:
        from netcoredbg_mcp.session.manager import DebugState
        from netcoredbg_mcp.tools.ui import register_ui_tools

        backend = SimpleNamespace(
            process_id=42,
            invoke_element=AsyncMock(
                side_effect=RuntimeError(
                    "selector result did not match exact automation_id. "
                    "Requested automationId='playButton'. Search: automationId='playButton'"
                )
            ),
        )
        session = SimpleNamespace(
            process_registry=None,
            state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
            stealth_mode=False,
        )

        with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
            register_ui_tools(
                capturing_mcp,
                session,
                check_session_access=lambda ctx: None,
            )
            response = await capturing_mcp.tools["ui_invoke"](
                SimpleNamespace(),
                automation_id="playButton",
                control_type="Button",
                root_id="selectorSafetyPanel",
            )

        assert "error" not in response
        assert response["data"]["status"] == "BLOCKED"
        assert response["data"]["reason"] == "selector result did not match exact automation_id"
        assert response["data"]["requested"]["automationId"] == "playButton"
        assert response["data"]["requested"]["rootAutomationId"] == "selectorSafetyPanel"


@pytest.mark.asyncio
async def test_ui_click_annotated_uses_cached_screen_bounds_after_annotated_screenshot(
    capturing_mcp,
    monkeypatch,
) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    png = io.BytesIO()
    Image.new("RGB", (120, 80), (255, 255, 255)).save(png, format="PNG")

    backend = PywinautoBackend.__new__(PywinautoBackend)
    backend._ui = SimpleNamespace(process_id=42, _app=object())
    backend.click_at = AsyncMock()

    def fake_get_window_rect(_hwnd, rect_ptr):
        rect = rect_ptr._obj
        rect.left = 100
        rect.top = 200
        rect.right = 220
        rect.bottom = 280
        return True

    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid",
        lambda _pid: 555,
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (png.getvalue(), 120, 80),
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.collect_visible_elements",
        lambda _app, _max_depth, _interactive_only: [
            {
                "id": 7,
                "name": "Save",
                "type": "Button",
                "automationId": "saveButton",
                "bounds": {"x": 110, "y": 220, "width": 40, "height": 20},
            }
        ],
    )
    monkeypatch.setattr(
        "ctypes.windll",
        SimpleNamespace(user32=SimpleNamespace(GetWindowRect=fake_get_window_rect)),
        raising=False,
    )

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id=None,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        await capturing_mcp.tools["ui_take_annotated_screenshot"](SimpleNamespace())
        response = await capturing_mcp.tools["ui_click_annotated"](
            SimpleNamespace(),
            element_id=7,
        )

    backend.click_at.assert_awaited_once_with(130, 230)
    assert response["data"]["clicked"] is True
    assert response["data"]["position"] == {"x": 130, "y": 230}


def _pywinauto_backend_with_element(element: MagicMock) -> tuple[object, SimpleNamespace]:
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    backend = PywinautoBackend.__new__(PywinautoBackend)
    inner = SimpleNamespace(
        process_id=42,
        _element_cache={},
        find_element=AsyncMock(return_value=element),
        get_element_info=AsyncMock(return_value=element.element_info),
        _executor=None,
        click=AsyncMock(),
        _right_click_at_coords=AsyncMock(),
        _double_click_at_coords=AsyncMock(),
    )
    backend._ui = inner
    return backend, inner


def _mismatched_pywinauto_element() -> MagicMock:
    element = MagicMock()
    element.element_info = SimpleNamespace(
        automation_id="buttonCharlistRemove",
        name="Remove",
        control_type="Button",
    )
    return element


def _blank_id_pywinauto_element() -> MagicMock:
    element = MagicMock()
    element.element_info = SimpleNamespace(
        automation_id="",
        name="Remove",
        control_type="Button",
    )
    return element


@pytest.mark.asyncio
async def test_ui_click_blocks_mismatched_exact_automation_id_before_side_effect(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _mismatched_pywinauto_element()
    backend, inner = _pywinauto_backend_with_element(element)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
        )

    inner.click.assert_not_awaited()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_click"
    assert response["data"]["candidate"]["automationId"] == "buttonCharlistRemove"


@pytest.mark.asyncio
async def test_ui_click_blocks_blank_candidate_automation_id_before_side_effect(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _blank_id_pywinauto_element()
    backend, inner = _pywinauto_backend_with_element(element)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
        )

    inner.click.assert_not_awaited()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["candidate"]["automationId"] == ""


@pytest.mark.asyncio
async def test_ui_click_with_secondary_selector_uses_guard_before_flaui_click(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    backend.find_element = AsyncMock(
        return_value={
            "automationId": "buttonCharlistRemove",
            "name": "Remove",
            "controlType": "Button",
            "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
        }
    )
    backend.click_at = AsyncMock()
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
            root_id="selectorSafetyPanel",
        )

    backend._client.call.assert_not_awaited()
    backend.click_at.assert_not_awaited()
    backend.find_element.assert_awaited_once_with(
        automation_id="playButton",
        name=None,
        control_type="Button",
        root_id="selectorSafetyPanel",
        xpath=None,
    )
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_click"


@pytest.mark.asyncio
async def test_ui_click_with_secondary_selector_skips_cache_before_guard(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {
        "playButton": {"rect": {"left": 10, "right": 30, "top": 20, "bottom": 40}}
    }
    backend._client = AsyncMock()
    backend.find_element = AsyncMock(
        return_value={
            "automationId": "buttonCharlistRemove",
            "name": "Remove",
            "controlType": "Button",
            "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
        }
    )
    backend.click_at = AsyncMock()
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
        )

    backend._client.call.assert_not_awaited()
    backend.click_at.assert_not_awaited()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["candidate"]["automationId"] == "buttonCharlistRemove"


@pytest.mark.asyncio
async def test_ui_right_click_uses_flaui_backend_for_dict_elements(capturing_mcp) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(
        process_id=42,
        element_cache={},
        find_element=AsyncMock(
            return_value={
                "automationId": "dataGrid",
                "controlType": "DataGrid",
                "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
            }
        ),
        right_click_at=AsyncMock(),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_right_click"](
            SimpleNamespace(),
            automation_id="dataGrid",
        )

    assert "error" not in response
    backend.right_click_at.assert_awaited_once_with(60, 50)
    assert response["data"]["right_clicked"] is True


@pytest.mark.asyncio
async def test_ui_right_click_blocks_mismatched_exact_automation_id_before_side_effect(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _mismatched_pywinauto_element()
    backend, _ = _pywinauto_backend_with_element(element)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_right_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
        )

    element.click_input.assert_not_called()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_right_click"
    assert response["data"]["candidate"]["automationId"] == "buttonCharlistRemove"


@pytest.mark.asyncio
async def test_ui_right_click_with_secondary_selector_skips_cache_before_guard(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _mismatched_pywinauto_element()
    backend, inner = _pywinauto_backend_with_element(element)
    inner._element_cache["playButton"] = {
        "rect": {"left": 10, "right": 30, "top": 20, "bottom": 40}
    }
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_right_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
            root_id="selectorSafetyPanel",
        )

    inner._right_click_at_coords.assert_not_awaited()
    element.click_input.assert_not_called()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_right_click"


@pytest.mark.asyncio
async def test_ui_double_click_uses_flaui_backend_for_dict_elements(capturing_mcp) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(
        process_id=42,
        element_cache={},
        find_element=AsyncMock(
            return_value={
                "automationId": "dataGrid",
                "controlType": "DataGrid",
                "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
            }
        ),
        double_click_at=AsyncMock(),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_double_click"](
            SimpleNamespace(),
            automation_id="dataGrid",
        )

    assert "error" not in response
    backend.double_click_at.assert_awaited_once_with(60, 50)
    assert response["data"]["double_clicked"] is True


@pytest.mark.asyncio
async def test_ui_double_click_blocks_mismatched_exact_automation_id_before_side_effect(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _mismatched_pywinauto_element()
    backend, _ = _pywinauto_backend_with_element(element)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_double_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
        )

    element.double_click_input.assert_not_called()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_double_click"
    assert response["data"]["candidate"]["automationId"] == "buttonCharlistRemove"


@pytest.mark.asyncio
async def test_ui_double_click_with_secondary_selector_skips_cache_before_guard(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    element = _mismatched_pywinauto_element()
    backend, inner = _pywinauto_backend_with_element(element)
    inner._element_cache["playButton"] = {
        "rect": {"left": 10, "right": 30, "top": 20, "bottom": 40}
    }
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_double_click"](
            SimpleNamespace(),
            automation_id="playButton",
            control_type="Button",
            root_id="selectorSafetyPanel",
        )

    inner._double_click_at_coords.assert_not_awaited()
    element.double_click_input.assert_not_called()
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["action"] == "ui_double_click"


@pytest.mark.asyncio
async def test_ui_select_items_uses_flaui_backend_multi_select_evidence(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    backend.multi_select = AsyncMock(
        return_value={
            "selected": 2,
            "indices": [0, 2],
            "mode": "replace",
            "method": "SelectionItemPattern",
        }
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_select_items"](
            SimpleNamespace(),
            automation_id="fixtureList",
            indices=[0, 2],
            mode="replace",
        )

    backend.multi_select.assert_awaited_once_with("fixtureList", [0, 2], mode="replace")
    assert "error" not in response
    assert response["data"]["selected"] == 2
    assert response["data"]["indices"] == [0, 2]
    assert response["data"]["mode"] == "replace"
    assert response["data"]["method"] == "SelectionItemPattern"


@pytest.mark.asyncio
async def test_ui_select_items_flaui_add_mode_passes_mode_to_backend(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    backend.multi_select = AsyncMock(
        return_value={
            "selected": 1,
            "indices": [3],
            "mode": "add",
            "method": "SelectionItemPattern",
        }
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_select_items"](
            SimpleNamespace(),
            automation_id="fixtureList",
            indices=[3],
            mode="add",
        )

    backend.multi_select.assert_awaited_once_with("fixtureList", [3], mode="add")
    assert response["data"]["mode"] == "add"
    assert response["data"]["selected"] == 1


@pytest.mark.asyncio
async def test_ui_select_items_normalizes_bool_selected_count_from_flaui_evidence(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    backend._last_multi_select_result = {
        "selected": True,
        "selected_count": True,
        "indices": [0],
        "method": "SelectionItemPattern",
    }
    backend.multi_select = AsyncMock(return_value=1)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_select_items"](
            SimpleNamespace(),
            automation_id="fixtureList",
            indices=[0],
            mode="replace",
        )

    assert type(response["data"]["selected"]) is int
    assert response["data"]["selected"] == 1


@pytest.mark.asyncio
async def test_ui_get_selected_item_uses_flaui_backend_selected_item_evidence(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    backend.get_selected_item = AsyncMock(
        return_value={
            "index": 1,
            "name": "Beta",
            "automationId": "Item_1",
            "controlType": "ListItem",
            "selected": True,
        }
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_get_selected_item"]("fixtureList")

    backend.get_selected_item.assert_awaited_once_with(
        automation_id="fixtureList",
        root_id=None,
        xpath=None,
    )
    assert "error" not in response
    assert "warning" not in response["data"]
    assert response["data"]["index"] == 1
    assert response["data"]["name"] == "Beta"
    assert response["data"]["automationId"] == "Item_1"
    assert response["data"]["controlType"] == "ListItem"


@pytest.mark.asyncio
async def test_ui_get_focused_element_flaui_without_evidence_points_to_focus_assert(
    capturing_mcp,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._element_cache = {}
    backend._client = AsyncMock()
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda ctx: None,
        )
        response = await capturing_mcp.tools["ui_get_focused_element"]()

    assert "error" not in response
    assert response["data"]["status"] == "UNSUPPORTED"
    assert "ui_focus" in response["data"]["guidance"]
    assert "assert" in response["data"]["guidance"]
    assert response["data"]["name"] is None
    assert response["data"]["automationId"] is None
    assert response["data"]["controlType"] is None


class TestFlaUIBackendToggle:
    """Tests for FlaUIBackend.toggle_element."""

    @pytest.mark.asyncio
    async def test_toggle_returns_new_state(self):
        """toggle_element returns toggled state."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(
            return_value={
                "toggled": True,
                "newState": "On",
                "automationId": "chk1",
                "name": "Enabled",
                "controlType": "CheckBox",
            }
        )
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.toggle_element(automation_id="chk1")
        assert result["toggled"] is True
        assert result["newState"] == "On"

    @pytest.mark.asyncio
    async def test_toggle_with_root_id(self):
        """toggle_element passes rootAutomationId."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"toggled": True, "newState": "Off"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.toggle_element(name="Debug", root_id="settingsPanel")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "settingsPanel"

    @pytest.mark.asyncio
    async def test_ui_toggle_blocks_mismatched_exact_automation_id(
        self,
        capturing_mcp,
    ) -> None:
        from netcoredbg_mcp.session.manager import DebugState
        from netcoredbg_mcp.tools.ui import register_ui_tools

        backend = SimpleNamespace(
            process_id=42,
            toggle_element=AsyncMock(
                return_value={
                    "toggled": True,
                    "newState": "On",
                    "automationId": "buttonCharlistRemove",
                    "name": "Remove",
                    "controlType": "Button",
                }
            ),
        )
        session = SimpleNamespace(
            process_registry=None,
            state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
            stealth_mode=False,
        )

        with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
            register_ui_tools(
                capturing_mcp,
                session,
                check_session_access=lambda ctx: None,
            )
            response = await capturing_mcp.tools["ui_toggle"](
                SimpleNamespace(),
                automation_id="playButton",
                control_type="Button",
            )

        backend.toggle_element.assert_awaited_once_with(
            automation_id="playButton",
            name=None,
            control_type="Button",
            root_id=None,
            xpath=None,
        )
        assert response["data"]["status"] == "BLOCKED"
        assert response["data"]["action"] == "ui_toggle"


class TestFlaUIBackendXPath:
    """Tests for FlaUIBackend.find_by_xpath."""

    @pytest.mark.asyncio
    async def test_find_by_xpath_returns_element(self):
        """find_by_xpath returns element info with matchCount."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(
            return_value={
                "found": True,
                "automationId": "btn1",
                "name": "Save",
                "controlType": "Button",
                "matchCount": 1,
            }
        )
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.find_by_xpath("//Button[@Name='Save']")
        backend._client.call.assert_called_once_with(
            "find_by_xpath",
            {"xpath": "//Button[@Name='Save']"},
        )
        assert result["found"] is True
        assert result["matchCount"] == 1

    @pytest.mark.asyncio
    async def test_find_by_xpath_with_root_id(self):
        """find_by_xpath scopes search via rootAutomationId."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"found": True, "matchCount": 1})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.find_by_xpath("//Edit", root_id="form1")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "form1"


class TestPywinautoBackendInvoke:
    """Tests for PywinautoBackend invoke/toggle/xpath."""

    @pytest.mark.asyncio
    async def test_invoke_calls_iface_invoke(self):
        """invoke_element uses iface_invoke when available."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = MagicMock()
        mock_element.iface_invoke.Invoke = MagicMock()
        mock_element.element_info.automation_id = "btn1"
        mock_element.element_info.name = "Save"
        mock_element.element_info.control_type = "Button"

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="btn1",
                name="Save",
                control_type="Button",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="btn1")
        assert result["invoked"] is True
        assert result["method"] == "InvokePattern"

    @pytest.mark.asyncio
    async def test_invoke_falls_back_to_click(self):
        """invoke_element falls back to click when iface_invoke is None."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = None
        mock_element.click = MagicMock()

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="div1",
                name="Panel",
                control_type="Pane",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="div1")
        assert result["method"] == "Click"

    @pytest.mark.asyncio
    async def test_invoke_blocks_mismatched_exact_automation_id_before_side_effect(self):
        """invoke_element does not invoke/click when exact id resolves to another element."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = MagicMock()
        mock_element.iface_invoke.Invoke = MagicMock()
        mock_element.click = MagicMock()

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="buttonCharlistRemove",
                name="Remove",
                control_type="Button",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="playButton")

        mock_element.iface_invoke.Invoke.assert_not_called()
        mock_element.click.assert_not_called()
        assert result["invoked"] is False
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "selector result did not match exact automation_id"
        assert result["automationId"] == "buttonCharlistRemove"

    @pytest.mark.asyncio
    async def test_invoke_blocks_blank_candidate_automation_id_before_side_effect(self):
        """invoke_element treats a blank candidate id as unsafe for an exact id request."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = MagicMock()
        mock_element.iface_invoke.Invoke = MagicMock()
        mock_element.click = MagicMock()

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="",
                name="Remove",
                control_type="Button",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="playButton")

        mock_element.iface_invoke.Invoke.assert_not_called()
        mock_element.click.assert_not_called()
        assert result["invoked"] is False
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "selector result did not match exact automation_id"
        assert result["automationId"] == ""

    @pytest.mark.asyncio
    async def test_toggle_returns_state(self):
        """toggle_element returns On/Off/Indeterminate."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_toggle = MagicMock()
        mock_toggle.Toggle = MagicMock()
        mock_toggle.CurrentToggleState = 1  # On
        mock_element.iface_toggle = mock_toggle

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="chk1",
                name="Enabled",
                control_type="CheckBox",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.toggle_element(automation_id="chk1")
        assert result["toggled"] is True
        assert result["newState"] == "On"

    @pytest.mark.asyncio
    async def test_toggle_blocks_mismatched_exact_automation_id_before_side_effect(self):
        """toggle_element does not toggle when exact id resolves to another element."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_toggle = MagicMock()
        mock_toggle.Toggle = MagicMock()
        mock_toggle.CurrentToggleState = 1
        mock_element.iface_toggle = mock_toggle

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="buttonCharlistRemove",
                name="Remove",
                control_type="Button",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.toggle_element(automation_id="playButton")

        mock_toggle.Toggle.assert_not_called()
        assert result["toggled"] is False
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "selector result did not match exact automation_id"
        assert result["automationId"] == "buttonCharlistRemove"

    @pytest.mark.asyncio
    async def test_toggle_blocks_blank_candidate_automation_id_before_side_effect(self):
        """toggle_element treats a blank candidate id as unsafe for an exact id request."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_toggle = MagicMock()
        mock_toggle.Toggle = MagicMock()
        mock_toggle.CurrentToggleState = 1
        mock_element.iface_toggle = mock_toggle

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="",
                name="Remove",
                control_type="Button",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.toggle_element(automation_id="playButton")

        mock_toggle.Toggle.assert_not_called()
        assert result["toggled"] is False
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "selector result did not match exact automation_id"
        assert result["automationId"] == ""

    @pytest.mark.asyncio
    async def test_toggle_no_pattern_raises(self):
        """toggle_element raises when element has no TogglePattern."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_toggle = None
        mock_element.element_info.control_type = "TextBox"

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(
            return_value=MagicMock(
                automation_id="txt1",
                name="Text",
                control_type="TextBox",
            )
        )
        mock_ui._executor = None
        backend._ui = mock_ui

        with pytest.raises(RuntimeError, match="TogglePattern"):
            await backend.toggle_element(automation_id="txt1")

    @pytest.mark.asyncio
    async def test_find_by_xpath_raises_not_implemented(self):
        """find_by_xpath on pywinauto raises NotImplementedError."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        backend._ui = MagicMock()

        with pytest.raises(NotImplementedError, match="FlaUI backend"):
            await backend.find_by_xpath("//Button")

    @pytest.mark.asyncio
    async def test_find_element_xpath_only_raises(self):
        """find_element with only xpath on pywinauto raises."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        backend._ui = MagicMock()

        with pytest.raises(NotImplementedError, match="FlaUI backend"):
            await backend.find_element(xpath="//Button")


class TestBackendProtocol:
    """Tests for UIBackend protocol conformance."""

    def test_flaui_backend_has_new_methods(self):
        """FlaUIBackend implements invoke/toggle/find_by_xpath."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        assert hasattr(FlaUIBackend, "invoke_element")
        assert hasattr(FlaUIBackend, "toggle_element")
        assert hasattr(FlaUIBackend, "find_by_xpath")

    def test_pywinauto_backend_has_new_methods(self):
        """PywinautoBackend implements invoke/toggle/find_by_xpath."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        assert hasattr(PywinautoBackend, "invoke_element")
        assert hasattr(PywinautoBackend, "toggle_element")
        assert hasattr(PywinautoBackend, "find_by_xpath")

    def test_find_element_accepts_root_id_xpath(self):
        """find_element signature includes root_id and xpath params."""
        import inspect

        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        sig = inspect.signature(FlaUIBackend.find_element)
        params = list(sig.parameters.keys())
        assert "root_id" in params
        assert "xpath" in params

    def test_build_search_params(self):
        """_build_search_params converts snake_case to camelCase."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        params = FlaUIBackend._build_search_params(
            automation_id="btn1",
            root_id="panel1",
            xpath="//Button",
        )
        assert params == {
            "automationId": "btn1",
            "rootAutomationId": "panel1",
            "xpath": "//Button",
        }

    def test_build_search_params_omits_none(self):
        """_build_search_params skips None values."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        params = FlaUIBackend._build_search_params(automation_id="btn1")
        assert params == {"automationId": "btn1"}
        assert "rootAutomationId" not in params
        assert "xpath" not in params
