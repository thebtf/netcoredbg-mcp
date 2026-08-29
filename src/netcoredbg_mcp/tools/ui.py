"""UI automation tools."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, cast

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.state import DebugState

logger = logging.getLogger(__name__)

_SUPPORTED_MODIFIERS = {"ctrl", "shift", "alt", "win"}
_SUPPORTED_SYSTEM_EVENTS = {"theme_change"}
_SUPPORTED_THEME_MODES = {"toggle", "light", "dark"}
UI_TREE_DISCOVERY_TIMEOUT_SECONDS = 10.0
BRIDGE_NOT_CONNECTED_DIAGNOSTIC = "Not connected. Call 'connect' first."
BRIDGE_RAW_PNG_INVALID_DIAGNOSTIC = "Bridge screenshot raw PNG is invalid"


class _PhysicalCaptureProvenanceUnavailableError(ValueError):
    """The strict target cannot be compared to a trustworthy lossless capture."""


def _is_bridge_not_connected_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and BRIDGE_NOT_CONNECTED_DIAGNOSTIC in str(error)


def _normalize_modifier_list(modifiers: list[str] | None) -> list[str]:
    """Validate and normalize modifier names."""
    if modifiers is None:
        return []

    normalized: list[str] = []
    invalid: list[str] = []

    for modifier in modifiers:
        if not isinstance(modifier, str):
            invalid.append(str(modifier))
            continue
        value = modifier.strip().lower()
        if value not in _SUPPORTED_MODIFIERS:
            invalid.append(modifier)
            continue
        if value not in normalized:
            normalized.append(value)

    if invalid:
        accepted = ", ".join(sorted(_SUPPORTED_MODIFIERS))
        invalid_names = ", ".join(sorted(str(value) for value in invalid))
        raise ValueError(f"Unknown modifier names: {invalid_names}. Accepted values: {accepted}")

    return normalized


def _normalize_release_modifiers(modifiers: list[str] | str) -> list[str] | str:
    """Validate release_modifiers input."""
    if isinstance(modifiers, str):
        value = modifiers.strip().lower()
        if value != "all":
            raise ValueError(
                "release_modifiers expects either the string 'all' or a list of modifier names"
            )
        return "all"

    return _normalize_modifier_list(modifiers)


def register_ui_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register UI automation tools on the MCP server."""
    from mcp.types import ToolAnnotations

    # Lazy-loaded UI backend instance (FlaUI or pywinauto)
    _backend_holder: dict[str, Any] = {"instance": None}

    # Cache for last annotated screenshot (used by ui_click_annotated)
    _last_annotation: dict[str, Any] | None = None
    _annotation_generation: int = 0

    def _get_backend() -> Any:
        """Get or create UI backend (FlaUI preferred, pywinauto fallback)."""
        if _backend_holder["instance"] is None:
            from ..ui.backend import create_backend

            _backend_holder["instance"] = create_backend(
                process_registry=session.process_registry,
            )
        return _backend_holder["instance"]

    async def _ensure_ui_connected(
        *,
        restore_joined: bool = False,
        observation: bool = False,
    ) -> Any:
        """Ensure UI backend is connected to the debug process.

        Raises:
            NoActiveSessionError: If no debug session is active
            NoProcessIdError: If process ID not available
        """
        from ..ui import NoActiveSessionError, NoProcessIdError

        if session.state.state == DebugState.IDLE:
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        backend = _get_backend()
        if backend.process_id != process_id:
            await _reconnect_ui_backend(backend, process_id)

        if not restore_joined and await _join_launch_foreground_restore(observation=observation):
            await _reconnect_ui_backend(backend, process_id)

        return backend

    async def _join_launch_foreground_restore(*, observation: bool = False) -> bool:
        method_name = (
            "wait_for_pending_stealth_foreground_restore"
            if observation
            else "cancel_pending_stealth_foreground_restore"
        )
        join = getattr(session, method_name, None)
        if join is None:
            return False
        return (await cast(Callable[[], Awaitable[Any]], join)()) is True

    def _capture_route_diagnostics(
        *,
        bridge_screenshot: dict[str, Any] | None,
        capture_metadata: dict[str, Any] | None,
        frame_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = bridge_screenshot if bridge_screenshot is not None else capture_metadata
        diagnostics: dict[str, Any] = {
            "producer": "flaui_bridge" if bridge_screenshot is not None else "python_win32",
        }
        if not isinstance(source, dict):
            return diagnostics

        method = source.get("method")
        if isinstance(method, str):
            diagnostics["capture_method"] = method
        for key in (
            "hwnd",
            "process_id",
            "dpi",
            "client_rect",
            "window_bounds",
            "flags",
            "fallback",
            "fallback_reason",
            "authority",
            "capture_authority",
            "source_api",
            "rop",
            "alternate_attempts",
            "foreground",
        ):
            if key in source:
                diagnostics[key] = source[key]

        primary_analysis = source.get("printwindow_analysis")
        if isinstance(primary_analysis, dict):
            diagnostics["primary_analysis"] = primary_analysis
        elif isinstance(source.get("printwindow_classification"), str):
            diagnostics["primary_analysis"] = {
                "classification": source["printwindow_classification"],
                "variance": source.get("printwindow_variance"),
            }
        if frame_analysis is not None:
            diagnostics["fallback_analysis" if method == "BitBlt" else "raster_analysis"] = (
                frame_analysis
            )
        return diagnostics

    def _capture_provenance_unavailable_response(
        message: str,
        *,
        bridge_screenshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = build_error_response(message, state=session.state.state)
        response["code"] = "CAPTURE_PROVENANCE_UNAVAILABLE"
        response["data"] = {
            "capture_diagnostics": _capture_route_diagnostics(
                bridge_screenshot=bridge_screenshot,
                capture_metadata=None,
            )
        }
        return response

    def _probable_black_frame_response(
        frame_analysis: dict[str, Any],
        *,
        retry_tool: str,
        foreground_mutation_attempted: bool,
        capture_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        next_step = f"Call ui_bring_to_front explicitly, then retry {retry_tool}."
        response = build_error_response(
            "Captured frame is near-uniform black and cannot be trusted as visual evidence",
            state=session.state.state,
            next_actions=["ui_bring_to_front", retry_tool, "get_debug_state", "stop_debug"],
        )
        response["classification"] = "PROBABLE_BLACK_FRAME"
        response["data"] = {
            "frame_analysis": frame_analysis,
            "foreground_mutation_attempted": foreground_mutation_attempted,
            "next_step": next_step,
        }
        if capture_diagnostics is not None:
            response["data"]["capture_diagnostics"] = capture_diagnostics
        return response

    async def _reconnect_ui_backend(backend: Any, process_id: int) -> None:
        cache = getattr(backend, "element_cache", None)
        if isinstance(cache, dict):
            cache.clear()

        from ..ui.backend import connect_backend

        await connect_backend(
            backend,
            process_id,
            stealth_mode=getattr(session, "stealth_mode", False),
        )

    def _is_flaui_backend(ui_inst: Any) -> bool:
        from ..ui.flaui_client import FlaUIBackend

        return isinstance(ui_inst, FlaUIBackend)

    def _has_live_flaui_connection(ui_inst: Any, process_id: int) -> bool:
        return _is_flaui_backend(ui_inst) and ui_inst.process_id == process_id

    def _selection_items_payload(
        result: Any,
        *,
        indices: list[int],
        mode: str,
        default_method: str,
    ) -> dict[str, Any]:
        if isinstance(result, dict):
            payload = dict(result)
            selected_indices = payload.get("indices")
            if not isinstance(selected_indices, list):
                selected_indices = list(indices)
            selected_value = payload.get("selected")
            if isinstance(selected_value, bool):
                selected_count = payload.get("selected_count")
                if type(selected_count) is not int:
                    selected_count = len(selected_indices) if selected_value else 0
            elif type(selected_value) is int:
                selected_count = selected_value
            else:
                selected_count = payload.get("selected_count")
                if type(selected_count) is not int:
                    selected_count = len(selected_indices)
            payload["selected"] = selected_count
            payload["indices"] = selected_indices
            payload.setdefault("mode", mode)
            payload.setdefault("method", default_method)
            return payload

        selected_count = int(result)
        return {
            "selected": selected_count,
            "indices": list(indices),
            "mode": mode,
            "method": default_method,
        }

    def _flaui_focused_element_guidance() -> dict[str, Any]:
        return {
            "status": "UNSUPPORTED",
            "reason": "bounded focused-element query is not available via FlaUI legacy tool",
            "guidance": (
                'Use ui_focus(action="assert", automation_id=..., name=..., '
                "control_type=...) for bounded FlaUI focus evidence."
            ),
            "name": None,
            "automationId": None,
            "controlType": None,
            "value": None,
        }

    async def _read_window_tree(ui: Any, max_depth: int, max_children: int) -> Any:
        return await asyncio.wait_for(
            ui.get_window_tree(max_depth, max_children),
            timeout=UI_TREE_DISCOVERY_TIMEOUT_SECONDS,
        )

    def _ui_tree_timeout_payload(error: BaseException, *, session: SessionManager, ui: Any) -> dict:
        backend_name = type(ui).__name__ if ui is not None else "unknown"
        return {
            "status": "BLOCKED",
            "reason": "ui tree discovery timed out",
            "timeout_seconds": UI_TREE_DISCOVERY_TIMEOUT_SECONDS,
            "debuggee_process_id": getattr(session.state, "process_id", None),
            "ui_backend": backend_name,
            "error": str(error) or type(error).__name__,
            "next_step": (
                "Use a narrower selector with ui_wait_for/ui_find_element, lower max_depth "
                "or max_children, or inspect the debuggee UI thread responsiveness before "
                "retrying ui_get_window_tree."
            ),
        }

    def _element_identity_payload(result: Any) -> dict[str, Any] | None:
        if isinstance(result, dict):
            automation_id = (
                result["automationId"] if "automationId" in result else result.get("automation_id")
            )
            return {
                "automationId": automation_id,
                "name": result.get("name"),
                "controlType": result.get("controlType") or result.get("control_type"),
            }

        element_info = getattr(result, "element_info", None)
        if element_info is None:
            return None

        automation_id = getattr(element_info, "automation_id", None)
        if automation_id is None:
            automation_id = getattr(element_info, "automationId", None)
        control_type = getattr(element_info, "control_type", None)
        if control_type is None:
            control_type = getattr(element_info, "controlType", None)
        return {
            "automationId": automation_id,
            "name": getattr(element_info, "name", None),
            "controlType": control_type,
        }

    def _exact_automation_id_mismatch_payload(
        *,
        action: str,
        requested_automation_id: str | None,
        result: Any,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any] | None:
        if not requested_automation_id:
            return None

        candidate = _element_identity_payload(result)
        if candidate is None:
            return None

        candidate_automation_id = candidate.get("automationId")
        if candidate_automation_id == requested_automation_id:
            return None

        payload = {
            "status": "BLOCKED",
            "reason": "selector result did not match exact automation_id",
            "action": action,
            "requested": {
                "automationId": requested_automation_id,
                "name": name,
                "controlType": control_type,
                "rootAutomationId": root_id,
                "xpath": xpath,
            },
            "candidate": candidate,
            "accepted": {
                "selector_policy": "exact automation_id match",
            },
            "next_step": (
                "Inspect the scoped tree with ui_get_window_tree or adjust the selector; "
                "side-effecting UI actions require the returned element to match the "
                "requested exact automation_id."
            ),
        }
        if isinstance(result, dict):
            payload["backend_result"] = result
        return payload

    def _exact_automation_id_exception_payload(
        *,
        action: str,
        requested_automation_id: str | None,
        error: Exception,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any] | None:
        if not requested_automation_id:
            return None
        message = str(error)
        if "selector result did not match exact automation_id" not in message:
            return None
        return {
            "status": "BLOCKED",
            "reason": "selector result did not match exact automation_id",
            "action": action,
            "requested": {
                "automationId": requested_automation_id,
                "name": name,
                "controlType": control_type,
                "rootAutomationId": root_id,
                "xpath": xpath,
            },
            "accepted": {
                "selector_policy": "exact automation_id match",
            },
            "next_step": (
                "Inspect the scoped tree with ui_get_window_tree or adjust the selector; "
                "side-effecting UI actions require the returned element to match the "
                "requested exact automation_id."
            ),
            "backend_error": message,
        }

    def _has_secondary_selector_constraints(
        *,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> bool:
        return any((name, control_type, root_id, xpath))

    def _stealth_response_mode(result: Any) -> str | None:
        if not getattr(session, "stealth_mode", False):
            return None
        if isinstance(result, dict) and (
            result.get("fallback") == "flash-focus"
            or result.get("method") == "flash-focus"
            or "flash_ms" in result
        ):
            return "flash-focus"
        return "stealth"

    def _is_verified_typed_bitblt_fallback(
        result: dict[str, Any],
        process_id: int,
        hwnd: int,
        client_rect: dict[str, Any],
        window_bounds: dict[str, Any],
        dpi: int,
    ) -> bool:
        def contains_nonfinite_number(value: Any) -> bool:
            if type(value) is float:
                return not math.isfinite(value)
            if isinstance(value, dict):
                return any(contains_nonfinite_number(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_nonfinite_number(item) for item in value)
            return False

        if contains_nonfinite_number(result):
            return False
        if (
            result.get("method") != "BitBlt"
            or result.get("fallback") != "flash-focus"
            or result.get("fallback_reason") != "probable_black_printwindow"
            or result.get("authority") != "foreground_window_gdi_raster"
            or result.get("capture_authority") != "foreground_window_gdi_raster"
            or result.get("source_api") != "GetWindowDC"
            or result.get("rop") != "SRCCOPY"
            or result.get("evidence_grade") != "typed_bitblt_fallback"
            or result.get("printwindow_classification") != "probable_black_discarded"
            or not isinstance(result.get("printwindow_analysis"), dict)
            or result["printwindow_analysis"].get("classification") != "PROBABLE_BLACK_FRAME"
            or type(result.get("printwindow_variance")) not in (int, float)
            or type(result.get("alternate_attempts")) is not int
            or result["alternate_attempts"] != 1
            or type(result.get("process_id")) is not int
            or result["process_id"] != process_id
        ):
            return False

        bridge_assembly = result.get("bridge_assembly")
        if (
            not isinstance(bridge_assembly, dict)
            or not isinstance(bridge_assembly.get("path"), str)
            or not bridge_assembly["path"]
            or not isinstance(bridge_assembly.get("sha256"), str)
            or len(bridge_assembly["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in bridge_assembly["sha256"])
        ):
            return False

        stability = result.get("capture_stability")
        if not isinstance(stability, dict):
            return False
        before = stability.get("before")
        after = stability.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False

        def matches_snapshot(snapshot: dict[str, Any]) -> bool:
            return (
                type(snapshot.get("hwnd")) is int
                and snapshot["hwnd"] == hwnd
                and type(snapshot.get("process_id")) is int
                and snapshot["process_id"] == process_id
                and snapshot.get("client_rect") == client_rect
                and snapshot.get("window_bounds") == window_bounds
                and type(snapshot.get("dpi")) is int
                and snapshot["dpi"] == dpi
            )

        if not matches_snapshot(before) or not matches_snapshot(after) or before != after:
            return False

        foreground = result.get("foreground")
        if not isinstance(foreground, dict):
            return False
        activation = foreground.get("activation")
        restoration = foreground.get("restoration")
        if not isinstance(activation, dict) or not isinstance(restoration, dict):
            return False
        if (
            activation.get("attempted") is not True
            or activation.get("set_foreground_returned") is not True
            or activation.get("verified") is not True
            or type(activation.get("foreground_hwnd")) is not int
            or activation["foreground_hwnd"] != hwnd
            or type(restoration.get("required")) is not bool
            or restoration.get("attempted") is not restoration["required"]
            or restoration.get("verified") is not True
            or type(restoration.get("set_foreground_returned")) is not bool
            or type(restoration.get("foreground_hwnd")) is not int
        ):
            return False
        if restoration["required"]:
            return (
                restoration["set_foreground_returned"]
                and restoration["foreground_hwnd"] != 0
                and type(restoration.get("process_id")) is int
                and restoration["process_id"] > 0
            )
        return not restoration["set_foreground_returned"] and restoration["foreground_hwnd"] == 0

    async def _find_ui_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ):
        """Helper to connect and find element with ambiguity detection.

        Returns (backend, element, ambiguity_info) where:
        - element is a pywinauto wrapper or FlaUI dict
        - ambiguity_info is None (single match) or dict with candidateCount + warning

        When searching by name/controlType (not automationId/xpath), uses
        find_all_cascade to detect multiple matches and returns the best-ranked one.
        The top-ranked element from find_all_cascade is used for the actual selection,
        not just for ambiguity reporting.
        """
        ui = await _ensure_ui_connected(observation=False)
        from ..ui.pywinauto_backend import PywinautoBackend

        if isinstance(ui, PywinautoBackend):
            element = await ui._find_element_scoped(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
            )
            return ui, element, None

        # FlaUI backend: use find_all_cascade when searching by name/controlType
        # so we select the best-ranked element, not simply the first match found.
        ambiguity_info = None
        if not automation_id and not xpath and (name or control_type):
            try:
                ranked = await ui.find_all_cascade(
                    name=name,
                    control_type=control_type,
                    root_id=root_id,
                    max_results=5,
                )
                results = ranked.get("results", [])
                total = ranked.get("totalMatches", 0)
                if results:
                    # Use the top-ranked result as the selected element
                    top = results[0]
                    top_automation_id = top.get("automationId") or None

                    if total > 1:
                        ambiguity_info = {
                            "ambiguous": True,
                            "candidateCount": total,
                            "warning": (
                                f"Multiple matches ({total}) for search criteria. "
                                "Using best-ranked result."
                            ),
                            "alternatives": [
                                {
                                    "automationId": r.get("automationId", ""),
                                    "name": r.get("name", ""),
                                    "controlType": r.get("controlType", ""),
                                    "parentDesc": r.get("parentDesc", ""),
                                }
                                for r in results[1:4]  # Show up to 3 alternatives
                            ],
                        }

                    # Resolve the top-ranked element: prefer its automationId for
                    # a precise lookup; fall back to original criteria if no id.
                    if top_automation_id:
                        element = await ui.find_element(
                            automation_id=top_automation_id,
                            root_id=root_id,
                        )
                    else:
                        element = await ui.find_element(
                            name=top.get("name") or name,
                            control_type=top.get("controlType") or control_type,
                            root_id=root_id,
                        )

                    if ambiguity_info and isinstance(element, dict):
                        element.update(ambiguity_info)
                    return ui, element, ambiguity_info
            except Exception:
                pass  # Fall through to normal find_element

        element = await ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
            root_id=root_id,
            xpath=xpath,
        )

        return ui, element, ambiguity_info

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_get_window_tree(max_depth: int = 3, max_children: int = 50) -> dict:
        """
        Get the visual tree of the debugged application — ALL top-level windows.

        Covers the main app window and any sibling windows (modal dialogs,
        popups, file pickers) owned by the same process. Modal dialogs
        created via WPF Window.ShowDialog() are sibling top-level windows,
        not descendants of the main window — they appear in the "windows"
        array alongside the main window.

        Call after start_debug and wait for the application window to appear.

        Args:
            max_depth: Maximum depth to traverse within each window (default 3)
            max_children: Maximum children per element (default 50)

        Returns:
            FlaUI backend: {"windows": [tree, ...], "count": N, "primary": "Main App"}
            Each tree entry carries automationId, controlType, name, rect,
            children, etc. Use ui_switch_window to retarget subsequent calls
            at a specific window (e.g. a modal dialog).

            pywinauto fallback backend: a single-window tree dict with
            automationId/name/rect/children at the root (no windows array,
            no count, no primary). Callers that need to support both
            backends should probe for the "windows" key and fall back to
            treating the response itself as a single-window tree.
        """
        try:
            ui = await _ensure_ui_connected(observation=True)
            try:
                tree = await _read_window_tree(ui, max_depth, max_children)
            except RuntimeError as e:
                if not _is_bridge_not_connected_error(e):
                    raise

                process_id = session.state.process_id
                if not process_id:
                    raise

                await _reconnect_ui_backend(ui, process_id)
                try:
                    tree = await _read_window_tree(ui, max_depth, max_children)
                except (asyncio.TimeoutError, TimeoutError) as retry_timeout:
                    return build_response(
                        data=_ui_tree_timeout_payload(retry_timeout, session=session, ui=ui),
                        state=session.state.state,
                    )
            except (asyncio.TimeoutError, TimeoutError) as e:
                return build_response(
                    data=_ui_tree_timeout_payload(e, session=session, ui=ui),
                    state=session.state.state,
                )
            # Backend returns dict directly (both FlaUI and pywinauto)
            data = tree if isinstance(tree, dict) else tree.to_dict()
            return build_response(data=data, state=session.state.state)
        except Exception as e:
            from ..ui import UIOperationTimeoutError

            if isinstance(e, UIOperationTimeoutError):
                return build_response(
                    data=_ui_tree_timeout_payload(e, session=session, ui=locals().get("ui")),
                    state=session.state.state,
                )
            return build_error_response(str(e) or type(e).__name__, state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_bring_to_front(ctx: Context) -> dict:
        """Bring the debuggee window to the foreground and exit stealth mode."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            bring_to_front = getattr(ui, "bring_to_front", None)
            if bring_to_front is None:
                import ctypes

                from ..ui.foreground import restore_foreground_window
                from ..ui.screenshot import get_hwnd_for_pid

                pid = session.state.process_id
                hwnd = get_hwnd_for_pid(pid) if pid else None
                if not hwnd:
                    return build_error_response(
                        f"No visible window for process {pid}.",
                        state=session.state.state,
                    )

                loop = asyncio.get_running_loop()

                def activate_window() -> bool:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    return restore_foreground_window(hwnd)

                activated = await loop.run_in_executor(None, activate_window)
                result = {"activated": activated, "hwnd": hwnd}
            else:
                result = await bring_to_front()
            if not isinstance(result, dict):
                return build_error_response(
                    f"bring_to_front: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )

            if result.get("activated") is True:
                session.stealth_mode = False
                result["stealth_mode"] = False

            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def ui_switch_window(
        ctx: Context,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict:
        """
        Retarget the UI backend at a different top-level window of the same process.

        Use this to enter modal dialogs, file pickers, or popups that appear
        as sibling windows of the app's main window. After switching, all
        subsequent ui_* calls (find_element, click, send_keys, etc.) operate
        inside the new window's subtree.

        Typical flow:
          1. ui_get_window_tree() → inspect "windows" array
          2. ui_switch_window(name="Create collection") → enter dialog
          3. ui_find_element(control_type="Edit") → locate dialog TextBox
          4. ui_send_keys_batch(keys=["Characters", "{ENTER}"]) → type + submit
          5. After dialog closes, ui_switch_window(name="Main App Title")
             to return to the original window

        Requires the FlaUI bridge backend; the pywinauto fallback raises
        NotImplementedError. At least one of name or automation_id must be
        provided; automation_id is matched first.

        Args:
            name: Window title (e.g., the dialog's Title/Name property)
            automation_id: Window's AutomationId property (if any)

        Returns:
            {"switched": True, "title": "...", "automationId": "..."} on success
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if not name and not automation_id:
                return build_error_response(
                    "ui_switch_window requires at least one of: name, automation_id",
                    state=session.state.state,
                )

            ui = await _ensure_ui_connected()
            result = await ui.switch_window(name=name, automation_id=automation_id)
            if not isinstance(result, dict):
                return build_error_response(
                    f"switch_window: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "switch_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_find_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Find a UI element by AutomationId, name, control type, or XPath.

        At least one search criterion must be provided.
        Use ui_get_window_tree first to discover available elements.

        Args:
            automation_id: AutomationId property (most reliable for WPF)
            name: Element's Name/Title property
            control_type: Type like "Button", "TextBox", "MenuItem"
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)

        Returns:
            Element info if found
        """
        try:
            ui = await _ensure_ui_connected(observation=True)
            result = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            # Backend returns dict directly (both FlaUI and pywinauto)
            data = result if isinstance(result, dict) else result.to_dict()
            return build_response(data=data, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def ui_set_focus(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Set keyboard focus to a UI element.

        Uses UIA-based Focus() for FlaUI backend (monitor/DPI-agnostic),
        or element search + set_focus for pywinauto backend.

        Call this before ui_send_keys to ensure keys go to the right element.

        Args:
            automation_id: AutomationId property (FlaUI + pywinauto)
            name: Element's Name/Title property (FlaUI + pywinauto)
            control_type: Control type (pywinauto only)
            root_id: Optional AutomationId to scope search (pywinauto only)
            xpath: Optional XPath expression (pywinauto only)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # Use UIA-based focus via bridge (DPI/monitor-agnostic)
            from ..ui.flaui_client import FlaUIBackend

            if isinstance(ui, FlaUIBackend):
                params: dict = {}
                if automation_id:
                    params["automationId"] = automation_id
                if name:
                    params["name"] = name
                result = await ui.client.call("set_focus", params)
                return build_response(
                    data=result
                    if isinstance(result, dict)
                    else {"focused": True, "method": "UIA.Focus"},
                    state=session.state.state,
                )

            # Pywinauto fallback: find element and set focus
            from ..ui.pywinauto_backend import PywinautoBackend

            if isinstance(ui, PywinautoBackend):
                _, element, _ = await _find_ui_element(
                    automation_id, name, control_type, root_id, xpath
                )
                await ui.inner.set_focus(element)
                return build_response(
                    data={"focused": True, "method": "pywinauto"},
                    state=session.state.state,
                )

            return build_error_response("No UI backend available", state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys(
        ctx: Context,
        keys: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Send keyboard input to a UI element.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Tries cached coordinates first (click to focus, then send keys),
        then falls back to pywinauto element search.

        Key syntax (modifiers are PREFIX characters, special keys in braces):
        - Regular text: "hello world"
        - Modifiers: ^ = Ctrl, % = Alt, + = Shift
        - Alt+Z: "%z"    Alt+F4: "%{F4}"
        - Ctrl+C: "^c"   Ctrl+Shift+S: "^+s"
        - Shift+Tab: "+{TAB}"
        - Special keys: {ENTER} {TAB} {ESC} {DELETE} {BACKSPACE}
        - Arrow keys: {LEFT} {RIGHT} {UP} {DOWN}
        - Navigation: {HOME} {END} {PGUP} {PGDN}
        - Function keys: {F1} {F2} ... {F12}
        - Combined: Ctrl+End = "^{END}", Alt+Z = "%z"

        IMPORTANT: Modifier prefixes (^%+) apply to the NEXT character or {KEY}.
        For Alt+Z send "%z" (NOT "{ALT}z" or "Alt+Z").

        Args:
            keys: Keys to send (see syntax above)
            automation_id: Target element's AutomationId
            name: Target element's Name
            control_type: Target element's control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # FlaUI backend: route through bridge send_keys (handles
            # SetForegroundWindow + optional UIA element focus)
            from ..ui.flaui_client import FlaUIBackend

            if isinstance(ui, FlaUIBackend):
                params: dict = {"keys": keys}
                if automation_id:
                    params["automationId"] = automation_id
                result = await ui.client.call("send_keys", params)
                mode = _stealth_response_mode(result)
                data = {
                    "sent": keys,
                    "method": "bridge",
                    **(result if isinstance(result, dict) else {}),
                }
                if mode:
                    data["mode"] = mode
                return build_response(
                    data=data,
                    state=session.state.state,
                )

            # Pywinauto fallback: find element, then send keys
            from ..ui.pywinauto_backend import PywinautoBackend

            if isinstance(ui, PywinautoBackend):
                if automation_id or name or control_type:
                    _, element, _ = await _find_ui_element(
                        automation_id, name, control_type, root_id, xpath
                    )
                    await ui.inner.send_keys(element, keys)
                else:
                    await ui.send_keys(keys)
            else:
                await ui.send_keys(keys)
            return build_response(
                data={"sent": keys, "method": "element_search"}, state=session.state.state
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys_focused(ctx: Context, keys: str) -> dict:
        """
        Send keyboard input to the currently focused element.

        Use this AFTER ui_set_focus to avoid re-searching for complex elements
        like DataGrid that may timeout on repeated searches.

        Workflow:
        1. ui_set_focus(automation_id="MyElement")  # Focus the element
        2. ui_send_keys_focused(keys="^{END}")      # Send keys without re-search

        Key syntax (modifiers are PREFIX characters, special keys in braces):
        - ^ = Ctrl, % = Alt, + = Shift
        - Alt+Z: "%z"    Ctrl+C: "^c"    Shift+Tab: "+{TAB}"
        - Special: {ENTER} {TAB} {ESC} {DELETE} {BACKSPACE}
        - Arrows: {LEFT} {RIGHT} {UP} {DOWN}
        - Navigation: {HOME} {END} {PGUP} {PGDN}
        - Combined: Ctrl+End = "^{END}", Ctrl+Home = "^{HOME}"

        IMPORTANT: For Alt+Z send "%z" (NOT "{ALT}z").

        Args:
            keys: Keys to send (see syntax above)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # FlaUI backend: route through bridge (handles SetForegroundWindow)
            from ..ui.flaui_client import FlaUIBackend

            if isinstance(ui, FlaUIBackend):
                await ui.client.call("send_keys", {"keys": keys})
                return build_response(
                    data={"sent": keys, "target": "focused", "method": "bridge"},
                    state=session.state.state,
                )

            await ui.send_keys(keys)
            return build_response(
                data={"sent": keys, "target": "focused"},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys_batch(
        ctx: Context,
        keys: list[str],
        automation_id: str | None = None,
        delay_ms: int = 50,
    ) -> dict:
        """
        Send a batch of key sequences in a single call, holding focus throughout.

        Solves the race condition where the terminal steals focus between
        individual send_keys calls. The bridge holds foreground + element focus
        for the entire batch, sending keys with configurable delay.

        Use this for: arrow navigation (20x DOWN), typing sequences, keyboard shortcuts.

        Args:
            keys: List of key strings, each sent separately with delay.
                  Example: ["{DOWN}", "{DOWN}", "{DOWN}"] for 3 arrow presses.
            automation_id: Target element to focus before sending.
            delay_ms: Milliseconds between each key (default 50ms).
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            from ..ui.flaui_client import FlaUIBackend

            if isinstance(ui, FlaUIBackend):
                params: dict = {"keys": keys, "delay_ms": delay_ms}
                if automation_id:
                    params["automationId"] = automation_id
                result = await ui.client.call("send_keys_batch", params)
                return build_response(
                    data={
                        "sent": True,
                        "count": len(keys),
                        "method": "bridge_batch",
                        **(result if isinstance(result, dict) else {}),
                    },
                    state=session.state.state,
                )

            # Pywinauto fallback: send one by one (no focus hold guarantee)
            import asyncio

            for key in keys:
                await ui.send_keys(key)
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
            return build_response(
                data={"sent": True, "count": len(keys), "method": "pywinauto_sequential"},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Click on a UI element.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Tries cached coordinates first (from last ui_get_window_tree call),
        then falls back to pywinauto element search.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # FlaUI backend: use bridge click (automationId → InvokePattern,
            # coords → SetForegroundWindow + Mouse.Click)
            from ..ui.flaui_client import FlaUIBackend

            if isinstance(ui, FlaUIBackend):
                params: dict = {}
                if automation_id and not _has_secondary_selector_constraints(
                    name=name,
                    control_type=control_type,
                    root_id=root_id,
                    xpath=xpath,
                ):
                    params["automationId"] = automation_id
                elif automation_id is None and name is None:
                    # Coordinate click from cache
                    pass
                if not params and not _has_secondary_selector_constraints(
                    name=name,
                    control_type=control_type,
                    root_id=root_id,
                    xpath=xpath,
                ):
                    # Try cache coordinates
                    if automation_id:
                        rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                        if rect:
                            params["x"] = (rect["left"] + rect["right"]) // 2
                            params["y"] = (rect["top"] + rect["bottom"]) // 2
                if params:
                    result = await ui.client.call("click", params)
                    mode = _stealth_response_mode(result)
                    data = {
                        "clicked": True,
                        "method": "bridge",
                        **(result if isinstance(result, dict) else {}),
                    }
                    if mode:
                        data["mode"] = mode
                    return build_response(
                        data=data,
                        state=session.state.state,
                    )

            # Fallback to element search
            try:
                ui, element, _ = await _find_ui_element(
                    automation_id, name, control_type, root_id, xpath
                )
                mismatch = _exact_automation_id_mismatch_payload(
                    action="ui_click",
                    requested_automation_id=automation_id,
                    result=element,
                    name=name,
                    control_type=control_type,
                    root_id=root_id,
                    xpath=xpath,
                )
                if mismatch is not None:
                    return build_response(data=mismatch, state=session.state.state)
                from ..ui.pywinauto_backend import PywinautoBackend

                if isinstance(ui, PywinautoBackend):
                    await ui.inner.click(element)
                else:
                    # FlaUI: element is dict with rect
                    rect = element.get("rect", {}) if isinstance(element, dict) else {}
                    if rect:
                        cx = int(rect.get("x", 0) + rect.get("width", 0) / 2)
                        cy = int(rect.get("y", 0) + rect.get("height", 0) / 2)
                        await ui.click_at(cx, cy)
                return build_response(
                    data={"clicked": True, "method": "element_search"}, state=session.state.state
                )
            except Exception:
                # Last resort: if element found but click fails
                # (for example, DataGrid has no click wrapper),
                # try coordinate click from element's bounding rectangle
                if automation_id:
                    ui = await _ensure_ui_connected()
                    if not _has_secondary_selector_constraints(
                        name=name,
                        control_type=control_type,
                        root_id=root_id,
                        xpath=xpath,
                    ):
                        rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                    else:
                        rect = None
                    if rect:
                        cx = (rect["left"] + rect["right"]) // 2
                        cy = (rect["top"] + rect["bottom"]) // 2
                        await ui.click_at(cx, cy)
                        return build_response(
                            data={
                                "clicked": True,
                                "method": "coord_fallback",
                                "position": {"x": cx, "y": cy},
                            },
                            state=session.state.state,
                        )
                raise  # re-raise if no fallback possible
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_hover(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        timeout_ms: int = 5000,
    ) -> dict:
        """Move the real pointer over one uniquely resolved foreground UI element."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if getattr(session, "stealth_mode", False):
                return build_error_response(
                    "ui_hover is unavailable in stealth mode because it moves the real "
                    "pointer; call ui_bring_to_front to exit stealth mode first",
                    state=session.state.state,
                )

            from ..ui.hover import validate_hover_timeout

            timeout_ms = validate_hover_timeout(timeout_ms)
            ui = await _ensure_ui_connected()
            result = await ui.hover_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
                timeout_ms=timeout_ms,
            )
            if not isinstance(result, dict):
                return build_error_response(
                    f"hover: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_invoke(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Invoke a UI element using UIA InvokePattern (no mouse movement).

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Preferred over ui_click for buttons, menu items, and hyperlinks because
        it works reliably even when the element is off-screen or partially obscured.
        Falls back to Click() if InvokePattern is not supported.

        WPF top-level MenuItem headers: scope the native ENTER key to the parent,
        then rediscover the popup child and call ui_invoke separately for that exact
        child. Do not add a fixture-specific key handler. Invoking the parent alone
        does not guarantee submenu peers materialize. Use pre/post UI oracles; a
        child missing after verified expansion is a harness observation, not
        automatically a product defect.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type (Button, MenuItem, Hyperlink, etc.)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.invoke_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            mismatch = _exact_automation_id_mismatch_payload(
                action="ui_invoke",
                requested_automation_id=automation_id,
                result=result,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            mismatch = _exact_automation_id_exception_payload(
                action="ui_invoke",
                requested_automation_id=automation_id,
                error=e,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_toggle(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Toggle a CheckBox or ToggleButton using UIA TogglePattern.

        Returns the new toggle state after the operation: "On", "Off", or
        "Indeterminate". Use this instead of ui_click for checkboxes to get
        reliable state feedback.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type (CheckBox, ToggleButton, etc.)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.toggle_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            mismatch = _exact_automation_id_mismatch_payload(
                action="ui_toggle",
                requested_automation_id=automation_id,
                result=result,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            mismatch = _exact_automation_id_exception_payload(
                action="ui_toggle",
                requested_automation_id=automation_id,
                error=e,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)
            return build_error_response(str(e), state=session.state.state)

    def _escape_sendkeys_path(path: str) -> str:
        """Escape special SendKeys characters in file paths."""
        # Characters with special meaning in SendKeys: + ^ % { } ( ) ~
        result = []
        for ch in path:
            if ch in "+^%{}()~":
                result.append("{")
                result.append(ch)
                result.append("}")
            else:
                result.append(ch)
        return "".join(result)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_file_dialog(
        ctx: Context,
        path: str,
        accept_button: str = "Open",
    ) -> dict:
        """
        Complete a standard Windows Open/Save file dialog in a single call.

        Enters the file path and clicks the accept button. Handles the standard
        Win32 dialog layout (File name ComboBox + Open/Save button) with
        multi-strategy fallback for different dialog variants.

        Args:
            path: Full file path to enter (e.g. "C:/data/test.txt")
            accept_button: Name of accept button (default "Open", use "Save" for save dialogs)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # Strategy 1: Find file name field by standard automationId "1148"
            edit_method = ""
            try:
                combo = await ui.find_element(automation_id="1148")
                if combo.get("found"):
                    # Set value via the ComboBox (bridge handles ValuePattern)
                    from ..ui.flaui_client import FlaUIBackend

                    if isinstance(ui, FlaUIBackend):
                        await ui.client.call(
                            "set_value",
                            {
                                "automationId": "1148",
                                "value": path,
                            },
                        )
                        edit_method = "set_value(id=1148)"
                    else:
                        # pywinauto fallback: type the path
                        await ui.send_keys(f"^a{_escape_sendkeys_path(path)}")
                        edit_method = "keyboard(Ctrl+A, type)"
            except Exception as exc:
                logger.debug("file_dialog strategy 1 (set_value) failed: %s", exc)

            # Strategy 2: Fallback — keyboard navigation
            if not edit_method:
                try:
                    # Standard dialog: Alt+N focuses the file name field
                    await ui.send_keys("%n")
                    await asyncio.sleep(0.2)
                    await ui.send_keys(f"^a{_escape_sendkeys_path(path)}")
                    edit_method = "keyboard(Alt+N, Ctrl+A, type)"
                except Exception as e:
                    return build_error_response(
                        f"Could not enter file path. This may not be a standard file dialog: {e}",
                        state=session.state.state,
                    )

            # Find and click the accept button
            button_method = ""
            try:
                # Strategy 1: Standard dialog accept button has automationId "1"
                result = await ui.invoke_element(automation_id="1")
                if result.get("invoked"):
                    button_method = "invoke(id=1)"
            except Exception:
                pass

            if not button_method:
                try:
                    # Strategy 2: Find button by name
                    result = await ui.invoke_element(name=accept_button, control_type="Button")
                    if result.get("invoked"):
                        button_method = f"invoke(name={accept_button})"
                except Exception:
                    pass

            if not button_method:
                try:
                    # Strategy 3: Press Enter as last resort
                    await ui.send_keys("{ENTER}")
                    button_method = "keyboard(Enter)"
                except Exception as e:
                    return build_error_response(
                        "File path entered via "
                        f"{edit_method} but could not click accept button: {e}",
                        state=session.state.state,
                    )

            return build_response(
                data={
                    "completed": True,
                    "path": path,
                    "editMethod": edit_method,
                    "buttonMethod": button_method,
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click_at(ctx: Context, x: int, y: int) -> dict:
        """Click at absolute screen coordinates.

        Use with ui_get_window_tree rectangle data when element search fails.
        Get coordinates from the 'rectangle' field in tree output.
        Click goes to the center: x = (left + right) / 2, y = (top + bottom) / 2

        Args:
            x: Screen X coordinate
            y: Screen Y coordinate
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            await ui.click_at(x, y)
            return build_response(
                data={"clicked": True, "position": {"x": x, "y": y}},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Screenshot & annotation tools --

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_take_screenshot(
        ctx: Context,
        max_width: int = 1568,
        format: str = "webp",
        evidence: bool = False,
        crop_x: int | None = None,
        crop_y: int | None = None,
        crop_width: int | None = None,
        crop_height: int | None = None,
        expected_hwnd: int | None = None,
        expected_physical_width: int | None = None,
        expected_physical_height: int | None = None,
    ) -> Any:
        """Take a screenshot of the debugged application's window.

        Returns inline ImageContent (WebP at max_width resolution) directly
        to your vision pipeline, plus TextContent with metadata and HD file path.

        Set evidence to retain a raw PNG for the active debug session. A probable-black
        PrintWindow raster may make one independently verified BitBlt attempt through
        the FlaUI bridge. Its explicit ``typed_bitblt_fallback`` evidence grade is a
        distinct capture authority; strict physical assertions separately validate the
        caller-supplied target. Crop coordinates require evidence mode and are applied
        to the accepted raw PNG.

        Args:
            max_width: Maximum image width. Default 1280; max useful is 1568.
            format: Image format: "webp" (smallest), "jpeg", "png"
            evidence: Persist the raw PNG as session-scoped evidence.
            crop_x: Raw-image crop origin X; requires all crop arguments.
            crop_y: Raw-image crop origin Y; requires all crop arguments.
            crop_width: Raw-image crop width; requires all crop arguments.
            crop_height: Raw-image crop height; requires all crop arguments.
            expected_hwnd: Require this HWND and physical raster size before persisting
                evidence.
            expected_physical_width: Required raw raster width in physical pixels with
                expected_hwnd.
            expected_physical_height: Required raw raster height in physical pixels with
                expected_hwnd.
        """

        valid_formats = {"webp", "jpeg", "png"}
        crop_values = (crop_x, crop_y, crop_width, crop_height)
        crop_requested = any(value is not None for value in crop_values)
        crop_rect: tuple[int, int, int, int] | None = None
        strict_values = (expected_hwnd, expected_physical_width, expected_physical_height)
        strict_target_requested = any(value is not None for value in strict_values)
        strict_target: dict[str, int | str] | None = None

        try:
            import base64
            import hashlib
            import io
            import json
            import time as _time
            import uuid

            from mcp.types import ImageContent, TextContent

            from ..ui.screenshot import (
                _process_screenshot,
                analyze_screenshot_frame,
                build_capture_metadata,
                capture_window,
                capture_window_evidence,
                create_preview,
                crop_png,
                get_hwnd_for_pid,
            )

            if crop_requested and not evidence:
                raise ValueError("crop arguments require evidence=True")
            if crop_requested and not all(value is not None for value in crop_values):
                raise ValueError(
                    "crop_x, crop_y, crop_width, and crop_height must be supplied together"
                )
            if crop_requested:
                crop_rect = cast(tuple[int, int, int, int], crop_values)

            if strict_target_requested:
                if not evidence:
                    raise ValueError("physical target assertions require evidence=True")
                if not all(value is not None for value in strict_values):
                    raise ValueError(
                        "expected_hwnd, expected_physical_width, and "
                        "expected_physical_height must be supplied together"
                    )
                strict_hwnd = cast(int, expected_hwnd)
                strict_width = cast(int, expected_physical_width)
                strict_height = cast(int, expected_physical_height)
                for name, value in (
                    ("expected_hwnd", strict_hwnd),
                    ("expected_physical_width", strict_width),
                    ("expected_physical_height", strict_height),
                ):
                    if type(value) is not int:
                        raise ValueError(f"{name} must be an integer")
                if strict_hwnd == 0:
                    raise ValueError("expected_hwnd must be non-zero")
                for name, value in (
                    ("expected_physical_width", strict_width),
                    ("expected_physical_height", strict_height),
                ):
                    if value <= 0:
                        raise ValueError(f"{name} must be positive, got {value}")
                strict_target = {
                    "hwnd": strict_hwnd,
                    "width": strict_width,
                    "height": strict_height,
                    "unit": "physical_px",
                    "coordinate_space": "raw_raster",
                }

            # Validate format against allow-list
            safe_format = format if format in valid_formats else "webp"
            bridge_screenshot: dict[str, Any] | None = None
            capture_metadata: dict[str, Any] | None = None
            foreground_mutation_attempted = False
            typed_bitblt_fallback = False
            png_bytes = b""
            raw_width = 0
            raw_height = 0
            persist_evidence = evidence
            physical_target: dict[str, Any] | None = None
            target_comparability: dict[str, Any] | None = (
                {"status": "UNASSERTED"} if evidence else None
            )
            pid = session.state.process_id
            if not pid:
                return build_error_response(
                    "No debug process. Start debugging first.", state=session.state.state
                )

            sid = getattr(session, "session_id", None)
            temp_manager = getattr(session, "temp_manager", None)
            if evidence and (not sid or temp_manager is None):
                raise ValueError(
                    "Evidence capture requires an active session with temporary storage"
                )

            loop = asyncio.get_running_loop()
            restore_joined = False

            bridge_capture_requested = strict_target_requested or getattr(
                session, "stealth_mode", False
            )
            if evidence and not bridge_capture_requested:
                bridge_capture_requested = _has_live_flaui_connection(
                    _backend_holder["instance"], pid
                )
            if bridge_capture_requested:
                ui = await _ensure_ui_connected(restore_joined=True)
                from ..ui.flaui_client import FlaUIBackend

                if not isinstance(ui, FlaUIBackend):
                    if strict_target_requested:
                        raise _PhysicalCaptureProvenanceUnavailableError(
                            "Physical target assertions require FlaUI bridge lossless capture"
                        )
                else:
                    bridge_request: dict[str, bool | int] = {"evidence": True} if evidence else {}
                    if strict_target_requested:
                        strict_target_values = cast(dict[str, int | str], strict_target)
                        bridge_request.update(
                            {
                                "typed_bitblt_fallback": True,
                                "expected_hwnd": cast(int, strict_target_values["hwnd"]),
                                "expected_physical_width": cast(int, strict_target_values["width"]),
                                "expected_physical_height": cast(
                                    int, strict_target_values["height"]
                                ),
                                "expected_process_id": pid,
                            }
                        )

                    restore_was_pending = await _join_launch_foreground_restore()
                    restore_joined = True
                    if restore_was_pending:
                        await _reconnect_ui_backend(ui, pid)
                    try:
                        bridge_result = await ui.client.call("screenshot", bridge_request)
                    except RuntimeError as error:
                        if strict_target_requested:
                            raise _PhysicalCaptureProvenanceUnavailableError(
                                "Physical target assertions require successful bridge capture: "
                                f"{error}"
                            ) from error
                        raise
                    foreground_mutation_attempted = (
                        _stealth_response_mode(bridge_result) == "flash-focus"
                    )
                    if not isinstance(bridge_result, dict) or "base64" not in bridge_result:
                        if strict_target_requested:
                            raise _PhysicalCaptureProvenanceUnavailableError(
                                "Physical target assertions require valid bridge "
                                "screenshot provenance"
                            )
                        if evidence:
                            return _capture_provenance_unavailable_response(
                                "Evidence capture requires valid bridge screenshot provenance",
                                bridge_screenshot=(
                                    bridge_result if isinstance(bridge_result, dict) else None
                                ),
                            )
                        logger.warning(
                            "screenshot: bridge returned invalid screenshot response; "
                            "falling back to HWND capture"
                        )
                    else:
                        bridge_screenshot = bridge_result
                        try:
                            png_bytes = base64.b64decode(bridge_result["base64"], validate=evidence)
                        except Exception as error:
                            if strict_target_requested:
                                raise _PhysicalCaptureProvenanceUnavailableError(
                                    BRIDGE_RAW_PNG_INVALID_DIAGNOSTIC
                                ) from error
                            if evidence:
                                return _capture_provenance_unavailable_response(
                                    BRIDGE_RAW_PNG_INVALID_DIAGNOSTIC,
                                    bridge_screenshot=bridge_screenshot,
                                )
                            raise
                        bridge_width = bridge_result.get("width")
                        bridge_height = bridge_result.get("height")
                        if (
                            type(bridge_width) is not int
                            or type(bridge_height) is not int
                            or bridge_width <= 0
                            or bridge_height <= 0
                        ):
                            if strict_target_requested:
                                raise _PhysicalCaptureProvenanceUnavailableError(
                                    "Bridge screenshot requires positive integer dimensions"
                                )
                            if evidence:
                                return _capture_provenance_unavailable_response(
                                    "Bridge screenshot response requires positive integer width and height",
                                    bridge_screenshot=bridge_screenshot,
                                )
                            raise ValueError(
                                "Bridge screenshot response requires positive integer "
                                "width and height"
                            )
                        raw_width = bridge_width
                        raw_height = bridge_height
                        if evidence:
                            method = bridge_result.get("method")
                            bridge_hwnd = bridge_result.get("hwnd")
                            bridge_client_rect = bridge_result.get("client_rect")
                            bridge_window_bounds = bridge_result.get("window_bounds")
                            bridge_dpi = bridge_result.get("dpi")
                            bridge_process_id = bridge_result.get("process_id")
                            client_rect_keys = ("left", "top", "right", "bottom")
                            valid_client_rect = (
                                isinstance(bridge_client_rect, dict)
                                and all(
                                    type(bridge_client_rect.get(key)) is int
                                    for key in client_rect_keys
                                )
                                and bridge_client_rect["right"] > bridge_client_rect["left"]
                                and bridge_client_rect["bottom"] > bridge_client_rect["top"]
                                and bridge_client_rect.get("unit") == "physical_px"
                                and bridge_client_rect.get("coordinate_space") == "client"
                                and bridge_client_rect.get("source_api") == "GetClientRect"
                            )
                            valid_window_bounds = (
                                isinstance(bridge_window_bounds, dict)
                                and all(
                                    type(bridge_window_bounds.get(key)) is int
                                    for key in client_rect_keys
                                )
                                and bridge_window_bounds["right"] > bridge_window_bounds["left"]
                                and bridge_window_bounds["bottom"] > bridge_window_bounds["top"]
                                and bridge_window_bounds.get("unit") == "physical_px"
                                and bridge_window_bounds.get("coordinate_space") == "screen"
                                and bridge_window_bounds.get("source_api") == "GetWindowRect"
                            )
                            typed_bitblt_fallback = (
                                type(bridge_hwnd) is int
                                and bridge_hwnd != 0
                                and isinstance(bridge_client_rect, dict)
                                and isinstance(bridge_window_bounds, dict)
                                and type(bridge_dpi) is int
                                and _is_verified_typed_bitblt_fallback(
                                    bridge_result,
                                    pid,
                                    bridge_hwnd,
                                    bridge_client_rect,
                                    bridge_window_bounds,
                                    bridge_dpi,
                                )
                            )
                            if (
                                (method != "PrintWindow" and not typed_bitblt_fallback)
                                or (
                                    (strict_target_requested or typed_bitblt_fallback)
                                    and (
                                        type(bridge_process_id) is not int
                                        or bridge_process_id != pid
                                    )
                                )
                                or type(bridge_hwnd) is not int
                                or bridge_hwnd == 0
                                or not isinstance(bridge_client_rect, dict)
                                or any(
                                    type(bridge_client_rect.get(key)) is not int
                                    for key in client_rect_keys
                                )
                                or bridge_client_rect["right"] <= bridge_client_rect["left"]
                                or bridge_client_rect["bottom"] <= bridge_client_rect["top"]
                                or type(bridge_dpi) is not int
                                or bridge_dpi <= 0
                                or not valid_client_rect
                                or (
                                    (strict_target_requested or typed_bitblt_fallback)
                                    and not valid_window_bounds
                                )
                            ):
                                if strict_target_requested:
                                    raise _PhysicalCaptureProvenanceUnavailableError(
                                        "Physical target assertions require valid bridge "
                                        "screenshot provenance"
                                    )
                                return _capture_provenance_unavailable_response(
                                    "Evidence capture requires valid bridge screenshot provenance",
                                    bridge_screenshot=bridge_screenshot,
                                )
                            if typed_bitblt_fallback:
                                foreground_mutation_attempted = True
                            if strict_target_requested or typed_bitblt_fallback:
                                from PIL import Image

                                try:
                                    with Image.open(io.BytesIO(png_bytes)) as raw_png:
                                        if raw_png.format != "PNG":
                                            raise ValueError("raw capture is not PNG")
                                        raw_png.load()
                                        decoded_width, decoded_height = raw_png.size
                                except Exception as error:
                                    if strict_target_requested:
                                        raise _PhysicalCaptureProvenanceUnavailableError(
                                            BRIDGE_RAW_PNG_INVALID_DIAGNOSTIC
                                        ) from error
                                    return _capture_provenance_unavailable_response(
                                        BRIDGE_RAW_PNG_INVALID_DIAGNOSTIC,
                                        bridge_screenshot=bridge_screenshot,
                                    )
                                if (decoded_width, decoded_height) != (raw_width, raw_height):
                                    if strict_target_requested:
                                        raise _PhysicalCaptureProvenanceUnavailableError(
                                            "Bridge screenshot dimensions do not match decoded PNG "
                                            "dimensions"
                                        )
                                    return _capture_provenance_unavailable_response(
                                        "Bridge screenshot dimensions do not match decoded PNG "
                                        "dimensions",
                                        bridge_screenshot=bridge_screenshot,
                                    )
                                window_bounds = cast(dict[str, Any], bridge_window_bounds)
                                if (
                                    window_bounds["right"] - window_bounds["left"],
                                    window_bounds["bottom"] - window_bounds["top"],
                                ) != (raw_width, raw_height):
                                    if strict_target_requested:
                                        raise _PhysicalCaptureProvenanceUnavailableError(
                                            "Bridge window bounds do not match claimed raw raster "
                                            "dimensions"
                                        )
                                    return _capture_provenance_unavailable_response(
                                        "Bridge window bounds do not match claimed raw raster "
                                        "dimensions",
                                        bridge_screenshot=bridge_screenshot,
                                    )
                            capture_metadata = await loop.run_in_executor(
                                None,
                                lambda: build_capture_metadata(
                                    bridge_hwnd,
                                    raw_width,
                                    raw_height,
                                    cast(str, method),
                                    client_rect=bridge_client_rect,
                                    dpi=bridge_dpi,
                                ),
                            )
                            if typed_bitblt_fallback:
                                cast(dict[str, Any], capture_metadata).update(
                                    {
                                        "fallback": bridge_result["fallback"],
                                        "fallback_reason": bridge_result["fallback_reason"],
                                        "authority": bridge_result["authority"],
                                        "capture_authority": bridge_result["capture_authority"],
                                        "source_api": bridge_result["source_api"],
                                        "rop": bridge_result["rop"],
                                        "evidence_grade": bridge_result["evidence_grade"],
                                        "alternate_attempts": bridge_result["alternate_attempts"],
                                        "printwindow_classification": bridge_result[
                                            "printwindow_classification"
                                        ],
                                        "printwindow_variance": bridge_result[
                                            "printwindow_variance"
                                        ],
                                        "printwindow_analysis": bridge_result[
                                            "printwindow_analysis"
                                        ],
                                        "process_id": bridge_result["process_id"],
                                        "window_bounds": bridge_result["window_bounds"],
                                        "capture_stability": bridge_result["capture_stability"],
                                        "foreground": bridge_result["foreground"],
                                        "bridge_assembly": bridge_result["bridge_assembly"],
                                    }
                                )
                            if strict_target_requested:
                                strict_target_values = cast(dict[str, int | str], strict_target)
                                capture_metadata_values = cast(dict[str, Any], capture_metadata)
                                actual_target = {
                                    "hwnd": bridge_hwnd,
                                    "width": raw_width,
                                    "height": raw_height,
                                    "unit": "physical_px",
                                    "coordinate_space": "window",
                                    "bounds_source": "GetWindowRect",
                                }
                                mismatch_fields = [
                                    field
                                    for field in ("hwnd", "width", "height")
                                    if actual_target[field] != strict_target_values[field]
                                ]
                                physical_target = {
                                    "status": "matched" if not mismatch_fields else "mismatch",
                                    **(
                                        {"code": "PHYSICAL_CAPTURE_MISMATCH"}
                                        if mismatch_fields
                                        else {}
                                    ),
                                    "expected": strict_target_values,
                                    "actual": actual_target,
                                    "mismatch_fields": mismatch_fields,
                                }
                                target_comparability = {
                                    "status": "MATCHED" if not mismatch_fields else "MISMATCH",
                                    **(
                                        {"code": "PHYSICAL_CAPTURE_MISMATCH"}
                                        if mismatch_fields
                                        else {}
                                    ),
                                    "expected": strict_target_values,
                                    "actual": actual_target,
                                    "mismatch_fields": mismatch_fields,
                                }
                                persist_evidence = not mismatch_fields
                                capture_metadata_values["raw_raster"] = {
                                    "width": raw_width,
                                    "height": raw_height,
                                    "unit": "physical_px",
                                    "coordinate_space": "window",
                                    "raster_source": method,
                                    "bounds_source": "GetWindowRect",
                                }

            if bridge_screenshot is None:
                if strict_target_requested:
                    raise _PhysicalCaptureProvenanceUnavailableError(
                        "Physical target assertions require FlaUI bridge lossless capture"
                    )
                if not restore_joined:
                    await _join_launch_foreground_restore()
                    restore_joined = True

                hwnd = get_hwnd_for_pid(pid)
                if not hwnd:
                    return build_error_response(
                        f"No visible window found for process {pid}. "
                        "The app may not have a UI yet.",
                        state=session.state.state,
                    )

                if evidence:
                    png_bytes, raw_width, raw_height, capture_metadata = await loop.run_in_executor(
                        None,
                        lambda: capture_window_evidence(hwnd),
                    )
                else:
                    png_bytes, raw_width, raw_height = await loop.run_in_executor(
                        None,
                        lambda: capture_window(hwnd),
                    )

            if evidence and (
                raw_width <= 0
                or raw_height <= 0
                or not isinstance(capture_metadata, dict)
                or (capture_metadata.get("method") != "PrintWindow" and not typed_bitblt_fallback)
                or not {
                    "method",
                    "hwnd",
                    "client_rect",
                    "dpi",
                    "dpi_scale",
                    "physical_width",
                    "physical_height",
                    "logical_width",
                    "logical_height",
                }.issubset(capture_metadata)
            ):
                if strict_target_requested:
                    raise _PhysicalCaptureProvenanceUnavailableError(
                        "Physical target assertions require complete bridge screenshot provenance"
                    )
                raise ValueError("Evidence capture requires complete capture metadata")

            frame_analysis = await loop.run_in_executor(
                None,
                lambda: analyze_screenshot_frame(png_bytes),
            )
            if frame_analysis["probable_black"] and (
                typed_bitblt_fallback or physical_target is None or persist_evidence
            ):
                return _probable_black_frame_response(
                    frame_analysis,
                    retry_tool="ui_take_screenshot",
                    foreground_mutation_attempted=foreground_mutation_attempted,
                    capture_diagnostics=_capture_route_diagnostics(
                        bridge_screenshot=bridge_screenshot,
                        capture_metadata=capture_metadata,
                        frame_analysis=frame_analysis,
                    ),
                )

            raw_path = None
            crop_path = None
            crop_bytes = None
            crop_size: tuple[int, int] | None = None
            if persist_evidence and crop_rect is not None:
                crop_left, crop_top, crop_w, crop_h = crop_rect
                crop_bytes, cropped_width, cropped_height = await loop.run_in_executor(
                    None,
                    lambda: crop_png(png_bytes, crop_left, crop_top, crop_w, crop_h),
                )
                crop_size = (cropped_width, cropped_height)

            # Create HD version in requested format
            hd_bytes, hd_w, hd_h, _ = await loop.run_in_executor(
                None,
                lambda: _process_screenshot(png_bytes, max_width=max_width, format=safe_format),
            )

            # Create inline preview (≤1280px WebP — Claude vision optimal)
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None,
                lambda: create_preview(png_bytes, max_width=max_width, quality=80),
            )
            if persist_evidence:
                assert sid is not None
                assert temp_manager is not None
                capture_id = uuid.uuid4().hex
                bundle = await loop.run_in_executor(
                    None,
                    lambda: temp_manager.save_screenshot_bundle(
                        sid,
                        png_bytes,
                        f"evidence_{capture_id}.png",
                        crop_bytes,
                        f"evidence_crop_{capture_id}.png" if crop_bytes is not None else None,
                    ),
                )
                if bundle is None:
                    raise ValueError("Failed to persist screenshot evidence bundle")
                raw_path, crop_path = bundle
                if crop_bytes is not None and crop_path is None:
                    raise ValueError("Failed to persist cropped screenshot evidence")

            metadata: dict[str, Any] = {
                "width": hd_w,
                "height": hd_h,
                "preview_width": preview_w,
                "format": safe_format,
                "evidence_grade": (
                    "typed_bitblt_fallback"
                    if persist_evidence and typed_bitblt_fallback
                    else "lossless_raster"
                    if persist_evidence
                    else "preview_only"
                ),
                "state": session.state.state.value
                if hasattr(session.state.state, "value")
                else str(session.state.state),
            }
            if bridge_screenshot is not None:
                mode = _stealth_response_mode(bridge_screenshot)
                if mode:
                    metadata["mode"] = mode
                for key in (
                    "method",
                    "hwnd",
                    "client_rect",
                    "window_bounds",
                    "dpi",
                    "fallback",
                    "fallback_reason",
                    "authority",
                    "capture_authority",
                    "source_api",
                    "rop",
                    "alternate_attempts",
                    "flags",
                    "variance",
                    "printwindow_variance",
                    "printwindow_classification",
                    "printwindow_analysis",
                    "process_id",
                    "capture_stability",
                    "foreground",
                    "bridge_assembly",
                    "printwindow_error",
                ):
                    if key in bridge_screenshot:
                        metadata[key] = bridge_screenshot[key]
            if physical_target is not None:
                metadata["physical_target"] = physical_target
            if target_comparability is not None:
                metadata["target_comparability"] = target_comparability
            if persist_evidence:
                metadata.update(
                    {
                        "retention": "stop_cleanup_or_stale_gc_after_4h",
                        "raw_path": str(raw_path),
                        "raw_sha256": hashlib.sha256(png_bytes).hexdigest(),
                        "raw_mime": "image/png",
                        "raw_width": raw_width,
                        "raw_height": raw_height,
                        "capture_metadata": capture_metadata,
                    }
                )
                if crop_bytes is not None and crop_path is not None and crop_size is not None:
                    metadata.update(
                        {
                            "crop_path": str(crop_path),
                            "crop_sha256": hashlib.sha256(crop_bytes).hexdigest(),
                            "crop_rect": {
                                "x": crop_x,
                                "y": crop_y,
                                "width": crop_width,
                                "height": crop_height,
                            },
                            "crop_width": crop_size[0],
                            "crop_height": crop_size[1],
                        }
                    )

            if (
                sid
                and temp_manager is not None
                and (not strict_target_requested or persist_evidence)
            ):
                ts = int(_time.time() * 1000) & 0xFFFFFFFF
                hd_path = temp_manager.save_screenshot(
                    sid,
                    hd_bytes,
                    f"screenshot_{ts:08x}.{safe_format}",
                )
                if hd_path:
                    metadata["hd_path"] = str(hd_path)

            return [
                ImageContent(
                    type="image",
                    data=base64.b64encode(preview_bytes).decode("ascii"),
                    mimeType="image/webp",
                ),
                TextContent(
                    type="text",
                    text=json.dumps(metadata),
                ),
            ]
        except _PhysicalCaptureProvenanceUnavailableError as error:
            response = build_error_response(str(error), state=session.state.state)
            response["code"] = "PHYSICAL_CAPTURE_PROVENANCE_UNAVAILABLE"
            return response
        except Exception as error:
            return build_error_response(str(error), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_take_annotated_screenshot(
        ctx: Context,
        max_depth: int = 3,
        interactive_only: bool = True,
        max_width: int = 1568,
        format: str = "webp",
        compact: bool = True,
    ) -> Any:
        """Take a screenshot with numbered UI elements overlaid (Set-of-Mark pattern).

        Returns annotated WebP image + compact element index.
        Each interactive element gets a numbered label on the screenshot.

        Use ui_click_annotated(element_id) to interact with elements by number.

        Args:
            max_depth: How deep to traverse the UI tree (default 3)
            interactive_only: Only interactive elements (default True)
            max_width: Max image width (default 1024)
            format: Image format: "webp" (smallest), "jpeg", "png"
            compact: Compact element index — id+name only (default True, saves ~60KB)
        """
        nonlocal _last_annotation, _annotation_generation

        try:
            import ctypes
            from ctypes import wintypes

            from ..ui.screenshot import (
                analyze_screenshot_frame,
                annotate_screenshot,
                capture_window,
                collect_visible_elements,
                get_hwnd_for_pid,
            )

            pid = session.state.process_id
            if not pid:
                return build_error_response("No debug process.", state=session.state.state)

            backend = await _ensure_ui_connected(observation=True)

            hwnd = get_hwnd_for_pid(pid)
            if not hwnd:
                return build_error_response(
                    f"No visible window for process {pid}.",
                    state=session.state.state,
                )

            loop = asyncio.get_running_loop()

            # Capture screenshot
            png_bytes, _, _ = await loop.run_in_executor(
                None,
                lambda: capture_window(hwnd),
            )

            frame_analysis = await loop.run_in_executor(
                None,
                lambda: analyze_screenshot_frame(png_bytes),
            )
            if frame_analysis["probable_black"]:
                _last_annotation = None
                return _probable_black_frame_response(
                    frame_analysis,
                    retry_tool="ui_take_annotated_screenshot",
                    foreground_mutation_attempted=False,
                )

            # Collect elements — needs pywinauto _app access
            from ..ui.pywinauto_backend import PywinautoBackend

            if isinstance(backend, PywinautoBackend):
                app = backend.inner._app
            else:
                # FlaUI backend: fall back to connecting pywinauto just for element collection
                from ..ui import UIAutomation

                _fallback_ui = UIAutomation()
                await _fallback_ui.connect(pid)
                app = _fallback_ui._app

            elements = await loop.run_in_executor(
                None,
                lambda: collect_visible_elements(app, max_depth, interactive_only),
            )

            # Get window screen position for coordinate conversion
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            window_rect = (rect.left, rect.top, rect.right, rect.bottom)

            # Annotate screenshot (with optional downsampling)
            annotated_bytes = await loop.run_in_executor(
                None,
                lambda: annotate_screenshot(png_bytes, elements, window_rect, max_width),
            )

            # Cache elements for ui_click_annotated
            _annotation_generation += 1
            _last_annotation = {
                "elements": elements,
                "window_rect": window_rect,
                "hwnd": hwnd,
                "generation": _annotation_generation,
            }

            # Convert to optimal format
            import base64
            import json

            from mcp.types import ImageContent, TextContent

            from ..ui.screenshot import _process_screenshot, create_preview

            valid_formats = {"webp", "jpeg", "png"}
            safe_format = format if format in valid_formats else "webp"

            hd_bytes, hd_w, hd_h, _ = await loop.run_in_executor(
                None,
                lambda: _process_screenshot(
                    annotated_bytes, max_width=max_width, format=safe_format
                ),
            )

            # Create inline preview (≤max_width WebP) — in executor to avoid blocking loop
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None,
                lambda: create_preview(annotated_bytes, max_width=max_width, quality=80),
            )

            # Build element index — compact (id+name) or full (id+name+type+automationId)
            if compact:
                elem_index: list[Any] = [
                    f"{e['id']}: {e['name'] or e['automationId'] or e['type']}" for e in elements
                ]
            else:
                elem_index = [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "type": e["type"],
                        "automationId": e["automationId"],
                    }
                    for e in elements
                ]

            # Save HD to session temp dir
            metadata: dict[str, Any] = {
                "width": hd_w,
                "height": hd_h,
                "preview_width": preview_w,
                "elements": elem_index,
                "element_count": len(elements),
                "generation": _annotation_generation,
                "format": safe_format,
                "state": session.state.state.value
                if hasattr(session.state.state, "value")
                else str(session.state.state),
            }

            sid = session.session_id
            if sid:
                hd_path = session.temp_manager.save_screenshot(
                    sid,
                    hd_bytes,
                    f"annotated_{_annotation_generation:04d}.{safe_format}",
                )
                if hd_path:
                    metadata["hd_path"] = str(hd_path)

            content: list = [
                ImageContent(
                    type="image",
                    data=base64.b64encode(preview_bytes).decode("ascii"),
                    mimeType="image/webp",
                ),
                TextContent(
                    type="text",
                    text=json.dumps(metadata),
                ),
            ]
            return content
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click_annotated(
        ctx: Context, element_id: int, generation: int | None = None
    ) -> dict:
        """Click an element by its ID from ui_take_annotated_screenshot.

        Uses the numbered element from the last annotated screenshot.
        Call ui_take_annotated_screenshot first to get element IDs.

        Args:
            element_id: Element ID number from the annotated screenshot
            generation: Generation counter from the screenshot response (optional, warns if stale)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if _last_annotation is None:
                return build_error_response(
                    "No annotation data. Call ui_take_annotated_screenshot first.",
                    state=session.state.state,
                )

            # Warn if annotation data may be stale
            stale_warning = None
            current_gen = _last_annotation.get("generation", 0)
            if generation is not None and generation != current_gen:
                stale_warning = (
                    f"Annotation data may be stale: requested generation {generation}, "
                    f"current is {current_gen}. Consider retaking the screenshot."
                )
                logger.warning(stale_warning)

            elements = _last_annotation["elements"]
            target = None
            for e in elements:
                if e["id"] == element_id:
                    target = e
                    break

            if target is None:
                return build_error_response(
                    f"Element {element_id} not found. Valid IDs: {[e['id'] for e in elements]}",
                    state=session.state.state,
                )

            # Click center of element bounds using centralized SendInput implementation
            bounds = target["bounds"]
            center_x = bounds["x"] + bounds["width"] // 2
            center_y = bounds["y"] + bounds["height"] // 2

            ui = await _ensure_ui_connected()
            await ui.click_at(center_x, center_y)

            response_data: dict[str, Any] = {
                "clicked": True,
                "element": target["name"],
                "position": {"x": center_x, "y": center_y},
            }
            if stale_warning:
                response_data["warning"] = stale_warning

            return build_response(
                data=response_data,
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Advanced interaction tools --

    async def _select_via_clicks(ui_inst, automation_id: str, indices: list[int], mode: str) -> int:
        """Fallback: select items by Ctrl+clicking their cached coordinates.

        Does a deeper tree walk (depth=5) to find ListBoxItem/DataItem children,
        then clicks them with Ctrl held for multi-select.
        """
        import ctypes

        # Deep tree walk to find child items
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: ui_inst.get_window_tree.__wrapped__(ui_inst, max_depth=5, max_children=100)
            if hasattr(ui_inst.get_window_tree, "__wrapped__")
            else None,
        )

        # Rebuild: find items inside the control's bounds from refreshed cache
        cache = ui_inst._element_cache
        parent_data = cache.get(automation_id)
        if not parent_data or not parent_data.get("rect"):
            return 0

        pr = parent_data["rect"]

        # Collect ListItem/DataItem children within parent bounds, sorted by Y position
        child_items = []
        for aid, data in cache.items():
            if aid == automation_id:
                continue
            r = data.get("rect")
            ct = data.get("control_type", "")
            if not r or ct not in ("ListItem", "DataItem", "TreeItem", "ListBoxItem"):
                continue
            if (
                pr["left"] <= r["left"]
                and r["right"] <= pr["right"]
                and pr["top"] <= r["top"]
                and r["bottom"] <= pr["bottom"]
            ):
                child_items.append(r)

        # Sort by vertical position (top to bottom)
        child_items.sort(key=lambda r: r["top"])

        selected = 0
        vk_control = 0x11
        keyeventf_keyup = 0x0002

        # Click first item (plain click to set initial selection)
        first_done = False
        for target_idx in indices:
            if target_idx >= len(child_items):
                continue
            rect = child_items[target_idx]
            cx = (rect["left"] + rect["right"]) // 2
            cy = (rect["top"] + rect["bottom"]) // 2
            await ui_inst._click_at_coords(cx, cy)
            selected += 1
            first_done = True
            break

        # Remaining items: hold Ctrl for entire sequence, click each
        remaining = [i for i in indices if i != indices[0] and i < len(child_items)]
        if remaining and first_done:
            # Press Ctrl ONCE before all remaining clicks
            ctypes.windll.user32.keybd_event(vk_control, 0, 0, 0)
            try:
                for target_idx in remaining:
                    rect = child_items[target_idx]
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await asyncio.sleep(0.05)
                    await ui_inst._click_at_coords(cx, cy)
                    selected += 1
            finally:
                # ALWAYS release Ctrl
                ctypes.windll.user32.keybd_event(vk_control, 0, keyeventf_keyup, 0)

        return selected

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_select_items(
        ctx: Context,
        automation_id: str,
        indices: list[int],
        mode: str = "replace",
    ) -> dict:
        """Select items by index in a list/grid control (DataGrid, ListView, ListBox).

        With FlaUI backend: uses SelectionItemPattern (reliable for virtualized lists).
        With pywinauto backend: two strategies (tries both):
        1. UIA SelectionItemPattern — works for non-virtualized lists
        2. Coordinate click fallback — clicks items using cached rectangles
           (Ctrl+click for multi-select, plain click for first item)

        For WPF virtualized lists (VirtualizingStackPanel), strategy 1 may fail
        because off-screen items don't have UI containers. Strategy 2 uses visible
        item coordinates from the cache. FlaUI backend handles this natively.

        Args:
            automation_id: AutomationId of the list/grid control
            indices: List of 0-based item indices to select
            mode: "replace" (clear existing, select these) or "add" (add to existing selection)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            multi_select = getattr(ui, "multi_select", None)
            if _is_flaui_backend(ui) and callable(multi_select):
                result = await multi_select(automation_id, indices, mode=mode)
                bridge_evidence = getattr(ui, "_last_multi_select_result", None)
                payload = _selection_items_payload(
                    bridge_evidence if isinstance(bridge_evidence, dict) else result,
                    indices=indices,
                    mode=mode,
                    default_method="FlaUI.multi_select",
                )
                return build_response(data=payload, state=session.state.state)

            # Strategy 1: UIA SelectionItemPattern
            def _select_via_pattern() -> int:
                window = ui._app.top_window()
                control = window.child_window(auto_id=automation_id)
                control.wait("exists", timeout=5)

                items = control.children()
                selected_count = 0

                for i, item in enumerate(items):
                    try:
                        iface = item.iface_selection_item
                    except Exception:
                        continue

                    if i in indices:
                        if mode == "replace" and selected_count == 0:
                            iface.select()
                        else:
                            iface.add_to_selection()
                        selected_count += 1
                    elif mode == "replace":
                        try:
                            iface.remove_from_selection()
                        except Exception:
                            pass

                return selected_count

            loop = asyncio.get_running_loop()
            try:
                selected = await asyncio.wait_for(
                    loop.run_in_executor(None, _select_via_pattern),
                    timeout=10.0,
                )
            except Exception:
                # Strategy 1 failed (e.g., add_to_selection error on Extended ListBox)
                selected = 0

            # Strategy 2: coordinate Ctrl+click fallback
            if selected < len(indices):
                selected = await _select_via_clicks(ui, automation_id, indices, mode)

            method = "pattern" if selected == len(indices) else "click_fallback"
            return build_response(
                data={"selected": selected, "indices": indices, "mode": mode, "method": method},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_right_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Right-click on a UI element to open context menu.

        Tries cached coordinates first, then falls back to pywinauto element search.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Try cache first
            if automation_id and not _has_secondary_selector_constraints(
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            ):
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.right_click_at(cx, cy)
                    return build_response(
                        data={
                            "right_clicked": True,
                            "method": "cache",
                            "position": {"x": cx, "y": cy},
                        },
                        state=session.state.state,
                    )

            # Fallback to element search
            ui, element, _ = await _find_ui_element(
                automation_id, name, control_type, root_id, xpath
            )
            mismatch = _exact_automation_id_mismatch_payload(
                action="ui_right_click",
                requested_automation_id=automation_id,
                result=element,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)

            if isinstance(element, dict):
                rect = element.get("rect", {})
                if not rect:
                    return build_error_response(
                        "right_click: selected element has no rectangle",
                        state=session.state.state,
                    )
                cx = int(rect.get("x", 0) + rect.get("width", 0) / 2)
                cy = int(rect.get("y", 0) + rect.get("height", 0) / 2)
                await ui.right_click_at(cx, cy)
            else:

                def _right_click() -> None:
                    element.click_input(button="right")

                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(None, _right_click),
                    timeout=5.0,
                )

            return build_response(
                data={"right_clicked": True, "method": "element_search"}, state=session.state.state
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_double_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Double-click on a UI element.

        Tries cached coordinates first, then falls back to pywinauto element search.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Try cache first
            if automation_id and not _has_secondary_selector_constraints(
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            ):
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.double_click_at(cx, cy)
                    return build_response(
                        data={
                            "double_clicked": True,
                            "method": "cache",
                            "position": {"x": cx, "y": cy},
                        },
                        state=session.state.state,
                    )

            # Fallback to element search
            ui, element, _ = await _find_ui_element(
                automation_id, name, control_type, root_id, xpath
            )
            mismatch = _exact_automation_id_mismatch_payload(
                action="ui_double_click",
                requested_automation_id=automation_id,
                result=element,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if mismatch is not None:
                return build_response(data=mismatch, state=session.state.state)

            if isinstance(element, dict):
                rect = element.get("rect", {})
                if not rect:
                    return build_error_response(
                        "double_click: selected element has no rectangle",
                        state=session.state.state,
                    )
                cx = int(rect.get("x", 0) + rect.get("width", 0) / 2)
                cy = int(rect.get("y", 0) + rect.get("height", 0) / 2)
                await ui.double_click_at(cx, cy)
            else:

                def _double_click() -> None:
                    element.double_click_input()

                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(None, _double_click),
                    timeout=5.0,
                )

            return build_response(
                data={"double_clicked": True, "method": "element_search"}, state=session.state.state
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_scroll(
        ctx: Context,
        automation_id: str,
        direction: str = "down",
        amount: int = 3,
    ) -> dict:
        """Scroll a UI control.

        Note: If app is STOPPED at breakpoint, resume with continue_execution() first.

        Args:
            automation_id: AutomationId of the scrollable control
            direction: "up", "down", "left", "right"
            amount: Number of scroll units (default 3)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            def _scroll() -> None:
                window = ui._app.top_window()
                control = window.child_window(auto_id=automation_id)
                control.wait("exists", timeout=5)
                control.scroll(direction, "page", amount)

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _scroll),
                timeout=5.0,
            )

            return build_response(
                data={"scrolled": True, "direction": direction, "amount": amount},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_drag(
        ctx: Context,
        from_automation_id: str | None = None,
        to_automation_id: str | None = None,
        from_x: int | None = None,
        from_y: int | None = None,
        to_x: int | None = None,
        to_y: int | None = None,
        speed_ms: int = 200,
        hold_modifiers: list[str] | None = None,
    ) -> dict:
        """Drag from one position to another.

        Two modes:
        1. By AutomationId: from_automation_id + to_automation_id (uses cached rectangles)
        2. By coordinates: from_x, from_y, to_x, to_y (absolute screen coords)

        For mode 1, call ui_get_window_tree first to populate cache.

        Args:
            from_automation_id: Source element AutomationId
            to_automation_id: Target element AutomationId
            from_x: Source X coordinate (screen absolute)
            from_y: Source Y coordinate
            to_x: Target X coordinate
            to_y: Target Y coordinate
            speed_ms: Total drag duration in milliseconds. Minimum 20 ms so the
                gesture always emits enough waypoints to cross common WPF drag
                thresholds reliably.
            hold_modifiers: Optional modifier names to hold for the full drag.
                Accepted values: ctrl, shift, alt, win.

        Notes:
            - Identical from/to coordinates are rejected.
            - Short drags that stay below the system drag threshold should use
              ui_click instead of ui_drag.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if speed_ms < 20:
                return build_error_response(
                    "speed_ms below drag-threshold safety floor (minimum 20)",
                    state=session.state.state,
                )

            normalized_modifiers = _normalize_modifier_list(hold_modifiers)

            def resolve_drag_coordinates(
                cache_owner: Any | None,
            ) -> tuple[int, int, int, int] | str:
                cache = getattr(cache_owner, "element_cache", {})
                if not isinstance(cache, dict):
                    cache = {}
                fx, fy, tx, ty = from_x, from_y, to_x, to_y

                if from_automation_id and (fx is None or fy is None):
                    from_rect = (cache.get(from_automation_id) or {}).get("rect")
                    if from_rect:
                        fx = (from_rect["left"] + from_rect["right"]) // 2
                        fy = (from_rect["top"] + from_rect["bottom"]) // 2
                    else:
                        return (
                            f"Element '{from_automation_id}' not in cache. "
                            "Call ui_get_window_tree first."
                        )

                if to_automation_id and (tx is None or ty is None):
                    to_rect = (cache.get(to_automation_id) or {}).get("rect")
                    if to_rect:
                        tx = (to_rect["left"] + to_rect["right"]) // 2
                        ty = (to_rect["top"] + to_rect["bottom"]) // 2
                    else:
                        return (
                            f"Element '{to_automation_id}' not in cache. "
                            "Call ui_get_window_tree first."
                        )

                if fx is None or fy is None or tx is None or ty is None:
                    return (
                        "Provide either automation_ids or coordinates for both source and target."
                    )
                if fx == tx and fy == ty:
                    return "from and to coordinates are identical (0 px distance)"
                return fx, fy, tx, ty

            preflight = resolve_drag_coordinates(_backend_holder["instance"])
            if isinstance(preflight, str):
                return build_error_response(preflight, state=session.state.state)

            ui = await _ensure_ui_connected()
            coordinates = resolve_drag_coordinates(ui)
            if isinstance(coordinates, str):
                return build_error_response(coordinates, state=session.state.state)
            fx, fy, tx, ty = coordinates

            result = await ui.drag(
                fx,
                fy,
                tx,
                ty,
                speed_ms=speed_ms,
                hold_modifiers=normalized_modifiers or None,
            )
            if not isinstance(result, dict):
                return build_error_response(
                    f"drag: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "drag not supported on current backend"),
                    state=session.state.state,
                )

            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_system_event(
        ctx: Context,
        event: str,
        mode: str = "toggle",
    ) -> dict:
        """Send a supported system event through the active UI backend."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            normalized_event = event.strip().lower()
            if normalized_event not in _SUPPORTED_SYSTEM_EVENTS:
                return build_error_response(
                    "Unsupported event. Supported events: theme_change",
                    state=session.state.state,
                )

            normalized_mode = mode.strip().lower()
            if normalized_mode not in _SUPPORTED_THEME_MODES:
                return build_error_response(
                    "Unsupported mode. Supported modes: toggle, light, dark",
                    state=session.state.state,
                )

            ui = await _ensure_ui_connected()
            result = await ui.send_system_event(normalized_event, mode=normalized_mode)
            if not isinstance(result, dict):
                return build_error_response(
                    "send_system_event: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "send_system_event not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_hold_modifiers(ctx: Context, modifiers: list[str]) -> dict:
        """Hold modifiers across subsequent UI input calls."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            normalized_modifiers = _normalize_modifier_list(modifiers)
            ui = await _ensure_ui_connected()
            result = await ui.hold_modifiers(normalized_modifiers)
            if not isinstance(result, dict):
                return build_error_response(
                    f"hold_modifiers: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "hold_modifiers not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_release_modifiers(
        ctx: Context,
        modifiers: list[str] | str,
    ) -> dict:
        """Release held modifiers or all held modifiers."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            normalized_modifiers = _normalize_release_modifiers(modifiers)
            ui = await _ensure_ui_connected()
            result = await ui.release_modifiers(normalized_modifiers)
            if not isinstance(result, dict):
                return build_error_response(
                    "release_modifiers: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "release_modifiers not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_get_held_modifiers(ctx: Context) -> dict:
        """Inspect currently held modifiers."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected(observation=True)
            result = await ui.get_held_modifiers()
            if not isinstance(result, dict):
                return build_error_response(
                    "get_held_modifiers: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "get_held_modifiers not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Read / query tools --

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_get_selected_item(
        automation_id: str,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Get the currently selected item in a list/grid control.

        Returns the selected item's name, index, and properties.
        Useful for verifying selection state after clicks or keyboard navigation.

        Note: FlaUI backend returns selection for the first item only.
        Use ui_find_element to inspect individual items for full multi-selection state.

        Args:
            automation_id: AutomationId of the list/grid/combobox control
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            ui = await _ensure_ui_connected(observation=True)
            get_selected_item = getattr(ui, "get_selected_item", None)
            if callable(get_selected_item):
                result = await get_selected_item(
                    automation_id=automation_id,
                    root_id=root_id,
                    xpath=xpath,
                )
                if not isinstance(result, dict):
                    return build_error_response(
                        "get_selected_item: backend returned non-dict response "
                        f"({type(result).__name__})",
                        state=session.state.state,
                    )
                return build_response(data=result, state=session.state.state)

            def _get_selected() -> dict[str, Any]:
                from ..ui.pywinauto_backend import PywinautoBackend

                if isinstance(ui, PywinautoBackend):
                    window = ui.inner._app.top_window()
                    search_root = window
                    if root_id:
                        search_root = window.child_window(auto_id=root_id)
                        search_root.wait("exists", timeout=5)
                    control = search_root.child_window(auto_id=automation_id)
                    control.wait("exists", timeout=5)

                    # Try SelectionPattern via iface_selection
                    try:
                        selection = control.iface_selection.GetCurrentSelection()
                        if selection and selection.Length > 0:
                            selected_elem = selection.GetElement(0)
                            from pywinauto.uia_element_info import UIAElementInfo

                            elem_info = UIAElementInfo(selected_elem)
                            from pywinauto.controls.uiawrapper import UIAWrapper

                            wrapper = UIAWrapper(elem_info)
                            children = control.children()
                            idx = -1
                            for i, child in enumerate(children):
                                try:
                                    if (
                                        child.element_info.runtime_id
                                        == wrapper.element_info.runtime_id
                                    ):
                                        idx = i
                                        break
                                except Exception:
                                    pass
                            return {
                                "index": idx,
                                "name": wrapper.element_info.name or "",
                                "automationId": getattr(wrapper.element_info, "automation_id", "")
                                or "",
                                "controlType": wrapper.element_info.control_type or "",
                            }
                    except Exception:
                        pass

                    # Fallback: iterate children looking for IsSelected
                    children = control.children()
                    for i, child in enumerate(children):
                        try:
                            iface = child.iface_selection_item
                            if iface.IsSelected:
                                return {
                                    "index": i,
                                    "name": child.element_info.name or "",
                                    "automationId": getattr(child.element_info, "automation_id", "")
                                    or "",
                                    "controlType": child.element_info.control_type or "",
                                }
                        except Exception:
                            continue

                    return {"index": -1, "name": "", "automationId": "", "controlType": ""}
                else:
                    return {
                        "index": -1,
                        "name": "",
                        "automationId": "",
                        "controlType": "",
                        "warning": "Selection query not yet supported via FlaUI backend",
                    }

            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _get_selected),
                timeout=10.0,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_read_text(
        automation_id: str | None = None,
        name: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Read text content from a UI element using multi-strategy extraction.

        Tries 5 strategies in order: ValuePattern → TextPattern → Name →
        LegacyIAccessible → visible text descendants. The response includes
        which strategy provided the text (source field).

        When the primary text looks like a CLR type name (e.g., "Namespace.Class"),
        automatically falls back to visible descendant text.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            ui = await _ensure_ui_connected(observation=True)
            result = await ui.extract_text(
                automation_id=automation_id,
                name=name,
                root_id=root_id,
                xpath=xpath,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_get_focused_element() -> dict:
        """Get information about the currently focused UI element.

        Returns the focused element's automationId, name, controlType, and value.
        Useful for verifying focus state after ui_set_focus or tab navigation.

        Note: Returns focus within the app window, not always OS-level dialogs.
        """
        try:
            ui = await _ensure_ui_connected(observation=True)
            get_focused_element = getattr(ui, "get_focused_element", None)
            if callable(get_focused_element):
                result = await get_focused_element()
                if not isinstance(result, dict):
                    return build_error_response(
                        "get_focused_element: backend returned non-dict response "
                        f"({type(result).__name__})",
                        state=session.state.state,
                    )
                return build_response(data=result, state=session.state.state)
            if _is_flaui_backend(ui):
                return build_response(
                    data=_flaui_focused_element_guidance(),
                    state=session.state.state,
                )

            def _get_focused() -> dict[str, Any]:
                from ..ui.pywinauto_backend import PywinautoBackend

                if isinstance(ui, PywinautoBackend):
                    import comtypes.client  # noqa: F401
                    from pywinauto.uia_defines import IUIA

                    iuia = IUIA()
                    focused = iuia.iuia.GetFocusedElement()
                    if focused is None:
                        return {"name": "", "automationId": "", "controlType": "", "value": ""}

                    from pywinauto.uia_element_info import UIAElementInfo

                    elem_info = UIAElementInfo(focused)
                    from pywinauto.controls.uiawrapper import UIAWrapper

                    wrapper = UIAWrapper(elem_info)
                    info = wrapper.element_info

                    value = ""
                    try:
                        value = wrapper.iface_value.Value or ""
                    except Exception:
                        pass

                    return {
                        "name": info.name or "",
                        "automationId": getattr(info, "automation_id", "") or "",
                        "controlType": info.control_type or "",
                        "value": value,
                    }
                else:
                    return {
                        "name": "",
                        "automationId": "",
                        "controlType": "",
                        "value": "",
                        "warning": "Focused element query not yet supported via FlaUI backend",
                    }

            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _get_focused),
                timeout=5.0,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def ui_wait_for(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        timeout: float = 5.0,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Wait for a UI element to appear within timeout.

        Polls every 500ms until the element is found or timeout expires.
        Useful for waiting for dialogs, popups, or dynamically created elements.

        Args:
            automation_id: AutomationId to wait for
            name: Element name to wait for
            control_type: Control type to wait for
            timeout: Maximum wait time in seconds (default 5)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            if not any((automation_id, name, control_type, xpath)):
                return build_error_response(
                    "At least one search criterion must be provided.",
                    state=session.state.state,
                )

            # Clamp timeout to reasonable bounds
            clamped_timeout = max(0.5, min(timeout, 30.0))

            ui = await _ensure_ui_connected(observation=True)

            import time as _time

            from ..ui import ElementNotFoundError

            start = _time.monotonic()
            poll_interval = 0.5
            last_error = ""

            while True:
                elapsed = _time.monotonic() - start
                if elapsed >= clamped_timeout:
                    break
                try:
                    result = await ui.find_element(
                        automation_id=automation_id,
                        name=name,
                        control_type=control_type,
                        root_id=root_id,
                        xpath=xpath,
                    )
                    data = result if isinstance(result, dict) else result.to_dict()
                    return build_response(
                        data={"found": True, "elapsed": round(elapsed, 2), "element": data},
                        state=session.state.state,
                    )
                except (ElementNotFoundError, TimeoutError, asyncio.TimeoutError):
                    remaining = clamped_timeout - elapsed
                    sleep_time = min(poll_interval, remaining)
                    if sleep_time <= 0:
                        break
                    await asyncio.sleep(sleep_time)
                except Exception as e:
                    last_error = str(e)
                    remaining = clamped_timeout - elapsed
                    sleep_time = min(poll_interval, remaining)
                    if sleep_time <= 0:
                        break
                    await asyncio.sleep(sleep_time)

            return build_error_response(
                f"Element not found within {clamped_timeout}s. Last error: {last_error}",
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- v0.11.1: Pattern expansion tools --

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_close_window(ctx: Context, window_title: str | None = None) -> dict:
        """Close a top-level window via WindowPattern.

        After closing, subsequent ui_* calls return an error if the closed window
        was the active session window. Use window_title to target a specific window
        (e.g. a modal dialog); omit to close the main application window.

        Args:
            window_title: Optional partial title match to target a specific window.
                          Omit to target the main connected window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.close_window(window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    f"close_window: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "close_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_maximize_window(ctx: Context, window_title: str | None = None) -> dict:
        """Maximize a top-level window via WindowPattern.

        Args:
            window_title: Optional partial title match. Omit to target main window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.maximize_window(window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    "maximize_window: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "maximize_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_minimize_window(ctx: Context, window_title: str | None = None) -> dict:
        """Minimize a top-level window via WindowPattern.

        Args:
            window_title: Optional partial title match. Omit to target main window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.minimize_window(window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    "minimize_window: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "minimize_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_restore_window(ctx: Context, window_title: str | None = None) -> dict:
        """Restore a minimized or maximized window to normal state via WindowPattern.

        Args:
            window_title: Optional partial title match. Omit to target main window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.restore_window(window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    f"restore_window: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "restore_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_move_window(ctx: Context, x: int, y: int, window_title: str | None = None) -> dict:
        """Move a window to screen coordinates (x, y) via TransformPattern.

        Returns {moved: false, reason: "..."} if the window cannot be moved
        (CanMove = false). Does NOT raise an exception in that case.

        Args:
            x: Target screen X coordinate for the window's top-left corner.
            y: Target screen Y coordinate for the window's top-left corner.
            window_title: Optional partial title match. Omit to target main window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.move_window(x=x, y=y, window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    f"move_window: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "move_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_resize_window(
        ctx: Context,
        width: int,
        height: int,
        window_title: str | None = None,
    ) -> dict:
        """Resize a window to the given dimensions via TransformPattern.

        Returns {resized: false, reason: "..."} if the window cannot be resized
        (CanResize = false). Does NOT raise an exception in that case.

        Args:
            width: Target window width in pixels.
            height: Target window height in pixels.
            window_title: Optional partial title match. Omit to target main window.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if width <= 0:
                raise ValueError(f"width must be positive, got {width}")
            if height <= 0:
                raise ValueError(f"height must be positive, got {height}")

            ui = await _ensure_ui_connected()
            result = await ui.resize_window(width=width, height=height, window_title=window_title)
            if not isinstance(result, dict):
                return build_error_response(
                    f"resize_window: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "resize_window not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_expand(ctx: Context, automation_id: str) -> dict:
        """Expand a TreeView node, ComboBox dropdown, or other collapsible element.

        Uses ExpandCollapsePattern. Expanding an already-expanded element is safe
        and returns {expanded: true, was_already: true}.

        Args:
            automation_id: AutomationId of the element to expand.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if not automation_id:
                raise ValueError("automation_id is required")

            ui = await _ensure_ui_connected()
            result = await ui.expand(automation_id=automation_id)
            if not isinstance(result, dict):
                return build_error_response(
                    f"expand: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "expand not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_collapse(ctx: Context, automation_id: str) -> dict:
        """Collapse a TreeView node, ComboBox dropdown, or other collapsible element.

        Uses ExpandCollapsePattern. Collapsing an already-collapsed element is safe
        and returns {collapsed: true, was_already: true}.

        Args:
            automation_id: AutomationId of the element to collapse.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if not automation_id:
                raise ValueError("automation_id is required")

            ui = await _ensure_ui_connected()
            result = await ui.collapse(automation_id=automation_id)
            if not isinstance(result, dict):
                return build_error_response(
                    f"collapse: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "collapse not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_set_value(ctx: Context, automation_id: str, value: float) -> dict:
        """Set a numeric value on a slider, spinner, or progress bar via RangeValuePattern.

        Returns {set: false, reason: "value X out of range [min..max]"} if the
        value is outside the element's Min/Max bounds — does NOT raise an exception.

        Args:
            automation_id: AutomationId of the RangeValue element (slider, spinner, etc.)
            value: The numeric value to set. Must be within element's [Minimum, Maximum].
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if not automation_id:
                raise ValueError("automation_id is required")

            ui = await _ensure_ui_connected()
            result = await ui.set_value(automation_id=automation_id, value=value)
            if not isinstance(result, dict):
                return build_error_response(
                    f"set_value: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "set_value not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_clipboard_read(ctx: Context) -> dict:
        """Read text content from the system clipboard.

        Executes on an STA thread inside the FlaUI bridge (required by
        System.Windows.Clipboard). Returns {text: "...", has_text: bool}.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected(observation=True)
            result = await ui.clipboard_read()
            if not isinstance(result, dict):
                return build_error_response(
                    f"clipboard_read: backend returned non-dict response ({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "clipboard_read not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_clipboard_write(ctx: Context, text: str) -> dict:
        """Write text to the system clipboard.

        Executes on an STA thread inside the FlaUI bridge (required by
        System.Windows.Clipboard). Supports full Unicode including emoji and CJK.

        Args:
            text: The text to write to the clipboard.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.clipboard_write(text=text)
            if not isinstance(result, dict):
                return build_error_response(
                    "clipboard_write: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get("reason", "clipboard_write not supported on current backend"),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def ui_realize_virtualized_item(
        ctx: Context,
        container_automation_id: str,
        prop_name: str,
        value: str,
    ) -> dict:
        """Realize a virtualized list or grid item so it enters the visual tree.

        Virtualized lists (VirtualizingStackPanel with VirtualizationMode=Recycling)
        only create UI elements for visible rows. Items outside the viewport are
        "virtualized" — they exist in the data source but have no AutomationElement.
        This tool forces the item into the visual tree so subsequent ui_click or
        ui_find_element calls can reach it.

        Operation is idempotent: re-realizing an already-realized item is safe
        and returns {realized: true} without error.

        Returns:
            {realized: true, element_id: "...", bounding_rect: {x, y, width, height}} on success.
            {realized: false, reason: "item not found"} if the item is not in the data source.
            {realized: false, reason: "container does not support ItemContainerPattern"} if
            the container is not a virtualizing list.

        Args:
            container_automation_id: AutomationId of the list/grid container.
            prop_name: Property to search by. Supported: "AutomationId", "Name", "ClassName".
            value: Value to match against the chosen property.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if not container_automation_id:
                raise ValueError("container_automation_id is required")
            if not value:
                raise ValueError("value is required")

            supported_props = {"AutomationId", "Name", "ClassName"}
            if prop_name not in supported_props:
                raise ValueError(
                    "prop_name must be one of: "
                    f"{', '.join(sorted(supported_props))}. Got: {prop_name!r}"
                )

            ui = await _ensure_ui_connected()
            result = await ui.realize_virtualized_item(
                container_automation_id=container_automation_id,
                prop_name=prop_name,
                value=value,
            )
            if not isinstance(result, dict):
                return build_error_response(
                    "realize_virtualized_item: backend returned non-dict response "
                    f"({type(result).__name__})",
                    state=session.state.state,
                )
            if result.get("unsupported") is True:
                return build_error_response(
                    result.get(
                        "reason", "realize_virtualized_item not supported on current backend"
                    ),
                    state=session.state.state,
                )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)
