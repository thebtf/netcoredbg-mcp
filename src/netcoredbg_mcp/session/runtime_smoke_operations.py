"""Runtime smoke operation adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from ..ui.focus import assert_focus
from ..ui.grid import assert_grid_rows, read_grid_selected_rows, select_grid_range, snapshot_grid
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

    async def grid_viewport(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        selector = _selector(args)
        identity = dict(args.get("identity") or {})
        rows = dict(args.get("rows") or {})
        expect = dict(args.get("expect") or {})
        columns = _viewport_columns(identity)
        result = await snapshot_grid(backend, selector, rows=rows, columns=columns)
        if _is_non_pass_result(result):
            return cast(dict[str, Any], result)
        if not isinstance(result, Mapping):
            return _viewport_blocked(
                reason="grid viewport snapshot returned non-object result",
                selector=selector,
            )
        visible_rows = result.get("visible_rows")
        if not isinstance(visible_rows, list):
            return _viewport_blocked(
                reason="grid viewport visible row evidence unavailable",
                selector=selector,
            )

        selected_rows = _selected_viewport_rows_from_visible(visible_rows, identity)
        if expect.get("selected_payload_preserved") is True and not selected_rows:
            selected_rows, blocked = await _selected_viewport_rows_from_backend(
                backend,
                selector,
                identity,
            )
            if blocked is not None:
                return blocked

        return {
            "status": "PASS",
            "snapshot": _viewport_snapshot_from_rows(
                result,
                visible_rows=visible_rows,
                selected_rows=selected_rows,
                identity=identity,
            ),
            "phase": args.get("phase"),
            "probe_name": args.get("probe_name"),
        }

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
            try:
                result = await backend.extract_text(**_selector_kwargs(selector))
            except Exception as exc:
                return _adapter_blocked("ui.get_property", str(exc))
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

        try:
            result = await backend.find_element(**_selector_kwargs(selector))
        except Exception as exc:
            return _adapter_blocked("ui.get_property", str(exc))
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
        try:
            return cast(dict[str, Any], await backend.find_element(**_selector_kwargs(selector)))
        except Exception as exc:
            return _adapter_blocked("ui.find_element", str(exc))

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
            return cast(dict[str, Any], result)
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

    async def drag(**args: Any) -> dict[str, Any]:
        backend = await _backend_or_blocked(ensure_ui_connected)
        if isinstance(backend, dict):
            return backend
        backend_drag = getattr(backend, "drag", None)
        if not callable(backend_drag):
            return _adapter_blocked("ui.drag", "drag backend unavailable")

        source = dict(args.get("source") or {})
        path = _mapping_list(args.get("path"))
        drop = dict(args.get("drop") or {})
        route, route_evidence, blocked = await _drag_route(
            backend,
            source=source,
            path=path,
            drop=drop,
        )
        if blocked is not None:
            return blocked

        modifiers = [str(modifier) for modifier in args.get("modifiers") or []]
        speed_ms = _positive_int(args.get("duration_ms"), default=200)
        try:
            result = await backend_drag(
                route["from_x"],
                route["from_y"],
                route["to_x"],
                route["to_y"],
                speed_ms=speed_ms,
                hold_modifiers=modifiers,
            )
        except Exception as exc:
            return _adapter_blocked("ui.drag", str(exc))
        if not isinstance(result, dict):
            return {
                "status": "FAIL",
                "reason": "ui.drag backend returned non-object result",
                "result": result,
            }

        route_evidence = {
            **route_evidence,
            "move_points": path,
            "hold_points": [point for point in path if "hold_ms" in point],
            "final_pointer": drop
            if _screen_point(drop) is not None
            else route_evidence.get("target_point")
            or drop
            or (path[-1] if path else None),
            "modifiers": modifiers,
            "start": {"x": route["from_x"], "y": route["from_y"]},
            "drop": {"x": route["to_x"], "y": route["to_y"]},
        }
        status = str(result.get("status", "PASS")).upper()
        output: dict[str, Any] = {
            "status": status,
            "backend": type(backend).__name__,
            "route_evidence": route_evidence,
            "result": result,
        }
        if status != "PASS":
            for key in ("reason", "requested", "accepted", "next_step"):
                if key in result:
                    output[key] = result[key]
        return output

    adapters: OperationAdapterMap = {
        "ui.ensure_connected": ensure_connected,
        "ui.grid.snapshot": grid_snapshot,
        "ui.grid.viewport": grid_viewport,
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
        "ui.drag": drag,
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
            return cast(dict[str, Any], result)
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
            return cast(dict[str, Any], result)
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
        alive = [entry for entry in status if bool(entry.get("alive"))]
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


async def _drag_route(
    backend: Any,
    *,
    source: dict[str, Any],
    path: list[dict[str, Any]],
    drop: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any], dict[str, Any] | None]:
    source_point, source_evidence, blocked = await _resolve_drag_endpoint(
        backend,
        source,
        role="source",
    )
    if blocked is not None:
        return {}, {}, blocked

    target_payload = drop or (path[-1] if path else {})
    target_point, target_evidence, blocked = await _resolve_drag_endpoint(
        backend,
        target_payload,
        role="target",
    )
    if blocked is not None:
        return {}, {}, blocked

    route = {
        "from_x": source_point["x"],
        "from_y": source_point["y"],
        "to_x": target_point["x"],
        "to_y": target_point["y"],
    }
    return (
        route,
        {
            "source": source,
            "target": target_payload,
            "source_bounds": source_evidence.get("bounds"),
            "target_bounds": target_evidence.get("bounds"),
            "source_identity": source_evidence.get("identity"),
            "target_identity": target_evidence.get("identity"),
            "source_point": source_point,
            "target_point": target_point,
        },
        None,
    )


async def _resolve_drag_endpoint(
    backend: Any,
    endpoint: dict[str, Any],
    *,
    role: str,
) -> tuple[dict[str, int], dict[str, Any], dict[str, Any] | None]:
    point = _screen_point(endpoint)
    if point is not None:
        return {"x": point[0], "y": point[1]}, {"point": endpoint}, None

    kind = str(endpoint.get("kind") or _endpoint_kind(endpoint) or "")
    if kind == "point":
        point = _screen_point(endpoint.get("point"))
        if point is None:
            return {}, {}, _drag_blocked(
                reason=f"drag {role} requires screen coordinates",
                requested={role: endpoint},
                accepted={f"{role}.point": "screen coordinate object"},
                next_step=f"Provide {role}.point using relative_to: screen.",
            )
        return {"x": point[0], "y": point[1]}, {"point": endpoint.get("point")}, None

    if kind == "selector":
        selector = dict(endpoint.get("selector") or endpoint)
        return await _resolve_selector_endpoint(backend, selector, role=role)

    if kind in {"row_index", "row_identity"}:
        selector = dict(endpoint.get("selector") or {})
        if not selector:
            return {}, {}, _drag_blocked(
                reason=f"drag {role} row source requires selector",
                requested={role: endpoint},
                accepted={f"{role}.selector": "grid selector for visible row lookup"},
                next_step=f"Provide {role}.selector with {role}.{kind}.",
            )
        snapshot, blocked = await _grid_snapshot_for_drag(backend, selector, role=role)
        if blocked is not None:
            return {}, {}, blocked
        row, blocked = _row_from_drag_endpoint(snapshot, endpoint, role=role, kind=kind)
        if blocked is not None:
            return {}, {}, blocked
        bounds = _bounds_from_mapping(row)
        if bounds is None:
            return {}, {}, _drag_blocked(
                reason=f"drag {role} row bounds unavailable",
                requested={role: endpoint},
                accepted={"row.bounds": "visible row bounding rectangle"},
                next_step="Use a UI backend that returns row bounds in grid snapshot evidence.",
            )
        return (
            _center_point(bounds),
            {"bounds": bounds, "identity": _row_identity(row), "row": _compact_row_ref(row)},
            None,
        )

    return {}, {}, _drag_blocked(
        reason=f"drag {role} requires coordinate resolution",
        requested={role: endpoint},
        accepted={
            f"{role}.kind": "point, selector, row_index, or row_identity with resolvable bounds"
        },
        next_step="Provide a resolvable drag endpoint for ui.drag.",
    )


async def _resolve_selector_endpoint(
    backend: Any,
    selector: dict[str, Any],
    *,
    role: str,
) -> tuple[dict[str, int], dict[str, Any], dict[str, Any] | None]:
    find_element = getattr(backend, "find_element", None)
    if not callable(find_element):
        return {}, {}, _drag_blocked(
            reason=f"drag {role} selector lookup unavailable",
            requested={role: selector},
            accepted={"backend": "find_element-capable UI backend"},
            next_step="Use a UI backend that can resolve selectors to element bounds.",
        )
    result = await find_element(**_selector_kwargs(selector))
    if not isinstance(result, Mapping) or not result.get("found", True):
        return {}, {}, _drag_blocked(
            reason=f"drag {role} selector not found",
            requested={role: selector},
            accepted={"selector": "unique visible element selector"},
            next_step="Update the selector so it resolves to one visible element.",
        )
    bounds = _bounds_from_mapping(result)
    if bounds is None:
        return {}, {}, _drag_blocked(
            reason=f"drag {role} selector bounds unavailable",
            requested={role: selector},
            accepted={"selector.bounds": "element bounding rectangle"},
            next_step="Use a UI backend that returns element bounds.",
        )
    return _center_point(bounds), {"bounds": bounds, "identity": _selector_identity(result)}, None


async def _grid_snapshot_for_drag(
    backend: Any,
    selector: dict[str, Any],
    *,
    role: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    grid_snapshot = getattr(backend, "grid_snapshot", None)
    if callable(grid_snapshot):
        result = await grid_snapshot(
            selector,
            rows={"visible_only": True},
            columns=["Start", "End", "Character", "Phrase"],
        )
    else:
        grid_visible_rows = getattr(backend, "grid_visible_rows", None)
        if not callable(grid_visible_rows):
            return {}, _drag_blocked(
                reason=f"drag {role} row lookup unavailable",
                requested={role: {"selector": selector}},
                accepted={"backend": "grid_snapshot or grid_visible_rows capable UI backend"},
                next_step="Use a UI backend with visible row evidence.",
            )
        result = await grid_visible_rows(selector)
    if not isinstance(result, Mapping):
        return {}, _drag_blocked(
            reason=f"drag {role} grid lookup returned non-object result",
            requested={role: {"selector": selector}},
            accepted={"grid_result": "object with visible_rows"},
            next_step="Inspect the UI backend grid snapshot implementation.",
        )
    status = str(result.get("status", "PASS")).upper()
    if status not in {"PASS", "OK", "SUCCESS"}:
        return {}, _drag_blocked(
            reason=str(result.get("reason") or f"drag {role} grid lookup failed"),
            requested={role: {"selector": selector}},
            accepted={"grid_result": "PASS with visible_rows"},
            next_step="Resolve the grid selector or backend capability before dragging.",
        )
    return dict(result), None


def _row_from_drag_endpoint(
    snapshot: dict[str, Any],
    endpoint: dict[str, Any],
    *,
    role: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    rows = snapshot.get("visible_rows")
    if not isinstance(rows, list):
        return {}, _drag_blocked(
            reason=f"drag {role} visible row evidence unavailable",
            requested={role: endpoint},
            accepted={"visible_rows": "list of visible row objects"},
            next_step="Use a grid backend that returns visible row evidence.",
        )

    if kind == "row_index":
        try:
            raw_row_index = endpoint.get("row_index")
            if raw_row_index is None:
                raise ValueError("missing row_index")
            row_index = int(raw_row_index)
        except (TypeError, ValueError):
            row_index = -1
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                raw_visible_index = row.get("index")
                if raw_visible_index is None:
                    raise ValueError("missing row index")
                visible_index = int(raw_visible_index)
            except (TypeError, ValueError):
                visible_index = -1
            if visible_index == row_index:
                return dict(row), None
        return {}, _drag_blocked(
            reason=f"drag {role} row index not visible",
            requested={role: endpoint},
            accepted={"row_index": "currently visible row index"},
            next_step="Scroll the grid or choose a visible row index before dragging.",
        )

    requested_identity = str(endpoint.get("row_identity") or "")
    matches = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and _row_matches_identity(row, requested_identity)
    ]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return {}, _drag_blocked(
            reason=f"drag {role} row identity not visible",
            requested={role: endpoint},
            accepted={"row_identity": "visible row identity"},
            next_step="Use a visible row identity or scroll the grid before dragging.",
        )
    return {}, _drag_blocked(
        reason="duplicate row identity",
        requested={"row_identity": requested_identity},
        accepted={"row_identity": "unique visible row identity"},
        next_step="Disambiguate the row with row_index or cached_element.",
    )


def _endpoint_kind(endpoint: dict[str, Any]) -> str | None:
    for key in ("row_index", "row_identity", "cached_element", "point"):
        if endpoint.get(key) is not None:
            return key
    if endpoint.get("selector") or endpoint.get("automation_id") or endpoint.get("automationId"):
        return "selector"
    return None


def _row_matches_identity(row: Mapping[str, Any], identity: str) -> bool:
    if not identity:
        return False
    candidates = {
        _row_identity(row),
        str(row.get("automation_id") or ""),
        str(row.get("name") or ""),
    }
    cells = row.get("cells")
    if isinstance(cells, Mapping):
        candidates.update(str(value) for value in cells.values())
    cell_values = row.get("cell_values")
    if isinstance(cell_values, list):
        for cell in cell_values:
            if isinstance(cell, Mapping):
                candidates.add(str(cell.get("text") or ""))
    return identity in candidates


def _row_identity(row: Mapping[str, Any]) -> str:
    cells = row.get("cells")
    if isinstance(cells, Mapping):
        for key in ("Phrase", "Start", "Character", "End"):
            if cells.get(key):
                return str(cells[key])
    if row.get("automation_id"):
        return str(row["automation_id"])
    if row.get("name"):
        return str(row["name"])
    return f"row:{row.get('index')}"


def _compact_row_ref(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": row.get("index"),
        "automation_id": row.get("automation_id"),
        "identity": _row_identity(row),
    }


def _viewport_columns(identity: Mapping[str, Any]) -> list[str]:
    column = identity.get("column")
    return [str(column)] if column else []


def _viewport_snapshot_from_rows(
    result: Mapping[str, Any],
    *,
    visible_rows: list[Any],
    selected_rows: list[dict[str, Any]],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    compact_rows = [
        _viewport_row_ref(row, identity)
        for row in visible_rows
        if isinstance(row, Mapping)
    ]
    indices: list[int] = []
    for row in compact_rows:
        index = row.get("index")
        if isinstance(index, int):
            indices.append(index)
    identity_strategy = _viewport_identity_strategy(identity, compact_rows)
    return {
        "first_visible_index": min(indices) if indices else None,
        "last_visible_index": max(indices) if indices else None,
        "visible_rows": compact_rows,
        "selected_rows": selected_rows,
        "row_count": result.get("row_count"),
        "identity_strategy": identity_strategy,
    }


def _viewport_row_ref(row: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    row_identity, derived = _viewport_row_identity(row, identity)
    return {
        "index": _int_or_none(row.get("index")),
        "identity": row_identity,
        "derived": derived,
    }


def _viewport_row_identity(
    row: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> tuple[str, bool]:
    column = identity.get("column")
    cells = row.get("cells")
    if column and isinstance(cells, Mapping) and cells.get(str(column)):
        return str(cells[str(column)]), False
    for key in ("stable_id", "id", "automation_id", "name"):
        if row.get(key):
            return str(row[key]), False
    if isinstance(cells, Mapping):
        values = [str(value) for value in cells.values() if value]
        if values:
            return "|".join(values), False
    return f"row:{row.get('index')}", True


def _viewport_identity_strategy(
    identity: Mapping[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    column = identity.get("column")
    strategy: dict[str, Any] = (
        {"kind": "configured_column", "column": str(column)}
        if column
        else {"kind": "row_evidence"}
    )
    if any(bool(row.get("derived")) for row in rows):
        strategy["derived"] = True
    return strategy


def _selected_viewport_rows_from_visible(
    visible_rows: list[Any],
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        _viewport_row_ref(row, identity)
        for row in visible_rows
        if isinstance(row, Mapping) and bool(row.get("selected"))
    ]


async def _selected_viewport_rows_from_backend(
    backend: Any,
    selector: dict[str, Any],
    identity: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    grid_selected_rows = getattr(backend, "grid_selected_rows", None)
    if not callable(grid_selected_rows):
        return [], _viewport_blocked(
            reason="selected row evidence unavailable",
            selector=selector,
        )
    result = await read_grid_selected_rows(backend, selector)
    if _is_non_pass_result(result):
        return [], {
            **dict(result),
            "status": "BLOCKED",
            "reason": str(result.get("reason") or "selected row evidence unavailable"),
        }
    selected_rows = result.get("selected_rows")
    if not isinstance(selected_rows, list):
        return [], _viewport_blocked(
            reason="selected row evidence unavailable",
            selector=selector,
        )
    return [
        _viewport_row_ref(row, identity)
        for row in selected_rows
        if isinstance(row, Mapping)
    ], None


def _viewport_blocked(
    *,
    reason: str,
    selector: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "requested": {"selector": selector, "probe": "ui.grid.viewport"},
        "accepted": {
            "selected_rows": "before and after selected row identities",
            "visible_rows": "visible row identities with row bounds or indices",
        },
        "next_step": "Use a UI backend that returns grid viewport and selected row evidence.",
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _selector_identity(result: Mapping[str, Any]) -> str:
    return str(
        result.get("automationId")
        or result.get("automation_id")
        or result.get("name")
        or ""
    )


def _bounds_from_mapping(value: Mapping[str, Any]) -> dict[str, int] | None:
    raw = value.get("bounds") or value.get("rect")
    if not isinstance(raw, Mapping):
        return None
    try:
        bounds = {
            "x": int(round(float(raw["x"]))),
            "y": int(round(float(raw["y"]))),
            "width": int(round(float(raw["width"]))),
            "height": int(round(float(raw["height"]))),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        return None
    return bounds


def _center_point(bounds: Mapping[str, int]) -> dict[str, int]:
    return {
        "x": int(bounds["x"] + bounds["width"] / 2),
        "y": int(bounds["y"] + bounds["height"] / 2),
    }


def _point_drag_route(
    *,
    source: dict[str, Any],
    path: list[dict[str, Any]],
    drop: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any] | None]:
    if source.get("kind") != "point":
        return {}, _drag_blocked(
            reason="drag source requires coordinate resolution",
            requested={"source": source},
            accepted={"source.kind": "point with screen coordinates"},
            next_step="Resolve row or selector sources to screen coordinates before ui.drag.",
        )

    start = _screen_point(source.get("point"))
    end = _screen_point(drop) or _screen_point(path[-1] if path else None)
    if start is None or end is None:
        return {}, _drag_blocked(
            reason="drag route requires screen coordinates",
            requested={"source": source, "path": path, "drop": drop},
            accepted={
                "source.point": "screen coordinate object",
                "drop": "screen coordinate object or final screen waypoint",
            },
            next_step="Provide source.point and drop using relative_to: screen.",
        )
    return {
        "from_x": start[0],
        "from_y": start[1],
        "to_x": end[0],
        "to_y": end[1],
    }, None


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _screen_point(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    if str(value.get("relative_to") or "screen") != "screen":
        return None
    try:
        return int(round(float(value["x"]))), int(round(float(value["y"])))
    except (KeyError, TypeError, ValueError):
        return None


def _positive_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, candidate)


def _drag_blocked(
    *,
    reason: str,
    requested: dict[str, Any],
    accepted: dict[str, Any],
    next_step: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "requested": requested,
        "accepted": accepted,
        "next_step": next_step,
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
