"""Проверка доставки AudioSocket от Asterisk (ExternalMedia).

Инструмент поднимает временный TCP-листенер, открывает WebSocket ARI (чтобы
приложение `ivr_ai` существовало), просит Asterisk создать медиаканал на
указанный адрес и ждёт входящее TCP-соединение с нашим UUID.

    python tools/check_external_media.py --host 176.101.1.177 --port 9092
    python tools/check_external_media.py --host 172.18.185.162 --port 9092

Временный канал после проверки удаляется. Запускать при остановленном боте
(порт должен быть свободен) или указать другой `--listen-port` вместе с
`--port` — но тогда проверяется не тот порт, что в бою.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ari.client import AriClient  # noqa: E402 - путь к пакету добавляется выше
from app.ari.events import AriEventStream  # noqa: E402
from app.audiosocket.protocol import read_uuid_frame  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402


async def _noop_event(event: dict) -> None:
    return None


async def _check(args: argparse.Namespace) -> int:
    log = logging.getLogger("check")
    settings = load_settings(args.env_file)
    if not settings.ari_enabled:
        log.error("ARI не настроен: нужны ARI_URL/ARI_USER/ARI_PASSWORD/ARI_APP")
        return 2

    expected_uuid = str(uuid.uuid4())
    connected = asyncio.Event()
    received: dict[str, str] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("входящее TCP-соединение от %s", peer)
        try:
            frame_uuid = await asyncio.wait_for(read_uuid_frame(reader), timeout=5.0)
            received["uuid"] = str(frame_uuid) if frame_uuid else ""
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            log.warning("ошибка чтения UUID: %s", exc)
        finally:
            connected.set()
            with contextlib.suppress(Exception):
                writer.close()

    listener = await asyncio.start_server(handle, host="0.0.0.0", port=args.listen_port)
    events = AriEventStream(
        settings.ws_url, settings.ari_user or "", settings.ari_password or "", _noop_event
    )
    events_task = asyncio.create_task(events.run(), name="ari-check-events")

    try:
        try:
            await asyncio.wait_for(events.connected.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.error("ARI WebSocket не подключился: %s", settings.ws_url)
            return 1

        async with AriClient(settings) as client:
            log.info(
                "просим Asterisk подключиться к %s:%d (encapsulation=audiosocket)",
                args.host,
                args.port,
            )
            media = await client.create_external_media(
                app=settings.ari_app or "",
                external_host=f"{args.host}:{args.port}",
                data=expected_uuid,
                media_format=settings.external_media_format,
                transport=settings.external_media_transport,
                encapsulation=settings.external_media_encapsulation,
            )
            channel_id = str(media["id"])
            log.info("создан медиаканал %s (состояние %s)", channel_id, media.get("state"))
            try:
                await asyncio.wait_for(connected.wait(), timeout=args.timeout)
            except asyncio.TimeoutError:
                log.error(
                    "TCP-соединение от Asterisk не пришло за %.1f с: адрес %s:%d недоступен. "
                    "Проверьте файрвол и проброс порта",
                    args.timeout,
                    args.host,
                    args.port,
                )
                result = 1
            else:
                if received.get("uuid") == expected_uuid:
                    log.info("УСПЕХ: Asterisk подключился и прислал ожидаемый UUID")
                    result = 0
                else:
                    log.error(
                        "UUID не совпал: ожидали %s, получили %r",
                        expected_uuid,
                        received.get("uuid") or "ничего",
                    )
                    result = 1
            finally:
                with contextlib.suppress(Exception):
                    await client.delete_channel(channel_id)
            return result
    finally:
        listener.close()
        with contextlib.suppress(Exception):
            await listener.wait_closed()
        await events.stop()
        events_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await events_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить доставку ExternalMedia/AudioSocket")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument(
        "--host",
        required=True,
        help="адрес, который Asterisk будет набирать (тот, что пойдёт в external_host)",
    )
    parser.add_argument("--port", type=int, default=9092, help="порт для external_host")
    parser.add_argument(
        "--listen-port",
        type=int,
        default=None,
        help="локальный порт листенера (по умолчанию совпадает с --port)",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="ожидание TCP, с")
    args = parser.parse_args(argv)
    if args.listen_port is None:
        args.listen_port = args.port

    setup_logging("INFO")
    return asyncio.run(_check(args))


if __name__ == "__main__":
    raise SystemExit(main())
