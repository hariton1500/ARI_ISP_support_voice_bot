"""Сохранить исходный TTS PCM и результат текущего ресемплера в WAV.

Запуск из корня проекта:

    python tools/save_tts_comparison.py "Здравствуйте! Это проверка качества речи."

По умолчанию файлы появляются в recordings/tts-comparison/:
    original-24000.wav — исходный поток AITUNNEL без преобразования;
    current-8000.wav   — тот же поток после PcmStreamResampler проекта.

Команда делает один платный запрос к TTS-провайдеру. Ключ и модель берутся из
.env; response_format=pcm задаётся принудительно для корректного сравнения.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402 - путь к пакету добавляется выше

from app.audio.pcm import SAMPLE_RATE, PcmStreamResampler  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    """Упаковать PCM16 mono в обычный WAV, который откроет проигрыватель."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


async def _save_comparison(
    settings: Settings,
    *,
    text: str,
    output_dir: Path,
    model: str | None = None,
    voice: str | None = None,
) -> tuple[Path, Path]:
    if not settings.speech_enabled:
        raise RuntimeError("речь не настроена: задайте AITUNNEL_API_KEY в .env")
    if not text.strip():
        raise RuntimeError("текст для синтеза не должен быть пустым")

    source_rate = settings.tts_pcm_rate
    resampler = PcmStreamResampler(source_rate, SAMPLE_RATE)
    original = bytearray()
    converted = bytearray()
    url = f"{settings.ai_tunnel_base_url.rstrip('/')}/audio/speech"
    payload = {
        "model": model or settings.tts_model,
        "input": text.strip(),
        "voice": voice or settings.tts_voice,
        "response_format": "pcm",
    }
    headers = {"Authorization": f"Bearer {settings.ai_tunnel_api_key}"}
    timeout = aiohttp.ClientTimeout(
        total=settings.speech_timeout,
        connect=min(settings.speech_timeout, 5.0),
    )

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    body = await response.read()
                    detail = " ".join(body.decode("utf-8", errors="replace").split())[:300]
                    raise RuntimeError(f"TTS вернул HTTP {response.status}: {detail}")
                async for chunk in response.content.iter_chunked(4096):
                    original.extend(chunk)
                    for block in resampler.feed(chunk):
                        converted.extend(block)
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"не удалось получить TTS: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError("таймаут запроса к TTS") from exc

    converted.extend(resampler.flush())
    if not original:
        raise RuntimeError("TTS вернул пустой аудиопоток")

    original_path = output_dir / f"original-{source_rate}.wav"
    converted_path = output_dir / f"current-{SAMPLE_RATE}.wav"
    _write_wav(original_path, bytes(original), source_rate)
    _write_wav(converted_path, bytes(converted), SAMPLE_RATE)
    return original_path, converted_path


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    output_dir = args.output_dir or settings.recordings_dir / "tts-comparison"
    original, converted = await _save_comparison(
        settings,
        text=args.text,
        output_dir=output_dir,
        model=args.model,
        voice=args.voice,
    )
    print(f"Исходный TTS:       {original.resolve()}")
    print(f"Текущий ресемплинг: {converted.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сохранить исходный TTS и результат текущего ресемплера в WAV"
    )
    parser.add_argument("text", help="текст, который нужно синтезировать")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    parser.add_argument("--output-dir", type=Path, default=None, help="каталог для WAV")
    parser.add_argument("--model", default=None, help="переопределить TTS_MODEL")
    parser.add_argument("--voice", default=None, help="переопределить TTS_VOICE")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
