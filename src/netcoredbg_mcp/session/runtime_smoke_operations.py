"""Runtime smoke operation adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..ui.focus import assert_focus
from ..ui.grid import assert_grid_rows, select_grid_range, snapshot_grid
from ..ui.key_sequence import run_scoped_key_sequence
from ..ui.list_items import invoke_list_item, toggle_list_item_child

BackendProvider = Callable[[], Awaitable[Any]]
OperationAdapterMap = dict[str, Callable[..., Awaitable[dict[str, Any]]]]
STATE_CHANGE_SETTLE_SECONDS = 0.5


def ui_operation_adapters(ensure_ui_connected: BackendProvider) -> OperationAdapterMap:
    """Build runtime smoke UI operation adapters."""

    async def ensure_connected(**_: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        return {
            "status": "PASS",
            "reason": "ui backend connected",
            "backend": type(backend).__name__,
        }

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
        return await assert_grid_rows(
            backend,
            _selector(args),
            list(args["rows"]),
            columns=args.get("columns"),
        )

    async def list_invoke(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        result = await invoke_list_item(
            backend,
            _selector(args),
            item=dict(args["item"]),
            invoke=str(args.get("invoke", "default")),
        )
        return await _settle_after_state_change(result)

    async def list_toggle_child(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        result = await toggle_list_item_child(
            backend,
            _selector(args),
            item=dict(args["item"]),
            child=dict(args["child"]),
            target_state=args.get("target_state"),
        )
        return await _settle_after_state_change(result)

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
        if not isinstance(text_result, dict):
            return {
                "status": "FAIL",
                "matched": False,
                "reason": "text backend returned non-object result",
                "result": text_result,
            }
        backend_status = str(text_result.get("status", "PASS")).upper()
        if backend_status not in {"PASS", "OK", "SUCCESS"}:
            result = dict(text_result)
            result.setdefault("matched", False)
            result.setdefault("result", text_result)
            return result
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
        fallback = args.get("fallback_key_sequence")
        try:
            result = await backend.invoke_element(**_selector_kwargs(_selector(args)))
            if not _should_use_invoke_fallback(result, fallback):
                return await _settle_after_state_change(result)
            primary_error = str(result.get("reason") or result)
        except Exception as exc:
            if fallback is None:
                raise
            primary_error = str(exc)

        result = await _invoke_fallback_key_sequence(
            backend,
            fallback,
            primary_error=primary_error,
        )
        return await _settle_after_state_change(result)

    return {
        "ui.ensure_connected": ensure_connected,
        "ui.grid.snapshot": grid_snapshot,
        "ui.grid.select_range": grid_select_range,
        "ui.grid.assert_rows": grid_assert_rows,
        "ui.list.invoke_item": list_invoke,
        "ui.list.toggle_item_child": list_toggle_child,
        "ui.focus.assert": focus_assert,
        "ui.text.assert": text_assert,
        "ui.invoke": invoke,
    }


async def _settle_after_state_change(result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status", "PASS")).upper()
    if status not in {"PASS", "OK", "SUCCESS"}:
        return result

    await asyncio.sleep(STATE_CHANGE_SETTLE_SECONDS)
    settled = dict(result)
    settled.setdefault("settled_ms", int(STATE_CHANGE_SETTLE_SECONDS * 1000))
    return settled


async def _backend_or_blocked(ensure_ui_connected: BackendProvider) -> Any:
    last_error: Exception | None = None
    for delay in (0.0, 0.1, 0.2, 0.5, 1.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await ensure_ui_connected()
        except Exception as exc:
            last_error = exc

    assert last_error is not None
    return {
        "status": "BLOCKED",
        "reason": str(last_error),
        "operation": "ui backend connect",
    }


def _selector(args: dict[str, Any]) -> dict[str, Any]:
    if "selector" not in args:
        return {}
    selector = args["selector"]
    if not isinstance(selector, dict):
        raise TypeError("selector must be an object when provided")
    return dict(selector)


def _should_use_invoke_fallback(result: Any, fallback: Any) -> bool:
    if fallback is None or not isinstance(result, dict):
        return False
    return str(result.get("status", "")).upper() in {
        "FAIL",
        "BLOCKED",
        "UNSUPPORTED",
        "AMBIGUOUS",
    }


async def _invoke_fallback_key_sequence(
    backend: Any,
    fallback: Any,
    *,
    primary_error: str,
) -> dict[str, Any]:
    if not isinstance(fallback, dict):
        return {
            "status": "FAIL",
            "reason": "fallback_key_sequence must be an object",
            "primary_error": primary_error,
        }

    selector = _fallback_selector(fallback)
    if isinstance(selector, dict) and selector.get("status") == "FAIL":
        return {
            **selector,
            "primary_error": primary_error,
        }
    modifiers = fallback.get("modifiers") or []
    keys = fallback.get("keys") or []
    if not isinstance(modifiers, list) or not isinstance(keys, list):
        return {
            "status": "FAIL",
            "reason": "fallback_key_sequence modifiers and keys must be lists",
            "primary_error": primary_error,
        }

    fallback_result = await run_scoped_key_sequence(
        backend,
        selector,
        modifiers=[str(item) for item in modifiers],
        keys=[str(item) for item in keys],
    )
    status = str(fallback_result.get("status", "PASS")).upper()
    return {
        "status": status,
        "invoked": status == "PASS",
        "method": "fallback_key_sequence",
        "primary_error": primary_error,
        "fallback": fallback_result,
    }


def _fallback_selector(fallback: dict[str, Any]) -> dict[str, Any]:
    selector_source = fallback.get("selector")
    if selector_source is not None:
        if not isinstance(selector_source, dict):
            return {
                "status": "FAIL",
                "reason": "fallback_key_sequence.selector must be an object",
            }
        return dict(selector_source)

    return {
        key: fallback[key]
        for key in (
            "automation_id",
            "automationId",
            "name",
            "control_type",
            "controlType",
            "root_id",
            "rootAutomationId",
            "xpath",
        )
        if fallback.get(key)
    }


def _selector_kwargs(selector: dict[str, Any]) -> dict[str, Any]:
    return {
        "automation_id": selector.get("automation_id") or selector.get("automationId"),
        "name": selector.get("name"),
        "control_type": selector.get("control_type") or selector.get("controlType"),
        "root_id": selector.get("root_id") or selector.get("rootAutomationId"),
        "xpath": selector.get("xpath"),
    }
