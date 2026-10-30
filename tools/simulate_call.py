"""Симулятор Asterisk: подключается к боту по AudioSocket и проходит диалог.

Позволяет проверить бота без АТС: отправляет UUID канала, слушает промпты,
«нажимает» DTMF-цифры и ждёт завершения вызова.

    python -m app --port 9092            # в одном терминале
    python tools/simulate_call.py --digits 0
    python tools/simulate_call.py --digits 19 --interval 1.5 --save recordings/sim.wav
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audiosocket.protocol import (  # noqa: E402 - путь к пакету добавляется выше
    FrameKind,
    encode_dtmf,
    encode_frame,
    kind_name,
    read_frame,
)

SAMPLE_RATE = 8000


async def _call(args: argparse.Namespace) -> int:
    reader, writer = await asyncio.open_connection(args.host, args.port)
    channel = uuid.uuid4()
    writer.write(encode_frame(FrameKind.UUID, channel.bytes))
    await writer.drain()
    print(f"канал {channel}: подключились к {args.host}:{args.port}")

    audio = bytearray()
    seen: dict[str, int] = {}

    async def listen() -> None:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                return
            seen[kind_name(frame.kind)] = seen.get(kind_name(frame.kind), 0) + 1
            if frame.kind == FrameKind.AUDIO_8K:
                audio.extend(frame.payload)

    listen_task = asyncio.create_task(listen())
    try:
        for digit in args.digits:
            await asyncio.sleep(args.interval)
            writer.write(encode_dtmf(digit))
            await writer.drain()
            print(f"-> DTMF {digit}")
        await asyncio.wait_for(listen_task, timeout=args.timeout)
    except asyncio.TimeoutError:
        print(f"! за {args.timeout:.0f} с соединение не закрылось — прерываем")
        listen_task.cancel()
    finally:
        writer.close()

    print(f"фреймы от бота: {seen or 'нет'}")
    print(f"получено аудио: {len(audio) / (SAMPLE_RATE * 2):.2f} с")
    if args.save and audio:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(audio))
        print(f"промпты сохранены: {path}")

    return 0 if audio else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Симулятор Asterisk для AudioSocket-бота")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9092)
    parser.add_argument("--digits", default="0", help="строка DTMF-цифр, например 19")
    parser.add_argument("--interval", type=float, default=2.0, help="пауза перед каждой цифрой, с")
    parser.add_argument("--timeout", type=float, default=30.0, help="ожидание закрытия вызова, с")
    parser.add_argument("--save", default="", help="сохранить полученные промпты в WAV")
    args = parser.parse_args(argv)
    return asyncio.run(_call(args))


if __name__ == "__main__":
    raise SystemExit(main())
