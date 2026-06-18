from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..blocked import build_blocked
from ..timing import sleep_ms
from .ui_drag import handle_ui_drag
from .ui_key_sequence import handle_ui_key_sequence
from .ui_text_input import handle_ui_text_type_replace_selection

ActionHandler = Callable[[dict[str, Any], "ActionContext"], Awaitable[dict[str, Any]]]
_INTEGER_TEXT = re.compile(r"-?\d+")

_ACTION_REGISTRY: dict[str, ActionHandler] = {}


@dataclass(frozen=True)
class ActionContext:
    service_adapters: dict[str, Callable[..., Any]]
    clock: Callable[[], float]
    session: Any | None = None
    diagnostic_launch: dict[str, Any] | None = None

    async def call_adapter(self, name: str, **kwargs: Any) -> dict[str, Any]:
        adapter = self.service_adapters.get(name)
        if adapter is None:
            blocked = build_blocked(
                reason="service adapter not available",
                requested={"adapter": name},
                accepted={"adapter_names": sorted(self.service_adapters)},
                next_step="Connect a service adapter that exposes the requested route.",
            )
            return {"status": "BLOCKED", **blocked}
        result = adapter(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"status": "PASS", "value": result}

    def elapsed_ms(self, started: float) -> int:
        return int(max(0.0, self.clock() - started) * 1000)


def register_action(kind: str, handler: ActionHandler) -> None:
    if not kind:
        raise ValueError("action kind is required")
    _ACTION_REGISTRY[kind] = handler


def accepted_action_kinds() -> list[str]:
    return sorted(_ACTION_REGISTRY)


async def dispatch_action(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    kind = str(action.get("kind") or "")
    handler = _ACTION_REGISTRY.get(kind)
    if handler is None:
        blocked = build_blocked(
            reason="unsupported action kind",
            requested={"kind": kind},
            accepted={"action_kinds": accepted_action_kinds()},
            next_step="Use one of the accepted action kinds.",
        )
        return {"status": "BLOCKED", **blocked}
    return await handler(action, context)


async def _handle_ui_invoke(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "invoke",
        }
    result = await context.call_adapter("ui.invoke", selector=selector)
    return _action_result(
        status=result.get("status", "PASS"),
        route="invoke",
        selector=selector,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_click(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "click",
        }
    result = await context.call_adapter("ui.click", selector=selector)
    return _action_result(
        status=result.get("status", "PASS"),
        route="click",
        selector=selector,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_input_ensure_target(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "input_ensure_target",
        }
    require, blocked = _target_requirements_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "input_ensure_target",
        }
    target = await _ensure_input_target(
        selector,
        require=require,
        context=context,
        started=started,
        route="input_ensure_target",
    )
    return target


async def _handle_ui_click_verified(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "click_verified",
        }
    postcondition, blocked = _postcondition_from_action(action, default_selector=selector)
    if blocked is not None:
        return {
            "status": "BLOCKED",
            **blocked,
            "route": "click_verified",
            "selector": selector,
            "duration_ms": context.elapsed_ms(started),
        }
    target_result = await _ensure_input_target(
        selector,
        require={"visible": True, "enabled": True, "focus": True},
        context=context,
        started=started,
        route="click_verified",
    )
    if target_result.get("status") != "PASS":
        target_result["route"] = "click_verified"
        return target_result

    click_result = await context.call_adapter("ui.click", selector=selector)
    if not _is_adapter_success(click_result):
        failed = _adapter_failure_result(
            click_result,
            route="click_verified",
            duration_ms=context.elapsed_ms(started),
            default_reason="failed to click verified target",
        )
        failed["selector"] = selector
        failed["target"] = target_result["target"]
        failed["click"] = _bounded_result(click_result)
        return failed

    click_blocked = _click_evidence_failure(click_result)
    if click_blocked is not None:
        return {
            "status": "BLOCKED",
            **click_blocked,
            "route": "click_verified",
            "selector": selector,
            "target": target_result["target"],
            "click": _bounded_result(click_result),
            "duration_ms": context.elapsed_ms(started),
        }
    post_result = await _verify_postcondition(
        postcondition,
        context=context,
        started=started,
        route="click_verified",
    )
    if post_result.get("status") != "PASS":
        post_result["selector"] = selector
        post_result["target"] = target_result["target"]
        post_result["click"] = _bounded_result(click_result)
        return post_result

    return {
        "status": "PASS",
        "route": "click_verified",
        "selector": selector,
        "target": target_result["target"],
        "click": _bounded_result(click_result),
        "postcondition": post_result["postcondition"],
        "duration_ms": context.elapsed_ms(started),
    }


async def _handle_ui_grid_select(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select",
        }
    row_identities, blocked = _row_identities_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select",
        }
    if row_identities:
        identity = _mapping_from_action(action, "identity")
        rows = _mapping_from_action(action, "rows")
        columns = _list_from_action(action, "columns")
        result = await context.call_adapter(
            "ui.grid.select_identities",
            selector=selector,
            row_identities=row_identities,
            identity=identity,
            rows=rows,
            columns=columns,
        )
        return _action_result(
            status=result.get("status", "PASS"),
            route="grid_select",
            selector=selector,
            row_identities=row_identities,
            identity=identity,
            rows=rows,
            columns=columns,
            duration_ms=context.elapsed_ms(started),
            result=result,
        )
    indices, blocked = _indices_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select",
        }
    result = await context.call_adapter(
        "ui.grid.select_indices",
        selector=selector,
        indices=indices,
    )
    return _action_result(
        status=result.get("status", "PASS"),
        route="grid_select",
        selector=selector,
        indices=indices,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_grid_get_state(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_get_state",
        }
    identity = _mapping_from_action(action, "identity")
    rows = _mapping_from_action(action, "rows")
    columns = _list_from_action(action, "columns")
    result = await context.call_adapter(
        "ui.grid.get_state",
        selector=selector,
        identity=identity,
        rows=rows,
        columns=columns,
    )
    return _action_result(
        status=result.get("status", "PASS"),
        route="grid_get_state",
        selector=selector,
        identity=identity,
        rows=rows,
        columns=columns,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_grid_select_row(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select_row",
        }
    row, blocked = _row_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select_row",
        }
    identity = _mapping_from_action(action, "identity")
    rows = _mapping_from_action(action, "rows")
    columns = _list_from_action(action, "columns")
    result = await context.call_adapter(
        "ui.grid.select_row",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
    )
    return _action_result(
        status=result.get("status", "PASS"),
        route="grid_select_row",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_grid_ensure_visible(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_ensure_visible",
        }
    row, blocked = _row_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_ensure_visible",
        }
    identity = _mapping_from_action(action, "identity")
    rows = _mapping_from_action(action, "rows")
    columns = _list_from_action(action, "columns")
    max_scrolls = action.get("max_scrolls")
    scroll_settle_ms = action.get("scroll_settle_ms")
    result = await context.call_adapter(
        "ui.grid.ensure_visible",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
        max_scrolls=max_scrolls,
        scroll_settle_ms=scroll_settle_ms,
    )
    return _action_result(
        status=result.get("status", "PASS"),
        route="grid_ensure_visible",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
        max_scrolls=max_scrolls,
        scroll_settle_ms=scroll_settle_ms,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_ui_grid_click_row(
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_click_row",
        }
    row, blocked = _row_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_click_row",
        }
    identity = _mapping_from_action(action, "identity")
    rows = _mapping_from_action(action, "rows")
    columns = _list_from_action(action, "columns")
    column = action.get("column")
    result = await context.call_adapter(
        "ui.grid.click_row",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
        column=str(column) if column is not None else None,
    )
    return _action_result(
        status=result.get("status", "PASS"),
        route="grid_click_row",
        selector=selector,
        row=row,
        identity=identity,
        rows=rows,
        columns=columns,
        column=str(column) if column is not None else None,
        duration_ms=context.elapsed_ms(started),
        result=result,
    )


async def _handle_wait(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    idle_ms, blocked = _idle_ms_from_action(action)
    if blocked is not None:
        return {**blocked, "duration_ms": context.elapsed_ms(started), "route": "wait"}
    await _sleep_ms(context, idle_ms)
    return {
        "status": "PASS",
        "route": "wait",
        "idle_ms": idle_ms,
        "duration_ms": context.elapsed_ms(started),
    }


async def _handle_noop(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    return {
        "status": "PASS",
        "route": "noop",
        "duration_ms": context.elapsed_ms(started),
    }


async def _ensure_input_target(
    selector: dict[str, Any],
    *,
    require: dict[str, Any],
    context: ActionContext,
    started: float,
    route: str,
) -> dict[str, Any]:
    find_result = await context.call_adapter("ui.find_element", selector=selector)
    if find_result.get("found") is False:
        blocked = build_blocked(
            reason="selector not found",
            requested={"selector": selector},
            accepted={"selector": "visible enabled UI target"},
            next_step="Refresh the UI tree and provide a selector that resolves uniquely.",
        )
        return {
            "status": "BLOCKED",
            **blocked,
            "route": route,
            "selector": selector,
            "duration_ms": context.elapsed_ms(started),
        }
    if not _is_adapter_success(find_result):
        failed = _adapter_failure_result(
            find_result,
            route=route,
            duration_ms=context.elapsed_ms(started),
            default_reason="failed to find target element",
        )
        failed["selector"] = selector
        return failed

    target = _target_evidence(selector, find_result)
    blocked = _target_requirement_failure(target, require)
    if blocked is not None:
        return {
            "status": "BLOCKED",
            **blocked,
            "route": route,
            "selector": selector,
            "target": target,
            "duration_ms": context.elapsed_ms(started),
        }

    if require.get("focus") is True:
        focus_result = await context.call_adapter("ui.set_focus", selector=selector)
        if not _is_adapter_success(focus_result):
            failed = _adapter_failure_result(
                focus_result,
                route=route,
                duration_ms=context.elapsed_ms(started),
                default_reason="failed to focus target element",
            )
            failed["selector"] = selector
            failed["target"] = target
            return failed
        focus = _bounded_result(focus_result)
        focus_within = focus.get("focus_within")
        focused = focus.get("focused")
        if focus_within is False or focused is False:
            blocked = build_blocked(
                reason="target focus evidence failed",
                requested={"focus_within": focus_within, "focused": focused},
                accepted={"focus_within": True, "focused": True},
                next_step="Ensure focus remains inside the requested input target.",
            )
            return {
                "status": "BLOCKED",
                **blocked,
                "route": route,
                "selector": selector,
                "target": {**target, "focus": focus},
                "duration_ms": context.elapsed_ms(started),
            }
        if focus_within is not True and focused is not True:
            blocked = build_blocked(
                reason="target focus evidence missing",
                requested={"focus_within": focus_within, "focused": focused},
                accepted={"focus_within": True, "focused": True},
                next_step="Return positive focus evidence after focusing the input target.",
            )
            return {
                "status": "BLOCKED",
                **blocked,
                "route": route,
                "selector": selector,
                "target": {**target, "focus": focus},
                "duration_ms": context.elapsed_ms(started),
            }
        target["focus"] = focus

    target["verified"] = True
    return {
        "status": "PASS",
        "route": route,
        "selector": selector,
        "verified": True,
        "target": target,
        "duration_ms": context.elapsed_ms(started),
    }


async def _verify_postcondition(
    postcondition: dict[str, Any],
    *,
    context: ActionContext,
    started: float,
    route: str,
) -> dict[str, Any]:
    op_name = str(postcondition.get("op") or "")
    if op_name != "ui.get_property":
        blocked = build_blocked(
            reason="unsupported click postcondition",
            requested={"op": op_name},
            accepted={"op": "ui.get_property"},
            next_step="Use a bounded property postcondition for ui.click_verified.",
        )
        return {
            "status": "BLOCKED",
            **blocked,
            "route": route,
            "duration_ms": context.elapsed_ms(started),
        }
    selector = dict(postcondition["selector"])
    property_name = str(postcondition["property"])
    expected = postcondition["equals"]
    result = await context.call_adapter(
        "ui.get_property",
        selector=selector,
        property_name=property_name,
    )
    if not _is_adapter_success(result):
        failed = _adapter_failure_result(
            result,
            route=route,
            duration_ms=context.elapsed_ms(started),
            default_reason="failed to verify click postcondition",
        )
        failed["postcondition"] = {
            "op": op_name,
            "selector": selector,
            "property": property_name,
            "expected": expected,
            "result": _bounded_result(result),
            "verified": False,
        }
        return failed

    actual = result.get("value", result.get("actual"))
    if actual != expected:
        return {
            "status": "FAIL",
            "reason": "click postcondition mismatch",
            "route": route,
            "postcondition": {
                "op": op_name,
                "selector": selector,
                "property": property_name,
                "expected": expected,
                "actual": actual,
                "result": _bounded_result(result),
                "verified": False,
            },
            "duration_ms": context.elapsed_ms(started),
        }

    return {
        "status": "PASS",
        "route": route,
        "postcondition": {
            "op": op_name,
            "selector": selector,
            "property": property_name,
            "expected": expected,
            "actual": actual,
            "result": _bounded_result(result),
            "verified": True,
        },
        "duration_ms": context.elapsed_ms(started),
    }


def _action_result(**payload: Any) -> dict[str, Any]:
    result = dict(payload.get("result") or {})
    action = dict(payload)
    if str(action.get("status", "PASS")) != "PASS":
        for key in ("reason", "requested", "accepted", "next_step"):
            if key in result:
                action[key] = result[key]
    return action


def _target_requirements_from_action(
    action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_require = action.get("require", {})
    if not isinstance(raw_require, Mapping):
        blocked = build_blocked(
            reason="invalid target requirements",
            requested={"require": raw_require},
            accepted={"require": "object with visible/enabled/focus booleans"},
            next_step="Provide require as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    require = dict(raw_require)
    for key in ("visible", "enabled", "focus"):
        if key in require and not isinstance(require[key], bool):
            blocked = build_blocked(
                reason="invalid target requirement",
                requested={key: require[key]},
                accepted={key: "boolean"},
                next_step=f"Provide require.{key} as true or false.",
            )
            return {}, {"status": "BLOCKED", **blocked}
    return require, None


def _postcondition_from_action(
    action: dict[str, Any],
    *,
    default_selector: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_postcondition = action.get("postcondition")
    if not isinstance(raw_postcondition, Mapping):
        blocked = build_blocked(
            reason="click postcondition required",
            requested={"postcondition": raw_postcondition},
            accepted={"postcondition": "object with op=ui.get_property and equals"},
            next_step="Provide a bounded postcondition for ui.click_verified.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    postcondition = dict(raw_postcondition)
    op_name = str(postcondition.get("op") or "")
    if op_name != "ui.get_property":
        blocked = build_blocked(
            reason="unsupported click postcondition",
            requested={"op": op_name},
            accepted={"op": "ui.get_property"},
            next_step="Use a bounded property postcondition for ui.click_verified.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    selector = postcondition.get("selector", default_selector)
    if not isinstance(selector, Mapping):
        blocked = build_blocked(
            reason="invalid click postcondition selector",
            requested={"selector": selector},
            accepted={"selector": "object"},
            next_step="Provide postcondition.selector as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    property_name = postcondition.get("property", postcondition.get("property_name"))
    if not isinstance(property_name, str) or not property_name.strip():
        blocked = build_blocked(
            reason="click postcondition property required",
            requested={"property": property_name},
            accepted={"property": "non-empty string"},
            next_step="Provide the UI property that must be verified after the click.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    if "equals" not in postcondition:
        blocked = build_blocked(
            reason="click postcondition expected value required",
            requested={"equals": None},
            accepted={"equals": "bounded expected value"},
            next_step="Provide postcondition.equals so the click result can be verified.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    return {
        "op": op_name,
        "selector": dict(selector),
        "property": property_name.strip(),
        "equals": postcondition["equals"],
    }, None


def _target_requirement_failure(
    target: dict[str, Any],
    require: dict[str, Any],
) -> dict[str, Any] | None:
    for key in ("visible", "enabled"):
        if require.get(key) is True and target.get(key) is not True:
            return build_blocked(
                reason="target precondition failed",
                requested={key: target.get(key)},
                accepted={key: True},
                next_step="Choose a target that is visible and enabled before input/click.",
            )
    return None


def _target_evidence(
    selector: dict[str, Any],
    find_result: dict[str, Any],
) -> dict[str, Any]:
    target: dict[str, Any] = {
        "selector": selector,
        "found": find_result.get("found", True),
    }
    _copy_first_present(target, find_result, "visible", ("visible", "isVisible"))
    _copy_first_present(target, find_result, "enabled", ("enabled", "isEnabled"))
    for key in (
        "focusable",
        "isVisible",
        "isEnabled",
        "controlType",
        "control_type",
        "automationId",
        "automation_id",
        "name",
    ):
        if key in find_result:
            target[key] = find_result[key]
    return target


def _copy_first_present(
    target: dict[str, Any],
    source: Mapping[str, Any],
    output_key: str,
    input_keys: tuple[str, ...],
) -> None:
    for key in input_keys:
        if key in source:
            target[output_key] = source[key]
            return


def _is_adapter_success(result: dict[str, Any]) -> bool:
    return str(result.get("status", "PASS")).upper() in {"PASS", "OK", "SUCCESS"}


def _adapter_failure_result(
    result: dict[str, Any],
    *,
    route: str,
    duration_ms: int,
    default_reason: str,
) -> dict[str, Any]:
    status = str(result.get("status") or "BLOCKED").upper()
    failed: dict[str, Any] = {
        "status": status,
        "reason": str(result.get("reason") or default_reason),
        "route": route,
        "duration_ms": duration_ms,
        "result": _bounded_result(result),
    }
    for key in ("requested", "accepted", "next_step"):
        if key in result:
            failed[key] = result[key]
    return failed


def _click_evidence_failure(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("clicked") is True or result.get("invoked") is True:
        return None
    return build_blocked(
        reason="click activation evidence failed",
        requested={"clicked": result.get("clicked"), "invoked": result.get("invoked")},
        accepted={"clicked": True, "invoked": True},
        next_step="Return positive click activation evidence before verifying postconditions.",
    )


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"full_tree", "raw_tree", "ui_tree", "window_tree"}
    }


def _indices_from_action(action: dict[str, Any]) -> tuple[list[int], dict[str, Any] | None]:
    raw_indices = action.get("indices")
    if not isinstance(raw_indices, list) or not raw_indices:
        blocked = build_blocked(
            reason="invalid grid selection indices",
            requested={"indices": raw_indices},
            accepted={"indices": "non-empty list of non-negative integers"},
            next_step="Provide indices for each selected grid row.",
        )
        return [], {"status": "BLOCKED", **blocked}
    indices: list[int] = []
    for raw_index in raw_indices:
        if isinstance(raw_index, bool):
            blocked = build_blocked(
                reason="invalid grid selection index",
                requested={"index": raw_index},
                accepted={"index": "non-negative integer"},
                next_step="Use integer row indices.",
            )
            return [], {"status": "BLOCKED", **blocked}
        if isinstance(raw_index, int):
            index = raw_index
        elif isinstance(raw_index, str) and _INTEGER_TEXT.fullmatch(raw_index.strip()):
            index = int(raw_index)
        else:
            blocked = build_blocked(
                reason="invalid grid selection index",
                requested={"index": raw_index},
                accepted={"index": "non-negative integer"},
                next_step="Use integer row indices.",
            )
            return [], {"status": "BLOCKED", **blocked}
        if index < 0:
            blocked = build_blocked(
                reason="invalid grid selection index",
                requested={"index": raw_index},
                accepted={"index": "non-negative integer"},
                next_step="Use non-negative row indices.",
            )
            return [], {"status": "BLOCKED", **blocked}
        indices.append(index)
    return indices, None


def _row_identities_from_action(
    action: dict[str, Any],
) -> tuple[list[str], dict[str, Any] | None]:
    if "row_identities" not in action:
        return [], None
    raw_identities = action.get("row_identities")
    if not isinstance(raw_identities, list) or not raw_identities:
        blocked = build_blocked(
            reason="invalid grid selection identities",
            requested={"row_identities": raw_identities},
            accepted={"row_identities": "non-empty list of row identity strings"},
            next_step="Provide row_identities for each selected grid row.",
        )
        return [], {"status": "BLOCKED", **blocked}
    identities: list[str] = []
    for raw_identity in raw_identities:
        identity = str(raw_identity or "").strip()
        if not identity:
            blocked = build_blocked(
                reason="invalid grid selection identity",
                requested={"row_identity": raw_identity},
                accepted={"row_identity": "non-empty string"},
                next_step="Use stable row identity strings.",
            )
            return [], {"status": "BLOCKED", **blocked}
        identities.append(identity)
    return identities, None


def _row_from_action(action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_row = action.get("row")
    if not isinstance(raw_row, Mapping):
        blocked = build_blocked(
            reason="invalid grid row payload",
            requested={"row": raw_row},
            accepted={"row": "object with integer index or string identity"},
            next_step="Provide row.index or row.identity for a visible DataGrid row.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    if "index" in raw_row:
        raw_index = raw_row.get("index")
        if isinstance(raw_index, bool):
            return {}, _invalid_grid_row_index(raw_index)
        if isinstance(raw_index, int):
            index = raw_index
        elif isinstance(raw_index, str) and _INTEGER_TEXT.fullmatch(raw_index.strip()):
            index = int(raw_index)
        else:
            return {}, _invalid_grid_row_index(raw_index)
        if index < 0:
            return {}, _invalid_grid_row_index(raw_index)
        return {"index": index}, None
    identity = raw_row.get("identity", raw_row.get("key"))
    if identity is None or not str(identity):
        blocked = build_blocked(
            reason="invalid grid row identity",
            requested={"row": dict(raw_row)},
            accepted={"row": "object with integer index or string identity"},
            next_step="Provide row.identity for a unique visible DataGrid row.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    return {"identity": str(identity)}, None


def _invalid_grid_row_index(raw_index: Any) -> dict[str, Any]:
    blocked = build_blocked(
        reason="invalid grid row index",
        requested={"index": raw_index},
        accepted={"index": "non-negative integer"},
        next_step="Use an integer row index for visible DataGrid row actions.",
    )
    return {"status": "BLOCKED", **blocked}


def _mapping_from_action(action: dict[str, Any], key: str) -> dict[str, Any]:
    value = action.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _list_from_action(action: dict[str, Any], key: str) -> list[Any]:
    value = action.get(key)
    return list(value) if isinstance(value, list) else []


def _idle_ms_from_action(action: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    raw_idle_ms = action.get("idle_ms", 0)
    if isinstance(raw_idle_ms, bool) or (
        isinstance(raw_idle_ms, float) and not raw_idle_ms.is_integer()
    ):
        blocked = build_blocked(
            reason="invalid wait duration",
            requested={"idle_ms": raw_idle_ms},
            accepted={"idle_ms": "non-negative integer milliseconds"},
            next_step="Provide wait idle_ms as a non-negative integer.",
        )
        return 0, {"status": "BLOCKED", **blocked}
    try:
        idle_ms = int(raw_idle_ms)
    except (TypeError, ValueError):
        blocked = build_blocked(
            reason="invalid wait duration",
            requested={"idle_ms": raw_idle_ms},
            accepted={"idle_ms": "non-negative integer milliseconds"},
            next_step="Provide wait idle_ms as a non-negative integer.",
        )
        return 0, {"status": "BLOCKED", **blocked}
    if idle_ms < 0:
        blocked = build_blocked(
            reason="invalid wait duration",
            requested={"idle_ms": raw_idle_ms},
            accepted={"idle_ms": "non-negative integer milliseconds"},
            next_step="Provide wait idle_ms as a non-negative integer.",
        )
        return 0, {"status": "BLOCKED", **blocked}
    return idle_ms, None


async def _sleep_ms(context: ActionContext, idle_ms: int) -> None:
    await sleep_ms(context.clock, idle_ms)


def _selector_from_action(
    action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_selector = action.get("selector")
    if raw_selector is None:
        return {}, None
    if not isinstance(raw_selector, Mapping):
        blocked = build_blocked(
            reason="invalid selector payload",
            requested={"selector": raw_selector},
            accepted={"selector_type": "object"},
            next_step="Provide selector as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    return dict(raw_selector), None


register_action("noop", _handle_noop)
register_action("ui.noop", _handle_noop)
register_action("ui.click", _handle_ui_click)
register_action("ui.click_verified", _handle_ui_click_verified)
register_action("ui.drag", handle_ui_drag)
register_action("ui.grid.get_state", _handle_ui_grid_get_state)
register_action("ui.grid.ensure_visible", _handle_ui_grid_ensure_visible)
register_action("ui.grid.select_row", _handle_ui_grid_select_row)
register_action("ui.grid.click_row", _handle_ui_grid_click_row)
register_action("ui.grid.select", _handle_ui_grid_select)
register_action("ui.input.ensure_target", _handle_ui_input_ensure_target)
register_action("ui.invoke", _handle_ui_invoke)
register_action("ui.key_sequence", handle_ui_key_sequence)
register_action("ui.text.type_replace_selection", handle_ui_text_type_replace_selection)
register_action("wait", _handle_wait)
