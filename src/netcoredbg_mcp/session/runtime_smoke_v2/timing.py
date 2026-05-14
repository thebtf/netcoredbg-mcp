from __future__ import annotations

import asyncio
import inspect
from typing import Any


async def sleep_ms(clock: Any, idle_ms: int) -> None:
    sleeper = getattr(clock, "sleep_ms", None)
    if callable(sleeper):
        result = sleeper(idle_ms)
        if inspect.isawaitable(result):
            await result
        return
    await asyncio.sleep(idle_ms / 1000)
