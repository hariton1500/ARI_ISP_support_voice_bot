"""Генерация WAV-заглушек промптов (8 кГц, моно, PCM16).

Нужна, чтобы каркас проверялся без TTS: каждый шаг сценария озвучивается
узнаваемым набором тонов. Реальные промпты (запись диктора или TTS)
кладутся в prompts/ru, после чего достаточно указать PROMPTS_DIR=prompts/ru.

    python tools/make_placeholder_prompts.py
    python tools/make_placeholder_prompts.py --out-dir prompts/placeholder --tone-ms 150
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 8000
LEAD_MS = 200
TAIL_MS = 200
GAP_MS = 60

# имя файла -> (частоты тонов, длительность тона, мс)
PLACEHOLDERS: dict[str, tuple[tuple[int, ...], int]] = {
    "greeting": ((440,), 200),
    "dialog": ((440, 660, 880), 200),
    "no_input": ((300, 300), 180),
    "operator": ((520, 600, 680, 760, 840, 920, 1000), 140),
    "fallback": ((400, 500, 600, 700, 800, 900, 1000, 1100), 140),
}


def _tone(freq: int, duration_ms: int, amplitude: float = 0.25) -> np.ndarray:
    length = int(SAMPLE_RATE * duration_ms / 1000)
    if length <= 0:
        return np.zeros(0, dtype=np.int16)
    time = np.arange(length) / SAMPLE_RATE
    envelope = np.ones(length)
    fade = min(int(SAMPLE_RATE * 0.01), length // 2)  # 10 мс на фронты, чтобы не щёлкало
    if fade:
        envelope[:fade] = np.linspace(0.0, 1.0, fade)
        envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (np.sin(2 * np.pi * freq * time) * amplitude * envelope * 32767).astype(np.int16)


def _silence(duration_ms: int) -> np.ndarray:
    return np.zeros(max(0, int(SAMPLE_RATE * duration_ms / 1000)), dtype=np.int16)


def render(freqs: tuple[int, ...], tone_ms: int, gap_ms: int = GAP_MS) -> np.ndarray:
    parts = [_silence(LEAD_MS)]
    for index, freq in enumerate(freqs):
        parts.append(_tone(freq, tone_ms))
        if index != len(freqs) - 1:
            parts.append(_silence(gap_ms))
    parts.append(_silence(TAIL_MS))
    return np.concatenate(parts)


def write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.astype("<i2").tobytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сгенерировать WAV-заглушки промптов")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "prompts" / "placeholder",
        help="каталог для WAV-файлов",
    )
    parser.add_argument(
        "--tone-ms",
        type=int,
        default=0,
        help="длительность тона в мс (0 — значения по умолчанию)",
    )
    args = parser.parse_args(argv)

    for name, (freqs, tone_ms) in PLACEHOLDERS.items():
        path = args.out_dir / f"{name}.wav"
        write_wav(path, render(freqs, args.tone_ms or tone_ms))
        print(f"{path.name}: тонов {len(freqs)}, {path.stat().st_size} байт")
    print(f"готово: {len(PLACEHOLDERS)} файлов в {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
