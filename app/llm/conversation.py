"""Диалог с LLM: история, вызовы инструментов и текст для озвучки.

Клиент AITUNNEL не стримит ответ в этом слое: сначала получаем полный ответ
модели, затем отдаём его в TTS. Если модель попросила инструмент, результат
добавляется в историю и запрос повторяется — это стандартный function calling
цикл OpenAI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.llm.errors import LLMError
from app.llm.tools import CallControl, VoiceTools

__all__ = [
    "ChatCompletion",
    "ChatConversation",
    "LLMBackend",
    "LLMConversation",
    "LLMReply",
    "clean_for_speech",
]

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"[*_~]+")


def clean_for_speech(text: str) -> str:
    """Убрать markdown, чтобы TTS не читал звёздочки и решётки."""
    if not text:
        return ""
    cleaned = _FENCED_CODE.sub(" ", text)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _MD_HEADING.sub("", cleaned)
    cleaned = _MD_BULLET.sub("", cleaned)
    cleaned = _MD_EMPHASIS.sub("", cleaned)
    return " ".join(cleaned.split()).strip()


@dataclass(frozen=True, slots=True)
class LLMReply:
    """Ответ модели, готовый к озвучке."""

    text: str
    end_call: bool = False
    transfer: bool = False
    tool_calls: int = 0


class ChatCompletion(Protocol):
    """Минимальный интерфейс HTTP-клиента chat/completions."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Mapping[str, Any]: ...


class ChatConversation:
    """Один разговор: system-промпт, история и function calling."""

    def __init__(
        self,
        chat: ChatCompletion,
        *,
        system_prompt: str,
        tools: VoiceTools,
        max_tool_rounds: int = 4,
        max_history_messages: int = 40,
        logger: logging.Logger | None = None,
    ) -> None:
        self._chat = chat
        self._system_prompt = system_prompt
        self._tools = tools
        self._max_tool_rounds = max(1, max_tool_rounds)
        self._max_history = max(2, max_history_messages)
        self._log = logger or logging.getLogger("app.llm")
        self._history: list[dict[str, Any]] = []
        self.tool_calls = 0

    @property
    def history(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._history)

    async def ask(self, text: str) -> LLMReply:
        """Отправить реплику абонента и вернуть ответ для озвучки."""
        text = " ".join((text or "").split())
        if not text:
            raise LLMError("пустая реплика абонента")
        self._history.append({"role": "user", "content": text})
        self._trim_history()
        control = CallControl()

        for _round in range(self._max_tool_rounds):
            message = await self._chat.complete(self._messages(), self._tools.schemas())
            if not isinstance(message, Mapping):
                raise LLMError("LLM: сообщение ответа должно быть объектом")
            content = _content_text(message.get("content"))
            raw_calls = message.get("tool_calls")
            tool_calls = raw_calls if isinstance(raw_calls, list) else []

            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            self._history.append(assistant)

            if not tool_calls:
                return self._reply(content, control)

            for index, call in enumerate(tool_calls):
                name, arguments, call_id = _parse_tool_call(call, index)
                self._log.info("LLM вызвала инструмент %s", name or "?")
                result = await self._tools.execute(name or "", arguments, control)
                self.tool_calls += 1
                self._history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    }
                )

            if control.end_call or control.transfer:
                return self._reply(content, control)

        self._log.warning("LLM: исчерпан лимит раундов инструментов (%d)", self._max_tool_rounds)
        raise LLMError("LLM не завершила вызов инструментов за отведённые раунды")

    def _messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self._system_prompt}, *self._history]

    def _trim_history(self) -> None:
        if len(self._history) <= self._max_history:
            return
        start = len(self._history) - self._max_history
        # Нельзя обрезать посередине assistant/tool: начинаем с роли user.
        while start < len(self._history) and self._history[start].get("role") != "user":
            start += 1
        if start < len(self._history):
            del self._history[:start]

    def _reply(self, content: str, control: CallControl) -> LLMReply:
        speech = clean_for_speech(content)
        if not speech:
            if control.end_call:
                speech = "Всего доброго."
            elif control.transfer:
                speech = "Перевожу вас на оператора."
            else:
                speech = "Извините, не расслышал. Повторите, пожалуйста."
        return LLMReply(
            text=speech,
            end_call=control.end_call,
            transfer=control.transfer,
            tool_calls=self.tool_calls,
        )


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [
            str(item.get("text", ""))
            for item in value
            if isinstance(item, Mapping) and item.get("type") in {None, "text"}
        ]
        return " ".join(part for part in parts if part).strip()
    return ""


def _parse_tool_call(call: Any, index: int) -> tuple[str | None, Any, str]:
    if not isinstance(call, Mapping):
        return None, None, f"call_{index}"
    call_id = str(call.get("id") or f"call_{index}")
    function = call.get("function")
    if not isinstance(function, Mapping):
        return None, None, call_id
    name = str(function.get("name") or "") or None
    arguments = function.get("arguments")
    if isinstance(arguments, Mapping):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return name, arguments, call_id


class LLMConversation(Protocol):
    """Разговор, который использует CallSession."""

    tool_calls: int

    async def ask(self, text: str) -> LLMReply: ...


class LLMBackend(Protocol):
    """Фабрика разговоров (в тестах подменяется заглушкой)."""

    def conversation(self, tools: VoiceTools) -> LLMConversation: ...
