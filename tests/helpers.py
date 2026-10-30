"""Вспомогательные функции тестов."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Дождаться, пока условие станет истинным (опрос каждые 20 мс)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()
