"""Resource subscription authority and update delivery (Engram #393 / FD-006)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData, ServerCapabilities
from pydantic import AnyUrl

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.session import ServerSession

logger = logging.getLogger(__name__)

STATE_URI = "debug://state"
BREAKPOINTS_URI = "debug://breakpoints"
OUTPUT_URI = "debug://output"
THREADS_URI = "debug://threads"

KNOWN_RESOURCE_URIS: frozenset[str] = frozenset(
    {STATE_URI, BREAKPOINTS_URI, OUTPUT_URI, THREADS_URI}
)

_MISSING = object()
ResourceTokenProvider = Callable[[str], object]


class ResourceSubscriptions:
    """Own the connected Python session's resource subscriptions and ordered sends."""

    def __init__(self, token_provider: ResourceTokenProvider | None = None) -> None:
        self._subscribed: set[str] = set()
        self._session: ServerSession | None = None
        self._token_provider = token_provider
        self._last_sent_tokens: dict[str, object] = {}
        self._send_lock = asyncio.Lock()

    async def subscribe(self, uri: str, session: ServerSession) -> None:
        """Subscribe idempotently, rejecting URIs this server does not publish."""
        if uri not in KNOWN_RESOURCE_URIS:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown resource: {uri}"))

        async with self._send_lock:
            self._session = session
            if uri in self._subscribed:
                return

            self._subscribed.add(uri)
            if self._token_provider is not None:
                self._last_sent_tokens[uri] = self._token_provider(uri)

    async def unsubscribe(self, uri: str) -> None:
        """Remove a subscription idempotently and serialize it after in-flight sends."""
        async with self._send_lock:
            self._subscribed.discard(uri)
            self._last_sent_tokens.pop(uri, None)
            if not self._subscribed:
                self._session = None

    def is_subscribed(self, uri: str) -> bool:
        return uri in self._subscribed

    async def notify(self, uris: Iterable[str]) -> None:
        """Send each subscribed changed URI once, in caller order."""
        ordered_uris = tuple(dict.fromkeys(uris))
        if not ordered_uris:
            return

        async with self._send_lock:
            session = self._session
            if session is None:
                return

            for uri in ordered_uris:
                if uri not in self._subscribed:
                    continue

                token = (
                    self._token_provider(uri)
                    if self._token_provider is not None
                    else _MISSING
                )
                if (
                    self._token_provider is not None
                    and self._last_sent_tokens.get(uri, _MISSING) == token
                ):
                    continue

                try:
                    await session.send_resource_updated(AnyUrl(uri))
                except Exception as exc:
                    logger.warning(
                        "Failed to send resource update notification for %s: %s",
                        uri,
                        exc,
                    )
                else:
                    if self._token_provider is not None:
                        self._last_sent_tokens[uri] = token


def register_resource_subscription_handlers(
    mcp: FastMCP, subscriptions: ResourceSubscriptions
) -> None:
    """Register the SDK 1.25.0 low-level subscribe/unsubscribe escape hatch."""
    server = mcp._mcp_server

    @server.subscribe_resource()
    async def _subscribe(uri: AnyUrl) -> None:
        await subscriptions.subscribe(str(uri), server.request_context.session)

    @server.unsubscribe_resource()
    async def _unsubscribe(uri: AnyUrl) -> None:
        await subscriptions.unsubscribe(str(uri))


async def notify_resource_updated(
    uri: str, subscriptions: ResourceSubscriptions
) -> None:
    await subscriptions.notify((uri,))


async def notify_resources_updated(
    uris: Iterable[str], subscriptions: ResourceSubscriptions
) -> None:
    await subscriptions.notify(uris)


def apply_subscribe_capability(capabilities: ServerCapabilities) -> None:
    """Correct mcp 1.25.0's hardcoded ``resources.subscribe=False`` projection."""
    if capabilities.resources is not None:
        capabilities.resources.subscribe = True
