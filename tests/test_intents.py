"""Тесты сопоставления распознанной речи с переходами сценария."""

from __future__ import annotations

from app.dialog.intents import match_voice, normalize_text, spoken_digit


def test_normalize_removes_punctuation_and_yo() -> None:
    assert normalize_text("  Соедините, пожалуйста, с оператором!  ") == (
        "соедините пожалуйста с оператором"
    )
    assert normalize_text("Всё хорошо") == "все хорошо"


def test_spoken_digit_words_and_numbers() -> None:
    assert spoken_digit("нажмите один") == "1"
    assert spoken_digit("раз") == "1"
    assert spoken_digit("я выбираю 3") == "3"
    assert spoken_digit("оператор") is None


def test_match_voice_prefers_phrase_over_digit() -> None:
    dtmf = {"1": "balance", "0": "operator"}
    voice = {"проверить баланс": "balance", "оператор": "operator"}
    assert match_voice("Проверить баланс, пожалуйста", dtmf, voice) == "balance"
    assert match_voice("соедините с оператором", dtmf, voice) == "operator"


def test_match_voice_falls_back_to_spoken_dtmf() -> None:
    dtmf = {"1": "balance", "2": "tech_support", "0": "operator"}
    assert match_voice("два", dtmf, {}) == "tech_support"
    assert match_voice("нажмите ноль", dtmf, {}) == "operator"


def test_match_voice_ignores_unmatched_speech() -> None:
    assert match_voice("погода сегодня хорошая", {"1": "balance"}, {}) is None
    assert match_voice("", {"1": "balance"}, {}) is None
