"""Клиент AITUNNEL для LLM: OpenAI-совместимый `POST /chat/completions`.

Поддерживается function calling. Текст ответа возвращается целиком и затем
озвучивается через `SpeechClient`; потоковый TTS уже работает на уровне
`/audio/speech`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import aiohttp

from app.config import Settings
from app.llm.conversation import ChatConversation, LLMConversation
from app.llm.errors import LLMError
from app.llm.prompts import LLMPromptSet
from app.llm.tools import VoiceTools

__all__ = ["LLMClient", "LLMError"]


class LLMClient:
    """HTTP-клиент AITUNNEL и фабрика разговоров."""

    def __init__(
        self,
        settings: Settings,
        prompts: LLMPromptSet,
        *,
        logger: logging.Logger | None = None,
        timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._prompts = prompts
        self._base_url = settings.ai_tunnel_base_url.rstrip("/")
        self._key = (settings.ai_tunnel_api_key or "").strip()
        self._timeout = timeout if timeout is not None else settings.llm_timeout
        self._log = logger or logging.getLogger("app.llm")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    @property
    def model(self) -> str:
        return self._settings.llm_model

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        if not self.enabled:
            raise LLMError("AITUNNEL не настроен: задайте AITUNNEL_API_KEY")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=self._timeout, connect=min(self._timeout, 5.0)
            ),
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def __aenter__(self) -> LLMClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def conversation(self, tools: VoiceTools) -> LLMConversation:
        """Создать независимый разговор для одного звонка."""
        return ChatConversation(
            self,
            system_prompt=self._prompts.system,
            tools=tools,
            max_tool_rounds=self._settings.llm_max_tool_rounds,
            max_history_messages=self._settings.llm_history_messages,
            logger=self._log,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        """Отправить историю и вернуть `message` из первого choice."""
        await self.start()
        assert self._session is not None
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with self._session.post(
                self._url("/chat/completions"), json=payload
            ) as response:
                body = await response.read()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise LLMError(f"LLM: сеть недоступна: {exc}") from exc

        if status != 200:
            raise LLMError(f"LLM: HTTP {status}: {_error_text(body)}")

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"LLM: ответ не JSON: {_error_text(body)}") from exc
        if not isinstance(data, dict):
            raise LLMError("LLM: ответ должен быть JSON-объектом")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM: в ответе нет choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            raise LLMError("LLM: в ответе нет message")
        return message


def _error_text(body: bytes) -> str:
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - диагностика не должна падать
        return repr(body[:160])
    try:
        payload = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:300] or "пустой ответ"
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error)[:300]
        return str(error or payload)[:300]
    return " ".join(text.split())[:300]
