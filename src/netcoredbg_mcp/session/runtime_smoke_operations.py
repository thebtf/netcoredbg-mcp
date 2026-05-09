"""Runtime smoke operation adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..ui.focus import assert_focus
from ..ui.grid import assert_grid_rows, select_grid_range, snapshot_grid
from ..ui.key_sequence import run_scoped_key_sequence
from ..ui.list_items import invoke_list_item, toggle_list_item_child

BackendProvider = Callable[[], Awaitable[Any]]
OperationAdapterMap = dict[str, Callable[..., Awaitable[dict[str, Any]]]]
STATE_CHANGE_SETTLE_SECONDS = 0.5


def ui_operation_adapters(
    ensure_ui_connected: BackendProvider,
    *,
    session: Any | None = None,
) -> OperationAdapterMap:
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

    async def get_property(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        selector = _selector(args)
        property_name = str(args.get("property_name") or args.get("property") or "")
        text_properties = {"text", "value", "valuetext"}
        if property_name.lower() in text_properties:
            result = await backend.extract_text(**_selector_kwargs(selector))
            if _is_selector_miss(result):
                return _selector_blocked(selector, result=result)
            if not _is_backend_success(result):
                return _backend_failure_result(result, operation="ui.get_property")
            return {
                "status": "PASS",
                "property": property_name,
                "value": str(result.get("text", "")),
                "result": result,
            }

        result = await backend.find_element(**_selector_kwargs(selector))
        if _is_selector_miss(result):
            return _selector_blocked(selector, result=result)
        if not _is_backend_success(result):
            return _backend_failure_result(result, operation="ui.get_property")
        property_keys = {
            "automationid": "automationId",
            "automation_id": "automationId",
            "name": "name",
            "controltype": "controlType",
            "control_type": "controlType",
            "classname": "className",
            "class_name": "className",
        }
        key = property_keys.get(property_name.lower(), property_name)
        return {
            "status": "PASS",
            "property": property_name,
            "value": result.get(key),
            "result": result,
        }

    async def find_element(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        selector = _selector(args)
        return await backend.find_element(**_selector_kwargs(selector))

    async def set_focus(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        selector = _selector(args)
        client = getattr(backend, "client", None)
        if client is not None:
            try:
                result = await client.call("set_focus", _bridge_selector_kwargs(selector))
            except Exception as exc:
                return _adapter_blocked("ui.set_focus", str(exc))
            return result if isinstance(result, dict) else {"status": "PASS", "result": result}
        result = await assert_focus(backend, selector)
        if str(result.get("status", "PASS")).upper() != "PASS":
            return result
        return {"status": "PASS", "focused": True, "result": result}

    async def send_keys_focused(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        keys = str(args.get("keys") or "")
        send_keys = getattr(backend, "send_keys", None)
        if not callable(send_keys):
            return _adapter_blocked(
                "ui.send_keys_focused",
                "focused key input service unavailable",
            )
        result = send_keys(keys)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            result = await result
        if _is_non_pass_result(result):
            return result
        return {"status": "PASS", "keys": keys, "result": result}

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
        selector = _selector(args)
        try:
            result = await backend.invoke_element(**_selector_kwargs(selector))
            if _is_selector_miss(result):
                return _selector_blocked(selector, result=result)
            if not _should_use_invoke_fallback(result, fallback):
                return await _settle_after_state_change(result)
            primary_error = str(result.get("reason") or result)
        except Exception as exc:
            if fallback is None:
                result = {"status": "BLOCKED", "reason": str(exc)}
                if _is_selector_miss(result):
                    return _selector_blocked(selector, result=result)
                return {
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "requested": {"selector": selector},
                    "accepted": {"backend": "connected UI backend supporting ui.invoke"},
                    "next_step": "Inspect UI backend or bridge transport diagnostics.",
                    "result": result,
                }
            primary_error = str(exc)

        result = await _invoke_fallback_key_sequence(
            backend,
            fallback,
            primary_error=primary_error,
        )
        return await _settle_after_state_change(result)

    adapters: OperationAdapterMap = {
        "ui.ensure_connected": ensure_connected,
        "ui.grid.snapshot": grid_snapshot,
        "ui.grid.select_range": grid_select_range,
        "ui.grid.assert_rows": grid_assert_rows,
        "ui.list.invoke_item": list_invoke,
        "ui.list.toggle_item_child": list_toggle_child,
        "ui.focus.assert": focus_assert,
        "ui.text.assert": text_assert,
        "ui.get_property": get_property,
        "ui.find_element": find_element,
        "ui.set_focus": set_focus,
        "ui.send_keys_focused": send_keys_focused,
        "ui.invoke": invoke,
    }
    if session is not None:
        adapters.update(_session_operation_adapters(session))
    return adapters


def _session_operation_adapters(session: Any) -> OperationAdapterMap:
    async def launch(**args: Any) -> dict[str, Any]:
        launch_service = getattr(session, "launch", None)
        if launch_service is None:
            return _adapter_blocked("launch", "launch service unavailable")
        launch_args = {
            "program": str(args.get("program") or ""),
            "cwd": args.get("cwd"),
            "args": args.get("args"),
            "env": args.get("env"),
            "stop_at_entry": bool(args.get("stop_at_entry", False)),
            "pre_build": bool(args.get("pre_build", False)),
            "build_project": args.get("build_project"),
            "build_configuration": str(args.get("build_configuration") or "Debug"),
        }
        try:
            result = await launch_service(**launch_args)
        except Exception as exc:
            return _adapter_blocked("launch", str(exc))
        if _is_non_pass_result(result):
            return result
        return {"status": "PASS", "reason": "launch completed", "result": result}

    async def debug_evaluate(**args: Any) -> dict[str, Any]:
        expression = str(args.get("expression") or "")
        quick_evaluate = getattr(session, "quick_evaluate", None)
        state = getattr(getattr(session, "state", None), "state", None)
        state_value = str(getattr(state, "value", state))
        try:
            if callable(quick_evaluate) and state_value == "running":
                result = await quick_evaluate(expression)
            else:
                evaluate = getattr(session, "evaluate", None)
                if evaluate is None:
                    return _adapter_blocked(
                        "debug.evaluate",
                        "debug evaluation service unavailable",
                    )
                result = await evaluate(expression)
        except Exception as exc:
            return {
                **_adapter_blocked("debug.evaluate", str(exc)),
                "value": None,
            }
        if not isinstance(result, dict):
            return {"status": "PASS", "value": result}
        if _is_non_pass_result(result):
            return result
        if "error" in result:
            return {
                "status": "BLOCKED",
                "reason": str(result["error"]),
                "value": None,
                "result": result,
            }
        return {
            "status": "PASS",
            "value": result.get("result", result.get("value")),
            "type": result.get("type"),
            "result": result,
        }

    async def debug_stop(**args: Any) -> dict[str, Any]:
        mode = str(args.get("mode") or "graceful")
        if mode != "graceful":
            return _adapter_blocked("debug.stop", f"unsupported debug.stop mode: {mode}")
        stop = getattr(session, "stop", None)
        if stop is None:
            return _adapter_blocked("debug.stop", "debug stop service unavailable")
        try:
            result = stop()
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
        except Exception as exc:
            return {"status": "FAIL", "mode": mode, "reason": str(exc)}
        if _is_non_pass_result(result):
            return result
        return {"status": "PASS", "mode": mode, "result": result}

    async def process_registry_count(**_: Any) -> dict[str, Any]:
        registry = getattr(session, "process_registry", None)
        if registry is None:
            return _adapter_blocked(
                "process.registry.count",
                "process registry service unavailable",
            )
        try:
            registry.reap_stale()
            status = registry.status()
        except Exception as exc:
            return _adapter_blocked("process.registry.count", str(exc))
        alive = [
            entry
            for entry in status
            if bool(entry.get("alive"))
        ]
        return {"status": "PASS", "count": len(alive), "alive": alive}

    async def fixture_restore(**args: Any) -> dict[str, Any]:
        validate_path = getattr(session, "validate_path", None)
        if validate_path is None:
            return _adapter_blocked("fixture.restore", "path validation service unavailable")
        try:
            target_path = str(validate_path(str(args.get("path") or ""), must_exist=False))
        except ValueError as exc:
            return _adapter_blocked("fixture.restore", str(exc))
        baseline_file = args.get("baseline_file")
        content = args.get("baseline_text")
        source = "baseline_text"
        if content is None:
            source = "baseline_file"
            if not baseline_file:
                return _adapter_blocked(
                    "fixture.restore",
                    "fixture restore requires baseline_text or baseline_file",
                )
            try:
                source_path = str(validate_path(str(baseline_file), must_exist=True))
                content = Path(source_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as exc:
                return _adapter_blocked(
                    "fixture.restore",
                    f"fixture baseline read failed: {exc}",
                )
        if not isinstance(content, str):
            return _adapter_blocked("fixture.restore", "fixture restore content must be text")
        target = Path(target_path)
        if not target.parent.is_dir():
            return _adapter_blocked(
                "fixture.restore",
                f"restore parent directory does not exist: {target.parent}",
            )
        try:
            target.write_text(content, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _adapter_blocked(
                "fixture.restore",
                f"fixture restore write failed: {exc}",
            )
        return {
            "status": "PASS",
            "path": target_path,
            "source": source,
            "char_count": len(content),
            "byte_count": len(content.encode("utf-8")),
        }

    return {
        "launch": launch,
        "debug.evaluate": debug_evaluate,
        "debug.stop": debug_stop,
        "process.registry.count": process_registry_count,
        "fixture.restore": fixture_restore,
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


def _adapter_blocked(adapter: str, reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "requested": {"adapter": adapter},
        "accepted": {"adapter_names": [adapter]},
        "next_step": f"Connect a service adapter for {adapter}.",
    }


def _is_selector_miss(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status", "PASS")).upper()
    if status not in {"FAIL", "BLOCKED", "NOT_FOUND"}:
        return False
    if result.get("found") is False:
        return True
    reason = str(result.get("reason") or result.get("error") or "").lower()
    return any(
        marker in reason
        for marker in (
            "not found",
            "not_found",
            "no element",
            "no such element",
            "no matching element",
            "selector not found",
            "unable to find",
        )
    )


def _is_non_pass_result(result: Any) -> bool:
    if not isinstance(result, dict) or "status" not in result:
        return False
    status = str(result.get("status", "PASS")).upper()
    return status not in {"PASS", "OK", "SUCCESS"}


def _is_backend_success(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("status", "PASS")).upper() in {"PASS", "OK", "SUCCESS"}


def _backend_failure_result(result: Any, *, operation: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": f"{operation} returned non-object result",
            "result": result,
        }
    failure = dict(result)
    failure.setdefault("status", "FAIL")
    failure.setdefault("reason", f"{operation} failed")
    return failure


def _selector_blocked(
    selector: dict[str, Any],
    *,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "selector not found",
        "requested": {"selector": selector},
        "accepted": {
            "selector_keys": [
                "automation_id",
                "name",
                "control_type",
                "root_id",
                "xpath",
            ]
        },
        "next_step": "Inspect the fixture UI tree and update the selector.",
        "result": result,
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


def _bridge_selector_kwargs(selector: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if selector.get("automation_id") or selector.get("automationId"):
        params["automationId"] = selector.get("automation_id") or selector.get("automationId")
    if selector.get("name"):
        params["name"] = selector["name"]
    if selector.get("control_type") or selector.get("controlType"):
        params["controlType"] = selector.get("control_type") or selector.get("controlType")
    if selector.get("root_id") or selector.get("rootAutomationId"):
        params["rootAutomationId"] = selector.get("root_id") or selector.get("rootAutomationId")
    if selector.get("xpath"):
        params["xpath"] = selector["xpath"]
    return params
