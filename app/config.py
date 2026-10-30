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

    # ARI понадобится на следующем этапе (управление вызовом из Stasis).
    ari_url: str | None = None
    ari_user: str | None = None
    ari_password: str | None = None
    ari_app: str | None = None

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

    def masked(self) -> dict[str, object]:
        """Настройки для логов — без секретов."""
        data: dict[str, object] = {}
        for field in self.__slots__:
            value = getattr(self, field)
            data[field] = "***" if field == "ari_password" and value else value
        return data


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Прочитать .env (если есть) и собрать настройки."""
    path = Path(env_file).expanduser() if env_file else _env_file_path()
    if path.is_file():
        load_dotenv(path, override=False)
    return Settings.from_env()
