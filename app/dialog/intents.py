"""Сопоставление распознанной речи с переходами сценария.

Голосовые команды задаются в шаге сценария:

    "voice": {"баланс": "balance", "оператор": "operator"}

Плюс без настройки работают числительные: «один»/«раз» → DTMF 1 и т.д., если
такая цифра есть в `dtmf` шага.
"""

from __future__ import annotations

import re
from typing import Mapping

__all__ = ["DIGIT_WORDS", "match_voice", "normalize_text", "spoken_digit"]

#: Слова, которыми абонент называет цифру меню.
DIGIT_WORDS: dict[str, str] = {
    "ноль": "0",
    "нуль": "0",
    "нолик": "0",
    "один": "1",
    "одна": "1",
    "одно": "1",
    "раз": "1",
    "первый": "1",
    "первое": "1",
    "два": "2",
    "две": "2",
    "второй": "2",
    "второе": "2",
    "три": "3",
    "третий": "3",
    "третье": "3",
    "четыре": "4",
    "четвертый": "4",
    "четвёртый": "4",
    "пять": "5",
    "пятый": "5",
    "шесть": "6",
    "шестой": "6",
    "семь": "7",
    "седьмой": "7",
    "восемь": "8",
    "восьмой": "8",
    "девять": "9",
    "девятый": "9",
}

_NON_WORD = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """Привести распознанный текст к сравнимому виду: нижний регистр, ё→е."""
    lowered = (text or "").lower().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", lowered).split())


def spoken_digit(text: str) -> str | None:
    """Найти в тексте число словами или цифрой (первое совпадение)."""
    normalized = normalize_text(text)
    for token in normalized.split():
        if token.isdigit() and len(token) == 1:
            return token
        digit = DIGIT_WORDS.get(token)
        if digit is not None:
            return digit
    return None


def match_voice(
    text: str,
    dtmf: Mapping[str, str],
    voice: Mapping[str, str],
) -> str | None:
    """Вернуть шаг перехода по распознанной фразе или None."""
    normalized = normalize_text(text)
    if not normalized:
        return None

    # Сначала точные фразы из сценария: они приоритетнее числительных.
    for phrase, target in sorted(voice.items(), key=lambda item: len(item[0]), reverse=True):
        needle = normalize_text(phrase)
        if needle and needle in normalized:
            return target

    digit = spoken_digit(normalized)
    if digit is not None:
        return dtmf.get(digit)
    return None
