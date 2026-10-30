"""Точка входа бота.

    python -m app                 # запуск с настройками из .env
    python -m app --check         # проверить сценарий и промпты, не поднимая порт
    python -m app --port 9093     # переопределить порт AudioSocket
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from app.audiosocket.server import AudioSocketServer
from app.config import Settings, load_settings
from app.dialog.prompts import load_prompts
from app.dialog.scenario import Scenario, ScenarioError
from app.logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Голосовой бот службы поддержки на AudioSocket (Asterisk)",
    )
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    parser.add_argument("--host", default=None, help="адрес прослушивания AudioSocket")
    parser.add_argument("--port", type=int, default=None, help="порт AudioSocket")
    parser.add_argument("--scenario", type=Path, default=None, help="путь к JSON-сценарию")
    parser.add_argument("--prompts-dir", type=Path, default=None, help="каталог с промптами")
    parser.add_argument("--recordings-dir", type=Path, default=None, help="каталог записей")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument(
        "--record",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="писать входящий звук в WAV (--no-record отключает)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить сценарий и промпты и выйти с кодом 0",
    )
    return parser


def _resolve_settings(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.env_file)
    return settings.with_overrides(
        audiosocket_host=args.host,
        audiosocket_port=args.port,
        scenario_path=args.scenario,
        prompts_dir=args.prompts_dir,
        recordings_dir=args.recordings_dir,
        log_level=args.log_level.upper() if args.log_level else None,
        record_calls=args.record,
    )


async def _serve(settings: Settings, scenario: Scenario, prompts: dict[str, bytes]) -> None:
    log = logging.getLogger("app")
    server = AudioSocketServer(settings, scenario, prompts)
    await server.start()
    log.info(
        "бот готов: сценарий %r (%d шагов), промптов %d, порт %s",
        scenario.name,
        len(scenario.steps),
        len(prompts),
        server.bound_port,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
        log.info("получен сигнал остановки")
    finally:
        await server.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _resolve_settings(args)
    except (TypeError, ValueError) as exc:
        print(f"ошибка настроек: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, secrets=(settings.ari_password,))
    log = logging.getLogger("app")
    log.debug("настройки: %s", settings.masked())

    try:
        scenario = Scenario.load(settings.scenario_path)
    except ScenarioError as exc:
        log.error("сценарий не загружен: %s", exc)
        return 2

    prompts = load_prompts(scenario, settings.prompts_dir, log)

    if args.check:
        missing = [name for name in scenario.prompt_names if name not in prompts]
        log.info(
            "проверка пройдена: сценарий %r, шагов %d, промптов %d/%d",
            scenario.name,
            len(scenario.steps),
            len(prompts),
            len(scenario.prompt_names),
        )
        for name in missing:
            log.warning("нет промпта: %s", settings.prompts_dir / name)
        return 0

    try:
        asyncio.run(_serve(settings, scenario, prompts))
    except KeyboardInterrupt:
        log.info("остановка по Ctrl+C")
    except OSError as exc:
        log.error("не удалось запустить сервер: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
