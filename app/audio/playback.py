"""Пошаговое воспроизведение PCM в AudioSocket-соединение.

Звук отдаётся кусками по 20 мс с реальным темпом: иначе TCP-буфер уедет
вперёд, и перебивание (barge-in, например DTMF во время промпта) потеряет
смысл.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.audio.pcm import FRAME_BYTES, FRAME_MS, chunk_pcm

WriteCallback = Callable[[bytes], Awaitable[None]]


class AudioPlayer:
    """Проигрыватель одного промпта с возможностью досрочной остановки."""

    def __init__(
        self,
        write: WriteCallback,
        *,
        chunk_bytes: int = FRAME_BYTES,
        frame_ms: int = FRAME_MS,
    ) -> None:
        if chunk_bytes <= 0 or frame_ms <= 0:
            raise ValueError("chunk_bytes и frame_ms должны быть положительными")
        self._write = write
        self._chunk_bytes = chunk_bytes
        self._frame_seconds = frame_ms / 1000.0
        self._stop = asyncio.Event()
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    async def play(self, pcm: bytes) -> bool:
        """Проиграть PCM. Возвращает True, если промпт доигран до конца."""
        if not pcm:
            return True

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        self._stop = stop
        self._playing = True
        deadline = loop.time()
        completed = True
        try:
            for chunk in chunk_pcm(pcm, self._chunk_bytes):
                if stop.is_set() or len(chunk) < 2:
                    completed = False
                    break
                await self._write(chunk)
                deadline += self._frame_seconds
                delay = deadline - loop.time()
                if delay <= 0:
                    continue
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    continue
                else:
                    completed = False
                    break
        finally:
            self._playing = False
        return completed

    def stop(self) -> None:
        """Прервать текущее воспроизведение (безопасно вызывать всегда)."""
        self._stop.set()
