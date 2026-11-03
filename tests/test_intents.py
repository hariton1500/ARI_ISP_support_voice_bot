"""Тесты сопоставления распознанной речи с переходами сценария."""

from __future__ import annotations

from app.dialog.intents import match_voice, normalize_text


def test_normalize_removes_punctuation_and_yo() -> None:
    assert normalize_text("  Соедините, пожалуйста, с оператором!  ") == (
        "соедините пожалуйста с оператором"
    )
    assert normalize_text("Всё хорошо") == "все хорошо"


def test_match_voice_finds_configured_phrases() -> None:
    voice = {"проверить баланс": "balance", "оператор": "operator"}
    assert match_voice("Проверить баланс, пожалуйста", voice) == "balance"
    assert match_voice("соедините с оператором", voice) == "operator"


def test_match_voice_ignores_unmatched_speech() -> None:
    assert match_voice("погода сегодня хорошая", {"оператор": "operator"}) is None
    assert match_voice("два", {"оператор": "operator"}) is None
    assert match_voice("", {"оператор": "operator"}) is None
