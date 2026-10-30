"""Запись входящего звука вызова в WAV-файл (8 кГц, моно, 16 бит)."""

from __future__ import annotations

import wave
from pathlib import Path
from types import TracebackType

from app.audio.pcm import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH


class WavRecorder:
    """Инкрементальная запись PCM в WAV.

    Заголовок WAV дописывается при close(); данные сбрасываются на диск после
    каждого фрейма, поэтому запись выживает и при аварийном завершении процесса
    (файл при этом может остаться без корректного заголовка).
    """

    def __init__(
        self,
        path: Path,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        sample_width: int = SAMPLE_WIDTH,
    ) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self._wave: wave.Wave_write | None = None
        self._file = None
        self._bytes_written = 0

    @property
    def is_open(self) -> bool:
        return self._wave is not None

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def duration_ms(self) -> float:
        frame_bytes = self.sample_width * self.channels
        if not self._bytes_written or not frame_bytes:
            return 0.0
        frames = self._bytes_written // frame_bytes
        return frames / self.sample_rate * 1000.0

    def open(self) -> None:
        if self._wave is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._wave = wave.open(self._file, "wb")
        self._wave.setnchannels(self.channels)
        self._wave.setsampwidth(self.sample_width)
        self._wave.setframerate(self.sample_rate)

    def write(self, pcm: bytes) -> int:
        """Дописать PCM; возвращает число записанных байт."""
        wav = self._wave
        if wav is None or not pcm:
            return 0
        frame_bytes = self.sample_width * self.channels
        if len(pcm) % frame_bytes:
            pcm = pcm[: len(pcm) - len(pcm) % frame_bytes]
        if not pcm:
            return 0
        wav.writeframes(pcm)
        if self._file is not None:
            self._file.flush()
        self._bytes_written += len(pcm)
        return len(pcm)

    def close(self) -> None:
        wav, self._wave = self._wave, None
        if wav is not None:
            try:
                wav.close()
            finally:
                self._file = None

    def __enter__(self) -> WavRecorder:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
