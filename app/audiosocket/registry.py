"""Реестр ожидающих AudioSocket-сессий.

Схема с ExternalMedia: бот создаёт медиаканал и передаёт в `data` свой UUID,
Asterisk подключается по TCP и присылает этот UUID первым фреймом. Реестр
позволяет связать входящее TCP-соединение с конкретным вызовом ARI.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

#: Колбэк завершения вызова (причина -> очистка через ARI).
CloseCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class PendingCall:
    """Вызов, для которого создан медиаканал и ожидается TCP от Asterisk."""

    uuid: str
    channel_id: str
    caller_number: str | None = None
    bridge_id: str | None = None
    media_channel_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    on_close: CloseCallback | None = field(default=None, repr=False)
    connected: asyncio.Event = field(default_factory=asyncio.Event)

    _closed: bool = field(default=False, repr=False)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def attach(self) -> None:
        """Отметить, что медиапоток подключён к сессии."""
        self.connected.set()

    async def close(self, reason: str) -> None:
        """Завершить вызов один раз: вызвать колбэк очистки."""
        if self._closed:
            return
        self._closed = True
        self.connected.set()
        if self.on_close is not None:
            await self.on_close(reason)

    @property
    def label(self) -> str:
        caller = self.caller_number or "?"
        return f"{caller} ({self.channel_id})"


class PendingRegistry:
    """Хранилище вызовов, ожидающих подключения медиаканала."""

    def __init__(self) -> None:
        self._items: dict[str, PendingCall] = {}

    def register(self, call: PendingCall) -> None:
        if call.uuid in self._items:
            raise ValueError(f"вызов с UUID {call.uuid} уже зарегистрирован")
        self._items[call.uuid] = call

    def pop(self, call_uuid: str) -> PendingCall | None:
        return self._items.pop(call_uuid, None)

    def get(self, call_uuid: str) -> PendingCall | None:
        return self._items.get(call_uuid)

    def discard(self, call_uuid: str) -> PendingCall | None:
        return self._items.pop(call_uuid, None)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, call_uuid: object) -> bool:
        return call_uuid in self._items

    def values(self) -> tuple[PendingCall, ...]:
        return tuple(self._items.values())
