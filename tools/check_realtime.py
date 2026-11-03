"""Проверить Yandex Realtime без звонка: WebSocket + session.update.

Запуск из корня проекта: ``python tools/check_realtime.py``. Секреты и содержимое
событий не выводятся; печатаются только тип первого ответа и результат.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.llm.prompts import load_llm_prompts  # noqa: E402
from app.llm.tools import VoiceTools  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.realtime.client import RealtimeError, YandexRealtimeClient  # noqa: E402


async def _check(env_file: Path | None) -> int:
    settings = load_settings(env_file).with_overrides(voice_provider="yandex_realtime")
    setup_logging(
        settings.log_level,
        secrets=(settings.yandex_api_key, settings.ari_password, settings.billing_token),
    )
    log = logging.getLogger("check_realtime")
    if not settings.realtime_enabled:
        log.error("задайте YANDEX_API_KEY и YANDEX_FOLDER_ID в %s", env_file or ".env")
        return 2
    prompts = load_llm_prompts(settings.llm_prompts_dir, log)
    client = YandexRealtimeClient(
        settings,
        prompts.system,
        VoiceTools(None).schemas(),
        logger=log,
    )
    try:
        await client.start()
        await client.configure()
        events = client.events()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.yandex_realtime_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            event = await asyncio.wait_for(anext(events), timeout=remaining)
            kind = event.get("type")
            if kind == "error":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else str(error)
                log.error("Yandex Realtime отклонил настройку сессии: %s", message or "без описания")
                return 1
            if kind == "session.updated":
                log.info("Yandex Realtime: session.updated; ключ, модель и настройки приняты")
                return 0
            log.debug("Yandex Realtime: служебное событие %s", kind or "?")
    except asyncio.TimeoutError:
        log.error("Yandex Realtime не ответил на session.update за %.1f с", settings.yandex_realtime_timeout)
        return 1
    except RealtimeError as exc:
        log.error("%s", exc)
        return 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args()
    return asyncio.run(_check(args.env_file))


if __name__ == "__main__":
    raise SystemExit(main())
