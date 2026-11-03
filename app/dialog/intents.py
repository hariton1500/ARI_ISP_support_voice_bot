"""Сопоставление распознанной речи с голосовыми переходами сценария."""

from __future__ import annotations

import re
from typing import Mapping

__all__ = ["match_voice", "normalize_text"]

_NON_WORD = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """Привести распознанный текст к сравнимому виду: нижний регистр, ё→е."""
    lowered = (text or "").lower().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", lowered).split())


def match_voice(text: str, voice: Mapping[str, str]) -> str | None:
    """Вернуть шаг перехода по распознанной фразе или None."""
    normalized = normalize_text(text)
    if not normalized:
        return None

    for phrase, target in sorted(voice.items(), key=lambda item: len(item[0]), reverse=True):
        needle = normalize_text(phrase)
        if needle and needle in normalized:
            return target
    return None
