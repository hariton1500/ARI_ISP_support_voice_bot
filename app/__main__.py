"""Точка входа бота.

    python -m app                  # настройки из .env, ARI включается автоматически
    python -m app --check          # проверить сценарий и промпты
    python -m app --ari-check      # проверить REST и WebSocket ARI
    python -m app --billing-check 1  # запросить абонента в биллинге и выйти
    python -m app --no-ari         # автономный режим (без Asterisk, для отладки)
    python -m app --port 9093      # переопределить порт AudioSocket
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from app.ari.client import AriClient
from app.ari.events import AriEventStream
from app.ari.manager import AriCallManager
from app.audiosocket.registry import PendingRegistry
from app.audiosocket.server import AudioSocketServer
from app.billing.client import BillingClient, BillingError
from app.config import Settings, load_settings
from app.dialog.prompts import load_prompts
from app.dialog.scenario import Scenario, ScenarioError
from app.logging_setup import setup_logging
from app.network import local_ipv4_addresses, primary_ipv4, public_ipv4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Голосовой бот службы поддержки (Asterisk ARI + AudioSocket)",
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
        "--ari",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="включить ARI (по умолчанию — если заданы ARI_* в .env)",
    )
    parser.add_argument(
        "--ari-check",
        action="store_true",
        help="проверить доступность ARI (REST + WebSocket) и выйти",
    )
    parser.add_argument(
        "--external-media-host",
        default=None,
        help="адрес, по которому Asterisk подключается к AudioSocket",
    )
    parser.add_argument(
        "--external-media-port", type=int, default=None, help="порт для ExternalMedia"
    )
    parser.add_argument(
        "--allow-unknown-sessions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="принимать AudioSocket-сессии без зарегистрированного вызова",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить сценарий и промпты и выйти с кодом 0",
    )
    parser.add_argument(
        "--billing-check",
        metavar="USER_ID",
        default=None,
        help="запросить абонента в биллинге по userId и выйти",
    )
    parser.add_argument(
        "--billing-mock",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="отдавать тестовые данные биллинга без обращения к сети",
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
        external_media_host=args.external_media_host,
        external_media_port=args.external_media_port,
        allow_unknown_sessions=args.allow_unknown_sessions,
        billing_mock=args.billing_mock,
    )


def _resolve_media_host(settings: Settings, log: logging.Logger) -> Settings:
    """Подставить адрес для ExternalMedia, если он не задан явно."""
    if settings.external_media_host:
        return settings
    host = primary_ipv4()
    if host is None:
        raise ValueError(
            "не удалось определить адрес для ExternalMedia — задайте EXTERNAL_MEDIA_HOST"
        )
    log.warning(
        "EXTERNAL_MEDIA_HOST не задан, использую %s. Если Asterisk вне локальной сети, "
        "укажите публичный адрес/домен и проброс порта %d",
        host,
        settings.media_port,
    )
    return settings.with_overrides(external_media_host=host)


async def _noop_event(event: dict[str, Any]) -> None:
    return None


async def _billing_check(settings: Settings, user_id: str, log: logging.Logger) -> int:
    """Запросить абонента в биллинге и напечатать ответ (без запуска бота)."""
    if not settings.billing_enabled:
        log.error(
            "биллинг не настроен: нужны BILLING_BASE_URL и BILLING_TOKEN "
            "(или BILLING_MOCK=1/--billing-mock)"
        )
        return 2
    async with BillingClient(settings, logger=log) as billing:
        try:
            subscriber = await billing.fetch_subscriber(user_id)
        except BillingError as exc:
            log.error("биллинг: %s", exc)
            return 1
    if subscriber is None:
        log.info("биллинг: абонент %r не найден", user_id)
        return 1
    log.info("биллинг: абонент %r — %s", user_id, subscriber.summary)
    print(json.dumps(subscriber.to_dict(), ensure_ascii=False, indent=2))
    return 0


async def _ari_check(settings: Settings, log: logging.Logger) -> int:
    """Проверить REST и WebSocket ARI, не поднимая бота."""
    log.info("локальные адреса: %s", ", ".join(local_ipv4_addresses()) or "нет")
    public = await asyncio.to_thread(public_ipv4)
    if public:
        log.info("внешний IP: %s (проверьте, что порт %d открыт с 91.195.230.13)",
                 public, settings.media_port)
    try:
        async with AriClient(settings, logger=log) as client:
            info = await client.asterisk_info()
            system = info.get("system") or {}
            log.info("ARI REST доступен: Asterisk %s", system.get("version", "?"))
    except Exception as exc:  # noqa: BLE001 - диагностика
        log.error("ARI REST недоступен: %s", exc)
        return 1

    stream = AriEventStream(
        settings.ws_url,
        settings.ari_user or "",
        settings.ari_password or "",
        _noop_event,
        logger=log,
    )
    task = asyncio.create_task(stream.run(), name="ari-check")
    try:
        await asyncio.wait_for(stream.connected.wait(), timeout=10.0)
        log.info("ARI WebSocket подключён: %s", settings.ws_url)
        return 0
    except asyncio.TimeoutError:
        log.error("ARI WebSocket не подключился за 10 с: %s", settings.ws_url)
        return 1
    except Exception as exc:  # noqa: BLE001 - диагностика
        log.error("ARI WebSocket: ошибка: %s", exc)
        return 1
    finally:
        await stream.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _serve(
    settings: Settings,
    scenario: Scenario,
    prompts: dict[str, bytes],
    *,
    use_ari: bool,
) -> None:
    log = logging.getLogger("app")
    billing_needed = any(step.billing for step in scenario.steps.values())
    billing: BillingClient | None = None
    if settings.billing_enabled:
        billing = BillingClient(settings, logger=log)
        await billing.start()
        log.info(
            "биллинг: %s%s",
            settings.billing_base_url or "—",
            " (BILLING_MOCK)" if settings.billing_mock else "",
        )
    elif billing_needed:
        log.warning(
            "в сценарии есть шаги с billing, но биллинг не настроен: "
            "задайте BILLING_BASE_URL и BILLING_TOKEN (или BILLING_MOCK=1)"
        )

    registry = PendingRegistry()
    server = AudioSocketServer(
        settings,
        scenario,
        prompts,
        registry=registry if use_ari else None,
        billing=billing,
    )
    await server.start()
    addresses = ", ".join(local_ipv4_addresses()) or "нет внешних адресов"
    log.info(
        "бот готов: сценарий %r (%d шагов), промптов %d, AudioSocket порт %s, адреса: %s",
        scenario.name,
        len(scenario.steps),
        len(prompts),
        server.bound_port,
        addresses,
    )

    manager: AriCallManager | None = None
    if use_ari:
        manager = AriCallManager(settings, registry)
        await manager.start()
        log.info(
            "ARI: приложение %r, события %s, ExternalMedia -> %s",
            settings.ari_app,
            settings.ws_url,
            manager.media_target,
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
        if manager is not None:
            await manager.stop()
        await server.close()
        if billing is not None:
            await billing.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _resolve_settings(args)
    except (TypeError, ValueError) as exc:
        print(f"ошибка настроек: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, secrets=(settings.ari_password, settings.billing_token))
    log = logging.getLogger("app")
    log.debug("настройки: %s", settings.masked())

    use_ari = args.ari if args.ari is not None else settings.ari_enabled
    if use_ari and not settings.ari_enabled:
        log.error("ARI включён, но не заданы ARI_URL/ARI_USER/ARI_PASSWORD/ARI_APP")
        return 2
    if use_ari:
        try:
            settings = _resolve_media_host(settings, log)
        except ValueError as exc:
            log.error("%s", exc)
            return 2

    if args.ari_check:
        if not use_ari:
            log.error("--ari-check требует настроенного ARI")
            return 2
        return asyncio.run(_ari_check(settings, log))

    if args.billing_check is not None:
        return asyncio.run(_billing_check(settings, args.billing_check, log))

    try:
        scenario = Scenario.load(settings.scenario_path)
    except ScenarioError as exc:
        log.error("сценарий не загружен: %s", exc)
        return 2

    prompts = load_prompts(scenario, settings.prompts_dir, log)

    if args.check:
        missing = [name for name in scenario.prompt_names if name not in prompts]
        log.info(
            "проверка пройдена: сценарий %r, шагов %d, промптов %d/%d%s",
            scenario.name,
            len(scenario.steps),
            len(prompts),
            len(scenario.prompt_names),
            ", ARI: включён" if use_ari else ", ARI: выключен",
        )
        for name in missing:
            log.warning("нет промпта: %s", settings.prompts_dir / name)
        return 0

    try:
        asyncio.run(_serve(settings, scenario, prompts, use_ari=use_ari))
    except KeyboardInterrupt:
        log.info("остановка по Ctrl+C")
    except OSError as exc:
        log.error("не удалось запустить сервер: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
