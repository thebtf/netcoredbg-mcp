from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..blocked import build_blocked
from .ui_key_sequence import handle_ui_key_sequence

ActionHandler = Callable[[dict[str, Any], "ActionContext"], Awaitable[dict[str, Any]]]

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


def _action_result(**payload: Any) -> dict[str, Any]:
    result = dict(payload.get("result") or {})
    action = dict(payload)
    if str(action.get("status", "PASS")) != "PASS":
        for key in ("reason", "requested", "accepted", "next_step"):
            if key in result:
                action[key] = result[key]
    return action


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


register_action("ui.click", _handle_ui_click)
register_action("ui.invoke", _handle_ui_invoke)
register_action("ui.key_sequence", handle_ui_key_sequence)
