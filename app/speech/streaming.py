"""Потоковый сбор реплики: VAD + буфер утterance + промежуточные гипотезы.

AITUNNEL принимает STT-запросы файлом, а не по WebSocket. Поэтому «потоковость»
обеспечивается на нашей стороне: VAD режет речь на реплики, пока абонент
говорит, бот периодически отправляет накопленный фрагмент в STT и получает
промежуточные гипотезы; по тишине отправляется финальный запрос.

Модуль не делает сетевых вызовов — только решает, когда и что отправлять.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from app.speech.vad import VadConfig, VadEventKind, VoiceActivityDetector

__all__ = ["SpeechEvent", "StreamingVoiceInput"]


@dataclass(frozen=True, slots=True)
class SpeechEvent:
    """Начало или конец реплики; в конце приходит PCM всей реплики."""

    kind: VadEventKind
    pcm: bytes = b""
    duration_ms: float = 0.0


class StreamingVoiceInput:
    """Собирает PCM в кадры, находит реплики и отдаёт их целиком."""

    def __init__(self, config: VadConfig | None = None, *, partial_interval_ms: int = 1200) -> None:
        self._vad = VoiceActivityDetector(config)
        self.config = self._vad.config
        self._partial_interval = max(0, partial_interval_ms) / 1000.0
        # Пре-ролл: начало слова VAD замечает через start_ms, поэтому первые
        # кадры реплики нужно сохранить и приклеить к utterance.
        self._preroll: deque[bytes] = deque(maxlen=self.config.start_frames + 2)
        self._pending = b""
        self._utterance = bytearray()
        self._speaking = False
        self._last_partial = 0.0

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def utterance(self) -> bytes:
        return bytes(self._utterance)

    @property
    def utterance_ms(self) -> float:
        return len(self._utterance) / (8000 * 2) * 1000.0

    def feed(self, pcm: bytes, *, now: float | None = None) -> SpeechEvent | None:
        """Добавить PCM; вернуть событие начала/конца речи, если оно случилось."""
        if not pcm:
            return None
        moment = time.monotonic() if now is None else now
        self._pending += pcm
        frame_bytes = self.config.frame_bytes
        event: SpeechEvent | None = None

        while len(self._pending) >= frame_bytes:
            frame = self._pending[:frame_bytes]
            self._pending = self._pending[frame_bytes:]
            vad_event = self._vad.feed(frame)

            if vad_event is not None and vad_event.kind is VadEventKind.START:
                self._speaking = True
                self._last_partial = moment
                self._utterance = bytearray(b"".join(self._preroll))
                self._preroll.clear()
                self._utterance += frame
                event = SpeechEvent(vad_event.kind, duration_ms=vad_event.duration_ms)
            elif self._speaking:
                self._utterance += frame
                if vad_event is not None and vad_event.kind is VadEventKind.END:
                    utterance = bytes(self._utterance)
                    duration = len(utterance) / (8000 * 2) * 1000.0
                    self._speaking = False
                    self._utterance.clear()
                    self._preroll.clear()
                    self._last_partial = 0.0
                    event = SpeechEvent(vad_event.kind, utterance, duration)
            else:
                self._preroll.append(frame)

        return event

    def partial_due(self, *, now: float | None = None) -> bool:
        """Пора ли отправить промежуточную гипотезу STT."""
        if not self._speaking or self._partial_interval <= 0:
            return False
        moment = time.monotonic() if now is None else now
        return moment - self._last_partial >= self._partial_interval

    def partial_audio(self) -> bytes:
        """Снимок накопленной речи для промежуточного распознавания."""
        return bytes(self._utterance)

    def mark_partial(self, *, now: float | None = None) -> None:
        self._last_partial = time.monotonic() if now is None else now

    def reset(self) -> None:
        self._vad.reset()
        self._preroll.clear()
        self._pending = b""
        self._utterance.clear()
        self._speaking = False
        self._last_partial = 0.0
