"""Единая настройка логирования с маскированием секретов."""

from __future__ import annotations

import logging
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SecretFilter(logging.Filter):
    """Заменяет секретные строки в сообщениях на ***.

    Пароль ARI попадает в сообщения об ошибках HTTP-клиентов и трассировки,
    поэтому фильтр ставится на корневой логгер.
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - защита от битых аргументов лога
            return True
        masked = message
        for secret in self._secrets:
            if secret in masked:
                masked = masked.replace(secret, "***")
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str | None] = ()) -> None:
    """Настроить корневой логгер (повторный вызов не плодит обработчики)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(SecretFilter(secrets))

    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("asyncio").setLevel(logging.WARNING)
