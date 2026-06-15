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

ActionHandler = Callable[[dict[str, Any], "ActionContext"], Awaitable[dict[str, Any]]]
_INTEGER_TEXT = re.compile(r"-?\d+")

_ACTION_REGISTRY: dict[str, ActionHandler] = {}


@dataclass(frozen=True)
class ActionContext:
    service_adapters: dict[str, Callable[..., Any]]
    clock: Callable[[], float]
    session: Any | None = None

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


async def _handle_ui_grid_select(action: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    started = context.clock()
    selector, blocked = _selector_from_action(action)
    if blocked is not None:
        return {
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "grid_select",
        }
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


def _action_result(**payload: Any) -> dict[str, Any]:
    result = dict(payload.get("result") or {})
    action = dict(payload)
    if str(action.get("status", "PASS")) != "PASS":
        for key in ("reason", "requested", "accepted", "next_step"):
            if key in result:
                action[key] = result[key]
    return action


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
register_action("ui.drag", handle_ui_drag)
register_action("ui.grid.select", _handle_ui_grid_select)
register_action("ui.invoke", _handle_ui_invoke)
register_action("ui.key_sequence", handle_ui_key_sequence)
register_action("wait", _handle_wait)
