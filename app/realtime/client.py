"""WebSocket-клиент Yandex AI Studio Realtime.

На входе и выходе используется PCM16 mono 16 кГц. Клиент отвечает только за
протокол: ресемплинг AudioSocket, темп воспроизведения и функции звонка живут
в ``app.realtime.session``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

import aiohttp

from app.config import Settings

__all__ = ["RealtimeBackend", "RealtimeError", "YandexRealtimeClient", "realtime_tool_schemas"]

REALTIME_SAMPLE_RATE = 16000
_GREETING_INSTRUCTION = (
    "Коротко поприветствуй абонента от имени службы поддержки «Евпанет» "
    "и спроси, чем ты можешь помочь. Не называйся другой компанией."
)


class RealtimeError(RuntimeError):
    """Yandex Realtime недоступен или вернул некорректное сообщение."""


class RealtimeBackend(Protocol):
    """Интерфейс одного Realtime-подключения (подменяется в тестах)."""

    async def start(self) -> None: ...

    async def configure(self) -> None: ...

    async def append_audio(self, pcm: bytes) -> None: ...

    async def request_greeting(self) -> None: ...

    async def send_function_output(self, call_id: str, output: str) -> None: ...

    async def request_response(self) -> None: ...

    def events(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


def realtime_tool_schemas(
    schemas: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Развернуть OpenAI tools во flat-формат Realtime session.update."""
    result: list[dict[str, Any]] = []
    for schema in schemas:
        function = schema.get("function")
        if not isinstance(function, Mapping):
            continue
        result.append(
            {
                "type": "function",
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object"}),
            }
        )
    return result


class YandexRealtimeClient:
    """Одно WebSocket-подключение на один телефонный вызов."""

    def __init__(
        self,
        settings: Settings,
        instructions: str,
        tools: Sequence[Mapping[str, Any]],
        *,
        logger: logging.Logger | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._instructions = instructions
        self._tools = realtime_tool_schemas(tools)
        self._log = logger or logging.getLogger("app.realtime")
        self._http = http_session
        self._owns_http = http_session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    def model_uri(self) -> str:
        return f"gpt://{self._settings.yandex_folder_id}/{self._settings.yandex_realtime_model}"

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        if self.connected:
            return
        if not self._settings.realtime_enabled:
            raise RealtimeError(
                "Yandex Realtime не настроен: задайте YANDEX_API_KEY и YANDEX_FOLDER_ID"
            )
        if self._http is None or self._http.closed:
            timeout = self._settings.yandex_realtime_timeout
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=timeout)
            )
            self._owns_http = True
        headers = {
            "Authorization": f"Api-Key {self._settings.yandex_api_key}",
            "x-data-logging-enabled": (
                "true" if self._settings.yandex_data_logging else "false"
            ),
        }
        try:
            self._ws = await asyncio.wait_for(
                self._http.ws_connect(
                    self._settings.yandex_realtime_url,
                    params={"model": self.model_uri},
                    headers=headers,
                    heartbeat=30.0,
                    max_msg_size=16 * 1024 * 1024,
                ),
                timeout=self._settings.yandex_realtime_timeout,
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise RealtimeError(f"не удалось подключиться к Yandex Realtime: {exc}") from exc
        self._log.info("Yandex Realtime подключён: %s", self.model_uri)

    async def configure(self) -> None:
        """Настроить PCM 16 кГц, серверный VAD, голос и функции звонка."""
        await self.send_json(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": self.model_uri,
                    "instructions": self._instructions,
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "silence_duration_ms": self._settings.vad_silence_ms,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                            "voice": self._settings.yandex_realtime_voice,
                        },
                    },
                    "tools": self._tools,
                },
            }
        )

    async def append_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        encoded = base64.b64encode(pcm).decode("ascii")
        await self.send_json({"type": "input_audio_buffer.append", "audio": encoded})

    async def request_greeting(self) -> None:
        await self.send_json(
            {
                "type": "response.create",
                "response": {
                    "instructions": _GREETING_INSTRUCTION,
                    "output_modalities": ["audio"],
                },
            }
        )

    async def send_function_output(self, call_id: str, output: str) -> None:
        await self.send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )

    async def request_response(self) -> None:
        await self.send_json({"type": "response.create"})

    async def send_json(self, payload: Mapping[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise RealtimeError("WebSocket Yandex Realtime не подключён")
        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
        except (aiohttp.ClientError, ConnectionError, OSError) as exc:
            raise RealtimeError(f"не удалось отправить событие в Yandex Realtime: {exc}") from exc

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Читать JSON-события до закрытия соединения."""
        ws = self._ws
        if ws is None:
            raise RealtimeError("WebSocket Yandex Realtime не подключён")
        while True:
            try:
                message = await ws.receive()
            except (aiohttp.ClientError, ConnectionError, OSError) as exc:
                raise RealtimeError(f"обрыв WebSocket Yandex Realtime: {exc}") from exc
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(message.data)
                except json.JSONDecodeError as exc:
                    raise RealtimeError("Yandex Realtime вернул некорректный JSON") from exc
                if not isinstance(event, dict):
                    raise RealtimeError("Событие Yandex Realtime должно быть JSON-объектом")
                yield event
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise RealtimeError(f"ошибка WebSocket Yandex Realtime: {ws.exception()}")
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            }:
                return

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except (aiohttp.ClientError, ConnectionError, OSError):
                pass
        http = self._http
        if http is not None and self._owns_http and not http.closed:
            await http.close()
        if self._owns_http:
            self._http = None

    async def __aenter__(self) -> YandexRealtimeClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
