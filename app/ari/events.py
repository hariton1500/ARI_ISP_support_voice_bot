"""Поток событий ARI по WebSocket.

Соединение должно быть открыто постоянно: пока приложение `ivr_ai` не
подключено, Asterisk не знает, кому отдавать звонки на 777, и завершает вызов.
Поэтому здесь реконнект с экспоненциальной задержкой.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class AriEventStream:
    """Читает события ARI и передаёт их обработчику."""

    def __init__(
        self,
        ws_url: str,
        username: str,
        password: str,
        on_event: EventHandler,
        *,
        logger: logging.Logger | None = None,
        reconnect_min: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self._ws_url = ws_url
        self._headers = {"Authorization": aiohttp.encode_basic_auth(username, password)}
        self._on_event = on_event
        self._log = logger or logging.getLogger("app.ari.events")
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._backoff = reconnect_min
        self._stopped = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.connected = asyncio.Event()
        self.events_received = 0

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    async def run(self) -> None:
        """Работать до stop(), переподключаясь при обрывах."""
        self._backoff = self._reconnect_min
        while not self._stopped:
            try:
                await self._run_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - сеть может падать как угодно
                if self._stopped:
                    break
                self._log.warning("ARI WebSocket: ошибка: %s", exc)
            finally:
                self.connected.clear()
                self._ws = None

            if self._stopped:
                break
            self._log.info("ARI WebSocket: переподключение через %.1f с", self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._reconnect_max)

    async def _run_session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=10.0, sock_read=None)
        async with aiohttp.ClientSession(headers=self._headers, timeout=timeout) as session:
            async with session.ws_connect(self._ws_url, heartbeat=30.0) as ws:
                self._ws = ws
                self._backoff = self._reconnect_min
                self.connected.set()
                self._log.info("ARI WebSocket подключён: %s", self._ws_url)

                async for message in ws:
                    if message.type is aiohttp.WSMsgType.TEXT:
                        await self._handle_text(message.data)
                    elif message.type is aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"WebSocket error: {ws.exception()}")

    async def _handle_text(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            self._log.warning("ARI: некорректный JSON в событии")
            return
        if not isinstance(event, dict):
            self._log.warning("ARI: событие не является объектом")
            return
        self.events_received += 1
        try:
            await self._on_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - одно событие не должно ломать поток
            self._log.exception("ARI: ошибка обработки события %s", event.get("type"))

    async def stop(self) -> None:
        self._stopped = True
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - соединение могло уже закрыться
                pass
