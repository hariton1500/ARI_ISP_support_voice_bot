"""Настройки приложения.

Значения читаются из переменных окружения, а также из файла .env
(переменная ENV_FILE позволяет указать другой путь). Файл .env в git не
попадает — см. .gitignore и .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "да"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "нет"})


def _env_file_path() -> Path:
    raw = os.environ.get("ENV_FILE")
    return Path(raw).expanduser() if raw else PROJECT_ROOT / ".env"


def _get_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name}: ожидалось целое число, получено {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name}: ожидалось число, получено {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name}: ожидалось логическое значение, получено {raw!r}")


def _get_optional_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _get_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return _get_int(name, 0)


def _get_path(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    path = Path(raw.strip()).expanduser() if raw and raw.strip() else Path(default)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass(frozen=True, slots=True)
class Settings:
    """Неизменяемый набор настроек, полученный из окружения."""

    # AudioSocket: здесь бот слушает, Asterisk подключается к нему.
    audiosocket_host: str
    audiosocket_port: int

    # Сценарий и промпты.
    scenario_path: Path
    prompts_dir: Path

    # Записи разговоров.
    record_calls: bool
    recordings_dir: Path

    # Таймауты в секундах.
    init_timeout: float
    idle_timeout: float
    max_call_seconds: float

    log_level: str

    # ARI: управление вызовом из Stasis (WebSocket + REST).
    ari_url: str | None = None
    ari_user: str | None = None
    ari_password: str | None = None
    ari_app: str | None = None

    # ExternalMedia: адрес, по которому Asterisk подключается к нашему AudioSocket.
    external_media_host: str | None = None
    external_media_port: int | None = None
    external_media_format: str = "slin"
    external_media_transport: str = "tcp"
    external_media_encapsulation: str = "audiosocket"
    external_media_timeout: float = 5.0

    # Разрешать AudioSocket-сессии без зарегистрированного вызова (автономный режим).
    allow_unknown_sessions: bool = True

    # Биллинг: запрос абонента POST-ом с Bearer-токеном (см. app/billing/client.py).
    billing_base_url: str | None = None
    billing_token: str | None = None
    billing_timeout: float = 10.0
    billing_mock: bool = False

    # Речь: AITUNNEL — OpenAI-совместимый API (STT /audio/transcriptions,
    # TTS /audio/speech). Ключ sk-aitunnel-…; без него речь выключена.
    ai_tunnel_api_key: str | None = None
    ai_tunnel_base_url: str = "https://api.aitunnel.ru/v1"
    stt_model: str = "whisper-large-v3-turbo"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "nova"
    tts_format: str = "pcm"
    tts_pcm_rate: int = 24000
    speech_language: str = "ru"
    speech_timeout: float = 20.0

    # VAD и эндпоинтинг (длительности в миллисекундах).
    voice_input: bool = True
    voice_barge_in: bool = True
    vad_start_ms: int = 160
    vad_silence_ms: int = 700
    vad_min_speech_ms: int = 200
    vad_rms_threshold: float = 300.0
    vad_noise_multiplier: float = 2.5

    #: 0 — не запрашивать промежуточные гипотезы STT.
    stt_partial_interval_ms: int = 1200

    # LLM: AITUNNEL /chat/completions + инструменты (см. app/llm/).
    llm_model: str = "gpt-4o-mini"
    llm_prompts_dir: Path = PROJECT_ROOT / "prompts" / "llm"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 300
    llm_timeout: float = 30.0
    llm_max_tool_rounds: int = 4
    llm_history_messages: int = 40

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            audiosocket_host=_get_str("AUDIOSOCKET_HOST", "0.0.0.0"),
            audiosocket_port=_get_int("AUDIOSOCKET_PORT", 9092),
            scenario_path=_get_path("SCENARIO_PATH", "scenarios/default.json"),
            prompts_dir=_get_path("PROMPTS_DIR", "prompts/placeholder"),
            record_calls=_get_bool("RECORD_CALLS", True),
            recordings_dir=_get_path("RECORDINGS_DIR", "recordings"),
            init_timeout=_get_float("INIT_TIMEOUT", 10.0),
            idle_timeout=_get_float("IDLE_TIMEOUT", 30.0),
            max_call_seconds=_get_float("MAX_CALL_SECONDS", 600.0),
            log_level=_get_str("LOG_LEVEL", "INFO").upper(),
            ari_url=_get_optional_str("ARI_URL"),
            ari_user=_get_optional_str("ARI_USER"),
            ari_password=_get_optional_str("ARI_PASSWORD"),
            ari_app=_get_optional_str("ARI_APP"),
            external_media_host=_get_optional_str("EXTERNAL_MEDIA_HOST"),
            external_media_port=_get_optional_int("EXTERNAL_MEDIA_PORT"),
            external_media_format=_get_str("EXTERNAL_MEDIA_FORMAT", "slin"),
            external_media_transport=_get_str("EXTERNAL_MEDIA_TRANSPORT", "tcp"),
            external_media_encapsulation=_get_str("EXTERNAL_MEDIA_ENCAPSULATION", "audiosocket"),
            external_media_timeout=_get_float("EXTERNAL_MEDIA_TIMEOUT", 5.0),
            allow_unknown_sessions=_get_bool("ALLOW_UNKNOWN_SESSIONS", True),
            billing_base_url=_get_optional_str("BILLING_BASE_URL"),
            billing_token=_get_optional_str("BILLING_TOKEN"),
            billing_timeout=_get_float("BILLING_TIMEOUT", 10.0),
            billing_mock=_get_bool("BILLING_MOCK", False),
            ai_tunnel_api_key=_get_optional_str("AITUNNEL_API_KEY"),
            ai_tunnel_base_url=_get_str("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1"),
            stt_model=_get_str("STT_MODEL", "whisper-large-v3-turbo"),
            tts_model=_get_str("TTS_MODEL", "gpt-4o-mini-tts"),
            tts_voice=_get_str("TTS_VOICE", "nova"),
            tts_format=_get_str("TTS_FORMAT", "pcm").lower(),
            tts_pcm_rate=_get_int("TTS_PCM_RATE", 24000),
            speech_language=_get_str("SPEECH_LANGUAGE", "ru"),
            speech_timeout=_get_float("SPEECH_TIMEOUT", 20.0),
            voice_input=_get_bool("VOICE_INPUT", True),
            voice_barge_in=_get_bool("VOICE_BARGE_IN", True),
            vad_start_ms=_get_int("VAD_START_MS", 160),
            vad_silence_ms=_get_int("VAD_SILENCE_MS", 700),
            vad_min_speech_ms=_get_int("VAD_MIN_SPEECH_MS", 200),
            vad_rms_threshold=_get_float("VAD_RMS_THRESHOLD", 300.0),
            vad_noise_multiplier=_get_float("VAD_NOISE_MULTIPLIER", 2.5),
            stt_partial_interval_ms=_get_int("STT_PARTIAL_INTERVAL_MS", 1200),
            llm_model=_get_str("LLM_MODEL", "gpt-4o-mini"),
            llm_prompts_dir=_get_path("LLM_PROMPTS_DIR", "prompts/llm"),
            llm_temperature=_get_float("LLM_TEMPERATURE", 0.3),
            llm_max_tokens=_get_int("LLM_MAX_TOKENS", 300),
            llm_timeout=_get_float("LLM_TIMEOUT", 30.0),
            llm_max_tool_rounds=_get_int("LLM_MAX_TOOL_ROUNDS", 4),
            llm_history_messages=_get_int("LLM_HISTORY_MESSAGES", 40),
        )

    def with_overrides(self, **overrides: object) -> Settings:
        """Вернуть копию настроек с заменой переданных полей (None игнорируется)."""
        clean = {key: value for key, value in overrides.items() if value is not None}
        unknown = set(clean) - {field for field in self.__slots__}
        if unknown:
            raise TypeError(f"неизвестные настройки: {', '.join(sorted(unknown))}")
        return replace(self, **clean)

    @property
    def ari_enabled(self) -> bool:
        """ARI-слой подключается только при полном наборе параметров."""
        return bool(self.ari_url and self.ari_user and self.ari_password and self.ari_app)

    @property
    def billing_enabled(self) -> bool:
        """Биллинг готов: заданы адрес и токен либо включён BILLING_MOCK."""
        return bool(self.billing_base_url and self.billing_token) or self.billing_mock

    @property
    def speech_enabled(self) -> bool:
        """AITUNNEL готов: есть ключ и не отключён голосовой ввод/синтез."""
        return bool(self.ai_tunnel_api_key)

    @property
    def llm_enabled(self) -> bool:
        """LLM доступна вместе с речью: AITUNNEL_API_KEY задан."""
        return bool(self.ai_tunnel_api_key)

    @property
    def media_port(self) -> int:
        return self.external_media_port or self.audiosocket_port

    @property
    def ws_url(self) -> str:
        """URL WebSocket-событий ARI, выведенный из ARI_URL."""
        if not self.ari_url or not self.ari_app:
            raise ValueError("ARI не настроен: нужны ARI_URL и ARI_APP")
        base = self.ari_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/events?app={self.ari_app}"

    def masked(self) -> dict[str, object]:
        """Настройки для логов — без секретов."""
        data: dict[str, object] = {}
        for field in self.__slots__:
            value = getattr(self, field)
            secret = field in {"ari_password", "billing_token", "ai_tunnel_api_key"}
            data[field] = "***" if secret and value else value
        return data


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Прочитать .env (если есть) и собрать настройки."""
    path = Path(env_file).expanduser() if env_file else _env_file_path()
    if path.is_file():
        load_dotenv(path, override=False)
    return Settings.from_env()
