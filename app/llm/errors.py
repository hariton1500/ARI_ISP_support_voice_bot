"""Ошибки LLM-слоя."""

from __future__ import annotations


class LLMError(RuntimeError):
    """AITUNNEL chat/completions недоступен или вернул некорректный ответ."""


__all__ = ["LLMError"]
