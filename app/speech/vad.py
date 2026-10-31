"""VAD (voice activity detection) и эндпоинтинг по энергии сигнала.

Asterisk присылает PCM 8 кГц / 16 бит, поэтому достаточно RMS по кадру 20 мс.
Порог адаптивный: к шумовой дорожке добавляется запас `noise_multiplier`, но не
ниже `rms_threshold` — так VAD не срабатывает на фоновый шум и не «залипает» на
тихой линии.

Эндпоинтинг: речь начинается после `start_ms` подряд идущих громких кадров и
заканчивается после `silence_ms` тишины. Слишком короткие реплики
отбрасываются на уровне `StreamingVoiceInput`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from app.audio.pcm import FRAME_MS, SAMPLE_RATE, SAMPLE_WIDTH

__all__ = ["VadConfig", "VadEvent", "VadEventKind", "VoiceActivityDetector", "rms"]


def rms(pcm: bytes) -> float:
    """Среднеквадратичное значение 16-битного PCM (в единицах сэмпла)."""
    if len(pcm) < 2:
        return 0.0
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class VadEventKind(Enum):
    START = "start"
    END = "end"


@dataclass(frozen=True, slots=True)
class VadEvent:
    """Событие VAD: начало или конец речи."""

    kind: VadEventKind
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class VadConfig:
    """Настройки VAD. Все длительности — в миллисекундах."""

    frame_ms: int = FRAME_MS
    start_ms: int = 160
    silence_ms: int = 700
    min_speech_ms: int = 200
    max_speech_ms: int = 15_000
    rms_threshold: float = 300.0
    noise_multiplier: float = 2.5

    def __post_init__(self) -> None:
        if self.frame_ms <= 0:
            raise ValueError("frame_ms должен быть положительным")
        if self.start_ms < self.frame_ms:
            raise ValueError("start_ms не может быть меньше кадра")
        if self.silence_ms <= 0:
            raise ValueError("silence_ms должен быть положительным")
        if self.min_speech_ms < 0 or self.max_speech_ms <= 0:
            raise ValueError("min_speech_ms/max_speech_ms заданы неверно")
        if self.rms_threshold <= 0 or self.noise_multiplier <= 0:
            raise ValueError("пороги VAD должны быть положительными")

    @property
    def frame_bytes(self) -> int:
        return SAMPLE_RATE * self.frame_ms // 1000 * SAMPLE_WIDTH

    @property
    def start_frames(self) -> int:
        return max(1, self.start_ms // self.frame_ms)

    @property
    def silence_frames(self) -> int:
        return max(1, self.silence_ms // self.frame_ms)

    @property
    def max_speech_frames(self) -> int:
        return max(1, self.max_speech_ms // self.frame_ms)


class VoiceActivityDetector:
    """Потоковый детектор речи: по одному кадру PCM за вызов."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self._noise_floor = 0.0
        self._voiced_run = 0
        self._speech_frames = 0
        self._silence_run = 0
        self._speaking = False

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def threshold(self) -> float:
        """Текущий порог срабатывания (для логов и тестов)."""
        return max(self.config.rms_threshold, self._noise_floor * self.config.noise_multiplier)

    def reset(self) -> None:
        self._voiced_run = 0
        self._speech_frames = 0
        self._silence_run = 0
        self._speaking = False

    def feed(self, frame: bytes) -> VadEvent | None:
        """Обработать кадр и, если надо, вернуть событие начала/конца речи."""
        level = rms(frame)
        if not self._speaking:
            # Шумовую дорожку обновляем только на тихих кадрах. Иначе начало
            # громкой речи поднимает порог и VAD «глохнет» на собственной речи.
            voiced = level >= self.threshold
            if not voiced:
                self._noise_floor = self._noise_floor * 0.95 + level * 0.05
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run >= self.config.start_frames:
                self._speaking = True
                self._speech_frames = self._voiced_run
                self._silence_run = 0
                self._voiced_run = 0
                return VadEvent(VadEventKind.START, self._speech_frames * self.config.frame_ms)
            return None

        self._speech_frames += 1
        if level >= self.threshold:
            self._silence_run = 0
        else:
            self._silence_run += 1

        duration_ms = self._speech_frames * self.config.frame_ms
        if self._silence_run >= self.config.silence_frames:
            self.reset()
            return VadEvent(VadEventKind.END, duration_ms)
        if self._speech_frames >= self.config.max_speech_frames:
            self.reset()
            return VadEvent(VadEventKind.END, duration_ms)
        return None
