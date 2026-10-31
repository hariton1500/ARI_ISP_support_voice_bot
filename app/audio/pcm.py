"""PCM-хелперы.

AudioSocket передаёт звук как 16-битный знаковый линейный PCM, little-endian,
моно, 8 кГц (фрейм 0x10). Промпты могут быть записаны с другой частотой
дискретизации — при загрузке они приводятся к 8 кГц.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

# Формат обмена с Asterisk (AudioSocket, фрейм 0x10).
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
CHANNELS = 1

# Один фрейм — 20 мс звука, как у Asterisk (50 фреймов в секунду).
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = SAMPLES_PER_FRAME * SAMPLE_WIDTH

PCM16_DTYPE = np.dtype("<i2")

_SILENT_FRAME = b"\x00" * FRAME_BYTES

# Расширения «сырого» PCM, которые понимает Asterisk.
_RAW_PCM_RATES = {".sln": 8000, ".sln8": 8000, ".pcm": 8000, ".raw": 8000, ".sln16": 16000}


def chunk_pcm(pcm: bytes, size: int = FRAME_BYTES) -> Iterator[bytes]:
    """Нарезать поток на куски фиксированного размера (последний может быть короче)."""
    if size <= 0:
        raise ValueError("размер куска должен быть положительным")
    for offset in range(0, len(pcm), size):
        yield pcm[offset : offset + size]


def silence_ms(ms: int | float) -> bytes:
    """Сгенерировать тишину заданной длительности, кратной фрейму 20 мс."""
    frames = max(0, int(round(float(ms) / FRAME_MS)))
    return _SILENT_FRAME * frames


def duration_ms(pcm: bytes) -> float:
    """Длительность PCM в миллисекундах."""
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH) * 1000.0


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Линейный ресемплинг 16-битного моно PCM."""
    if src_rate == dst_rate or not pcm:
        return pcm
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("частота дискретизации должна быть положительной")

    samples = np.frombuffer(pcm, dtype=PCM16_DTYPE)
    if samples.size == 0:
        return b""
    if samples.size == 1:
        return pcm

    out_len = max(1, int(round(samples.size * dst_rate / src_rate)))
    positions = np.linspace(0.0, samples.size - 1, out_len, dtype=np.float64)
    left = np.floor(positions).astype(np.int64)
    frac = positions - left
    right = np.minimum(left + 1, samples.size - 1)
    values = samples[left] * (1.0 - frac) + samples[right] * frac
    return np.clip(np.rint(values), -32768, 32767).astype(PCM16_DTYPE).tobytes()


def _to_mono_pcm16(data: bytes, sample_width: int, channels: int) -> bytes:
    """Привести сырые сэмплы любого целочисленного формата к моно PCM16."""
    if channels <= 0:
        raise ValueError("число каналов должно быть положительным")
    bytes_per_frame = sample_width * channels
    if bytes_per_frame <= 0:
        raise ValueError("некорректный формат сэмплов")
    data = data[: len(data) - len(data) % bytes_per_frame]
    if not data:
        return b""

    if sample_width == 1:
        samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(data, dtype=PCM16_DTYPE).astype(np.float32) / 32768.0
    elif sample_width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        ints = np.where(ints >= (1 << 23), ints - (1 << 24), ints)
        samples = ints.astype(np.float32) / float(1 << 23)
    elif sample_width == 4:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f"неподдерживаемая разрядность сэмпла: {sample_width} байт")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    return np.clip(np.rint(samples * 32767.0), -32768, 32767).astype(PCM16_DTYPE).tobytes()


def read_wav(path: Path) -> bytes:
    """Прочитать WAV и вернуть PCM16 моно 8 кГц."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    pcm = _to_mono_pcm16(frames, sample_width, channels)
    return pcm if rate == SAMPLE_RATE else resample(pcm, rate, SAMPLE_RATE)


def load_prompt(path: Path) -> bytes:
    """Загрузить промпт (*.wav или «сырой» PCM Asterisk) в формате AudioSocket."""
    suffix = path.suffix.lower()
    if suffix in {".wav", ".wave"}:
        return read_wav(path)
    if suffix in _RAW_PCM_RATES:
        data = path.read_bytes()
        data = data[: len(data) - len(data) % SAMPLE_WIDTH]
        rate = _RAW_PCM_RATES[suffix]
        return data if rate == SAMPLE_RATE else resample(data, rate, SAMPLE_RATE)
    raise ValueError(
        f"неподдерживаемый формат промпта {path.name!r}: ожидался WAV или sln/sln16/pcm"
    )


def to_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Упаковать PCM16 моно в WAV (для multipart-запроса STT)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class PcmStreamResampler:
    """Потоковый ресемплер: режет вход на блоки по 20 мс и приводит к 8 кГц.

    TTS отдаёт PCM 24 кГц кусками произвольного размера. Ресемплировать каждый
    кусок отдельно нельзя — на стыках появятся щелчки. Поэтому куски сначала
    собираются в блоки фиксированной длительности, а остаток отдаётся flush().
    """

    def __init__(
        self,
        src_rate: int,
        dst_rate: int = SAMPLE_RATE,
        *,
        block_ms: int = FRAME_MS,
    ) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("частота дискретизации должна быть положительной")
        if block_ms <= 0:
            raise ValueError("block_ms должен быть положительным")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._block_bytes = max(
            SAMPLE_WIDTH, src_rate * block_ms // 1000 * SAMPLE_WIDTH
        )
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Добавить кусок и вернуть готовые блоки PCM 8 кГц."""
        if data:
            self._buffer += data
        blocks: list[bytes] = []
        while len(self._buffer) >= self._block_bytes:
            block = bytes(self._buffer[: self._block_bytes])
            del self._buffer[: self._block_bytes]
            blocks.append(resample(block, self.src_rate, self.dst_rate))
        return blocks

    def flush(self) -> bytes:
        """Отдать остаток (неполный блок) и очистить буфер."""
        if not self._buffer:
            return b""
        data = bytes(self._buffer)
        self._buffer.clear()
        data = data[: len(data) - len(data) % SAMPLE_WIDTH]
        if not data:
            return b""
        return resample(data, self.src_rate, self.dst_rate)
