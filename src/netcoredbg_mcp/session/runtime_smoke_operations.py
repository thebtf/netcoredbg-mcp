"""Runtime smoke operation adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..ui.focus import assert_focus
from ..ui.grid import assert_grid_rows, select_grid_range, snapshot_grid
from ..ui.list_items import invoke_list_item, toggle_list_item_child

BackendProvider = Callable[[], Awaitable[Any]]
OperationAdapterMap = dict[str, Callable[..., Awaitable[dict[str, Any]]]]


def ui_operation_adapters(ensure_ui_connected: BackendProvider) -> OperationAdapterMap:
    """Build runtime smoke UI operation adapters."""

    async def grid_snapshot(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await snapshot_grid(
            backend,
            _selector(args),
            rows=args.get("rows"),
            columns=args.get("columns"),
        )

    async def grid_select_range(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await select_grid_range(
            backend,
            _selector(args),
            int(args["start_index"]),
            int(args["end_index"]),
        )

    async def grid_assert_rows(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await assert_grid_rows(backend, _selector(args), list(args["rows"]))

    async def list_invoke(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await invoke_list_item(
            backend,
            _selector(args),
            item=dict(args["item"]),
            invoke=str(args.get("invoke", "default")),
        )

    async def list_toggle_child(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await toggle_list_item_child(
            backend,
            _selector(args),
            item=dict(args["item"]),
            child=dict(args["child"]),
            target_state=args.get("target_state"),
        )

    async def focus_assert(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await assert_focus(backend, _selector(args))

    async def text_assert(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        selector = _selector(args)
        text_result = await backend.extract_text(**_selector_kwargs(selector))
        text = str(text_result.get("text", ""))
        contains = args.get("contains")
        equals = args.get("equals")
        must_exist = bool(args.get("must_exist", True))
        if must_exist and not text:
            return {
                "status": "FAIL",
                "matched": False,
                "reason": "text target did not exist or had no text",
                "result": text_result,
            }
        if contains is not None and str(contains) not in text:
            return {
                "status": "FAIL",
                "matched": False,
                "reason": "text did not contain expected value",
                "expected": str(contains),
                "actual": text,
            }
        if equals is not None and str(equals) != text:
            return {
                "status": "FAIL",
                "matched": False,
                "reason": "text did not equal expected value",
                "expected": str(equals),
                "actual": text,
            }
        return {"status": "PASS", "matched": True, "text": text, "result": text_result}

    async def invoke(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return await backend.invoke_element(**_selector_kwargs(_selector(args)))

    return {
        "ui.grid.snapshot": grid_snapshot,
        "ui.grid.select_range": grid_select_range,
        "ui.grid.assert_rows": grid_assert_rows,
        "ui.list.invoke_item": list_invoke,
        "ui.list.toggle_item_child": list_toggle_child,
        "ui.focus.assert": focus_assert,
        "ui.text.assert": text_assert,
        "ui.invoke": invoke,
    }

async def _backend_or_blocked(ensure_ui_connected: BackendProvider) -> Any:
    try:
        return await ensure_ui_connected()
    except Exception as exc:
        return {
            "status": "BLOCKED",
            "reason": str(exc),
            "operation": "ui backend connect",
        }


def _selector(args: dict[str, Any]) -> dict[str, Any]:
    if "selector" not in args:
        return {}
    selector = args["selector"]
    if not isinstance(selector, dict):
        raise TypeError("selector must be an object when provided")
    return dict(selector)


def _selector_kwargs(selector: dict[str, Any]) -> dict[str, Any]:
    return {
        "automation_id": selector.get("automation_id") or selector.get("automationId"),
        "name": selector.get("name"),
        "control_type": selector.get("control_type") or selector.get("controlType"),
        "root_id": selector.get("root_id") or selector.get("rootAutomationId"),
        "xpath": selector.get("xpath"),
    }
