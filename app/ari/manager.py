"""Оркестрация вызова через ARI.

Порядок, рекомендованный Asterisk:
    StasisStart -> answer -> bridges?type=mixing -> channels/externalMedia
    -> bridges/{id}/addChannel (абонент + медиаканал)

Звук идёт по AudioSocket (ExternalMedia подключается к нашему серверу сам),
завершение выполняется через DELETE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

import aiohttp

from app.ari.client import AriClient, AriError, close_quietly
from app.ari.events import AriEventStream
from app.audiosocket.registry import PendingCall, PendingRegistry
from app.config import Settings

#: События жизненного цикла канала: логируем на INFO, чтобы в обычном режиме
#: было видно, кто и когда завершил вызов (остальные события — на DEBUG).
LIFECYCLE_EVENTS = frozenset(
    {
        "StasisStart",
        "StasisEnd",
        "ChannelHangupRequest",
        "ChannelDestroyed",
        "ChannelStateChange",
        "ChannelEnteredBridge",
        "ChannelLeftBridge",
    }
)


class AriCallManager:
    """Следит за событиями ARI и поднимает медиа для каждого вызова."""

    def __init__(
        self,
        settings: Settings,
        registry: PendingRegistry,
        *,
        client: AriClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not settings.ari_app:
            raise ValueError("ARI не настроен: нужен ARI_APP")
        self._settings = settings
        self._registry = registry
        self._log = logger or logging.getLogger("app.ari.manager")
        self._client = client or AriClient(settings, logger=self._log)
        self._events = AriEventStream(
            settings.ws_url,
            settings.ari_user or "",
            settings.ari_password or "",
            self._on_event,
            logger=self._log,
        )
        self._calls_by_channel: dict[str, PendingCall] = {}
        self._media_to_call: dict[str, PendingCall] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._events_task: asyncio.Task[None] | None = None

    # --------------------------------------------------------------- API

    @property
    def media_target(self) -> str:
        """Адрес, по которому Asterisk подключается к нашему AudioSocket."""
        host = self._settings.external_media_host or "127.0.0.1"
        return f"{host}:{self._settings.media_port}"

    @property
    def active_calls(self) -> int:
        return len(self._calls_by_channel)

    @property
    def is_connected(self) -> bool:
        return self._events.is_connected

    async def start(self) -> None:
        await self._client.start()
        self._events_task = asyncio.create_task(self._events.run(), name="ari-events")

    async def stop(self) -> None:
        for call in list(self._calls_by_channel.values()):
            await call.close("shutdown")
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self._events_task is not None:
            await self._events.stop()
            self._events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._events_task
            self._events_task = None
        await self._client.close()

    # ------------------------------------------------------------ события

    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            return

        cause = event.get("cause")
        cause_txt = event.get("cause_txt")
        name = channel.get("name") or "?"
        if cause is not None or cause_txt:
            self._log.info(
                "ARI %s: канал %s (%s), причина %s (%s)",
                etype,
                channel_id,
                name,
                cause if cause is not None else "?",
                cause_txt or "без описания",
            )
        elif etype in LIFECYCLE_EVENTS:
            self._log.info(
                "ARI %s: канал %s (%s, состояние=%s)",
                etype,
                channel_id,
                name,
                channel.get("state"),
            )
        else:
            self._log.debug("ARI %s: канал %s (%s)", etype, channel_id, name)

        if etype == "StasisStart":
            await self._on_stasis_start(channel_id, channel)
        elif etype in {"StasisEnd", "ChannelHangupRequest", "ChannelDestroyed"}:
            await self._on_channel_gone(channel_id, etype)

    async def _on_stasis_start(self, channel_id: str, channel: dict[str, Any]) -> None:
        if channel_id in self._media_to_call:
            self._log.info("ARI: медиаканал %s вошёл в Stasis", channel_id)
            return
        if channel_id in self._calls_by_channel:
            return

        caller = (channel.get("caller") or {}).get("number")
        call = PendingCall(uuid=str(uuid.uuid4()), channel_id=channel_id, caller_number=caller)
        call.on_close = lambda reason, _call=call: self._on_call_closed(_call, reason)
        self._calls_by_channel[channel_id] = call
        self._registry.register(call)
        self._log.info(
            "ARI: новый вызов %s от %s, сессия %s", channel_id, caller or "?", call.uuid
        )
        self._spawn(self._setup_call(call))

    async def _on_channel_gone(self, channel_id: str, etype: str) -> None:
        media_call = self._media_to_call.get(channel_id)
        if media_call is not None:
            caller_alive = media_call.channel_id in self._calls_by_channel
            self._log.warning(
                "вызов %s: медиаканал %s завершился (событие %s), канал абонента %s",
                media_call.channel_id,
                channel_id,
                etype,
                "ещё активен" if caller_alive else "уже закрыт",
            )
            await media_call.close("media_ended")
            return

        call = self._calls_by_channel.get(channel_id)
        if call is not None:
            reasons = {
                "StasisEnd": "stasis_end",
                "ChannelHangupRequest": "hangup_request",
                "ChannelDestroyed": "channel_destroyed",
            }
            await call.close(reasons.get(etype, etype.lower()))

    # -------------------------------------------------------------- вызов

    async def _setup_call(self, call: PendingCall) -> None:
        try:
            await self._client.answer(call.channel_id)
            call.bridge_id = await self._client.create_bridge(name=f"ivr-{call.uuid[:8]}")
            media = await self._client.create_external_media(
                app=self._settings.ari_app or "",
                external_host=self.media_target,
                data=call.uuid,
                media_format=self._settings.external_media_format,
                transport=self._settings.external_media_transport,
                encapsulation=self._settings.external_media_encapsulation,
            )
            call.media_channel_id = str(media["id"])
            self._media_to_call[call.media_channel_id] = call
            await self._client.add_channels(
                call.bridge_id, [call.channel_id, call.media_channel_id]
            )
            if call.is_closed:
                self._log.debug(
                    "вызов %s: закрыт до входа в бридж — настройку прекращаем", call.channel_id
                )
                return
            self._log.info(
                "вызов %s: бридж %s, медиаканал %s, ждём TCP на %s",
                call.channel_id,
                call.bridge_id,
                call.media_channel_id,
                self.media_target,
            )
            await asyncio.wait_for(
                call.connected.wait(), timeout=self._settings.external_media_timeout
            )
            if not call.is_closed:
                self._log.info("вызов %s: медиапоток подключён", call.channel_id)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._log.error(
                "вызов %s: externalMedia не подключился к %s за %.1f с — "
                "проверьте маршрут и файрвол на порт %d",
                call.channel_id,
                self.media_target,
                self._settings.external_media_timeout,
                self._settings.media_port,
            )
            await call.close("media_timeout")
        except AriError as exc:
            self._log.error("вызов %s: ошибка ARI: %s", call.channel_id, exc)
            await call.close("ari_error")
        except Exception:  # noqa: BLE001 - вызов не должен ронять менеджер
            self._log.exception("вызов %s: не удалось настроить вызов", call.channel_id)
            await call.close("setup_error")

    async def _on_call_closed(self, call: PendingCall, reason: str) -> None:
        self._calls_by_channel.pop(call.channel_id, None)
        if call.media_channel_id:
            self._media_to_call.pop(call.media_channel_id, None)
        self._registry.discard(call.uuid)
        self._log.info("вызов %s: очистка ARI (причина %s)", call.channel_id, reason)

        if call.media_channel_id:
            await close_quietly(self._client, call.media_channel_id)
        if call.bridge_id:
            with contextlib.suppress(AriError, asyncio.TimeoutError, aiohttp.ClientError):
                await self._client.delete_bridge(call.bridge_id)
        await close_quietly(self._client, call.channel_id)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
