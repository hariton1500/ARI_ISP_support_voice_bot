"""Тесты настроек и логирования."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings, load_settings
from app.logging_setup import SecretFilter, setup_logging


def test_defaults_without_env(monkeypatch) -> None:
    for name in (
        "AUDIOSOCKET_HOST",
        "AUDIOSOCKET_PORT",
        "SCENARIO_PATH",
        "PROMPTS_DIR",
        "RECORDINGS_DIR",
        "RECORD_CALLS",
        "INIT_TIMEOUT",
        "IDLE_TIMEOUT",
        "MAX_CALL_SECONDS",
        "LOG_LEVEL",
        "ARI_URL",
        "ARI_USER",
        "ARI_PASSWORD",
        "ARI_APP",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.audiosocket_host == "0.0.0.0"
    assert settings.audiosocket_port == 9092
    assert settings.record_calls is True
    assert settings.log_level == "INFO"
    assert settings.ari_enabled is False
    assert settings.prompts_dir == PROJECT_ROOT / "prompts" / "placeholder"


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("AUDIOSOCKET_PORT", "9099")
    monkeypatch.setenv("RECORD_CALLS", "false")
    monkeypatch.setenv("IDLE_TIMEOUT", "1,5")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("PROMPTS_DIR", "prompts/ru")
    settings = Settings.from_env()
    assert settings.audiosocket_port == 9099
    assert settings.record_calls is False
    assert settings.idle_timeout == 1.5
    assert settings.log_level == "DEBUG"
    assert settings.prompts_dir == PROJECT_ROOT / "prompts" / "ru"


def test_billing_settings(monkeypatch) -> None:
    for name in ("BILLING_BASE_URL", "BILLING_TOKEN", "BILLING_MOCK", "BILLING_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.billing_enabled is False
    assert settings.billing_timeout == 10.0

    monkeypatch.setenv("BILLING_BASE_URL", "http://billing.local/by-id")
    monkeypatch.setenv("BILLING_TOKEN", "secret-token")
    monkeypatch.setenv("BILLING_TIMEOUT", "3,5")
    settings = Settings.from_env()
    assert settings.billing_enabled is True
    assert settings.billing_timeout == 3.5
    assert settings.masked()["billing_token"] == "***"
    assert settings.masked()["billing_base_url"] == "http://billing.local/by-id"


def test_billing_mock_enables_billing_without_url(monkeypatch) -> None:
    monkeypatch.delenv("BILLING_BASE_URL", raising=False)
    monkeypatch.delenv("BILLING_TOKEN", raising=False)
    monkeypatch.setenv("BILLING_MOCK", "1")
    settings = Settings.from_env()
    assert settings.billing_mock is True
    assert settings.billing_enabled is True


def test_speech_settings(monkeypatch) -> None:
    for name in (
        "AITUNNEL_API_KEY",
        "AITUNNEL_BASE_URL",
        "STT_MODEL",
        "TTS_MODEL",
        "TTS_VOICE",
        "VOICE_INPUT",
        "VOICE_BARGE_IN",
        "VAD_START_MS",
        "VAD_SILENCE_MS",
        "VAD_MIN_SPEECH_MS",
        "STT_PARTIAL_INTERVAL_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.speech_enabled is False
    assert settings.ai_tunnel_base_url == "https://api.aitunnel.ru/v1"
    assert settings.stt_model == "whisper-large-v3-turbo"
    assert settings.tts_pcm_rate == 24000
    assert settings.voice_input is True
    assert settings.voice_barge_in is True
    assert settings.vad_silence_ms == 700

    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-aitunnel-secret")
    monkeypatch.setenv("AITUNNEL_BASE_URL", "https://ru-api.aitunnel.ru/v1")
    monkeypatch.setenv("VOICE_BARGE_IN", "false")
    monkeypatch.setenv("VAD_SILENCE_MS", "900")
    monkeypatch.setenv("STT_PARTIAL_INTERVAL_MS", "0")
    settings = Settings.from_env()
    assert settings.speech_enabled is True
    assert settings.ai_tunnel_base_url == "https://ru-api.aitunnel.ru/v1"
    assert settings.voice_barge_in is False
    assert settings.vad_silence_ms == 900
    assert settings.stt_partial_interval_ms == 0
    assert settings.masked()["ai_tunnel_api_key"] == "***"


def test_bad_port_raises(monkeypatch) -> None:
    monkeypatch.setenv("AUDIOSOCKET_PORT", "abc")
    with pytest.raises(ValueError, match="AUDIOSOCKET_PORT"):
        Settings.from_env()


def test_bad_bool_raises(monkeypatch) -> None:
    monkeypatch.setenv("RECORD_CALLS", "может быть")
    with pytest.raises(ValueError, match="RECORD_CALLS"):
        Settings.from_env()


def test_with_overrides_ignores_none_and_validates_names(settings: Settings) -> None:
    changed = settings.with_overrides(audiosocket_port=1234, log_level=None)
    assert changed.audiosocket_port == 1234
    assert changed.log_level == settings.log_level
    with pytest.raises(TypeError):
        settings.with_overrides(unknown_field=1)


def test_ari_requires_full_set(monkeypatch) -> None:
    monkeypatch.setenv("ARI_URL", "http://host:8088/ari")
    monkeypatch.setenv("ARI_USER", "ivr_ai")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("ARI_APP", "ivr_ai")
    settings = Settings.from_env()
    assert settings.ari_enabled is True
    assert settings.masked()["ari_password"] == "***"
    assert settings.masked()["ari_user"] == "ivr_ai"


def test_load_settings_reads_env_file(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("AUDIOSOCKET_PORT=9500\nLOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.delenv("AUDIOSOCKET_PORT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    settings = load_settings(env_file)
    assert settings.audiosocket_port == 9500
    assert settings.log_level == "WARNING"
    # load_dotenv пишет в os.environ — убираем за собой, чтобы не влиять на другие тесты
    monkeypatch.delenv("AUDIOSOCKET_PORT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)


def test_secret_filter_masks_password() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="подключение с паролем %s",
        args=("s3cret",),
        exc_info=None,
    )
    SecretFilter(["s3cret"]).filter(record)
    assert record.getMessage() == "подключение с паролем ***"


def test_setup_logging_does_not_duplicate_handlers() -> None:
    setup_logging("INFO")
    count = len(logging.getLogger().handlers)
    setup_logging("DEBUG")
    assert len(logging.getLogger().handlers) == count
    assert logging.getLogger().level == logging.DEBUG
