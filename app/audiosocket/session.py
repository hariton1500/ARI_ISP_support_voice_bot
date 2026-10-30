"""Обработка одного вызова AudioSocket.

Сессия получает фреймы от Asterisk, проигрывает промпты с реальным темпом,
принимает DTMF, пишет входящий звук в WAV и складывает рядом JSON-метаданные
для разбора инцидентов.

Схема работы с Asterisk:

    Asterisk --(TCP: UUID, PCM, DTMF)--> AudioSocketServer --> CallSession

Asterisk подключается к боту сам (см. диалплан `AudioSocket(${UUID()},host:port)`),
поэтому сервер — это мы, а не Asterisk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid as uuid_module
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.audio.pcm import SAMPLE_RATE, resample
from app.audio.playback import AudioPlayer
from app.audio.recorder import WavRecorder
from app.audiosocket.protocol import (
    AUDIO_KINDS,
    Frame,
    FrameKind,
    encode_audio,
    encode_terminate,
    read_frame,
)
from app.audiosocket.registry import PendingCall
from app.billing.client import BillingError, BillingLookup, Subscriber
from app.config import Settings
from app.dialog.prompts import PromptMap
from app.dialog.scenario import Scenario, Step


class DigitBuffer:
    """Очередь DTMF-цифр.

    asyncio.Queue не подходит: отмена get() может «съесть» уже пришедшую цифру.
    Здесь deque + Event, а признак закрытия позволяет разбудить ожидающего и
    корректно завершить диалог.
    """

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._ready = asyncio.Event()
        self._closed = False

    def push(self, digit: str) -> None:
        if self._closed or not digit:
            return
        self._items.append(digit)
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    @property
    def pending(self) -> int:
        return len(self._items)

    async def get(self, timeout: float | None) -> str | None:
        """Вернуть цифру, None по таймауту; ConnectionAbortedError — вызов завершён."""
        if self._items:
            return self._items.popleft()
        if self._closed:
            raise ConnectionAbortedError("вызов завершён")
        self._ready.clear()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            if self._items:
                return self._items.popleft()
            return None
        if self._items:
            return self._items.popleft()
        raise ConnectionAbortedError("вызов завершён")


@dataclass(slots=True)
class CallStats:
    """Сводка по вызову для логов и sidecar-JSON."""

    started_at: float = field(default_factory=time.monotonic)
    started_wall: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    audio_frames: int = 0
    audio_bytes: int = 0
    dtmf_digits: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    hangup_reason: str = "dialog_end"
    #: Последний ответ биллинга (для sidecar-JSON): outcome, userId и данные абонента.
    billing: dict[str, object] | None = None


class CallSession:
    """Один вызов: чтение фреймов, диалог, запись."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        settings: Settings,
        scenario: Scenario,
        prompts: PromptMap,
        *,
        channel_uuid: uuid_module.UUID,
        pending: PendingCall | None = None,
        billing: BillingLookup | None = None,
        logger: logging.Logger | None = None,
        peer: str | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._settings = settings
        self._scenario = scenario
        self._prompts = prompts
        self._log = logger or logging.getLogger("app.session")
        self._peer = peer
        self._channel_uuid = channel_uuid
        self._pending = pending
        self._billing = billing
        self._channel_id: str | None = None

        self.session_id: str | None = None
        self._digits = DigitBuffer()
        self._player = AudioPlayer(self._send_audio)
        self._recorder: WavRecorder | None = None
        self._recording_path: Path | None = None
        self._closed = False
        self._read_task: asyncio.Task[None] | None = None
        self._stats = CallStats()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> CallStats:
        return self._stats

    async def run(self) -> None:
        """Обработать вызов до завершения диалога или обрыва соединения."""
        try:
            if self._pending is not None:
                self.session_id = self._pending.uuid
                self._channel_id = self._pending.channel_id
                for digit in self._pending.drain_digits():
                    self._digits.push(digit)
            else:
                self.session_id = str(self._channel_uuid)
            self._open_recorder()
            if self._pending is not None:
                self._pending.attach(self)
            self._log.info(
                "вызов %s: диалог начат (канал=%s, peer=%s)",
                self.session_id,
                self._channel_id or "-",
                self._peer or "?",
            )
            self._read_task = asyncio.create_task(
                self._read_frames(), name=f"audiosocket-read-{self.session_id}"
            )

            limit = self._settings.max_call_seconds
            if limit and limit > 0:
                await asyncio.wait_for(self._run_dialog(), timeout=limit)
            else:
                await self._run_dialog()
        except asyncio.CancelledError:
            self._stats.hangup_reason = "cancelled"
            raise
        except asyncio.TimeoutError:
            self._stats.hangup_reason = "max_duration"
            self._log.warning(
                "вызов %s: превышена максимальная длительность %.0f с",
                self.session_id,
                self._settings.max_call_seconds,
            )
        except ConnectionAbortedError:
            self._stats.hangup_reason = "peer_closed"
        except (ConnectionError, OSError) as exc:
            self._stats.hangup_reason = "connection_error"
            self._log.warning("вызов %s: обрыв соединения: %s", self.session_id or "?", exc)
        except Exception:  # noqa: BLE001 - сессия не должна ронять сервер
            self._stats.hangup_reason = "error"
            self._log.exception("вызов %s: неожиданная ошибка", self.session_id or "?")
        finally:
            await self._shutdown()

    def abort(self) -> None:
        """Аварийно прервать вызов (используется при остановке сервера)."""
        self._closed = True
        self._player.stop()
        self._digits.close()
        with contextlib.suppress(Exception):
            self._writer.close()

    # ------------------------------------------------------- приём фреймов

    async def _read_frames(self) -> None:
        """Читать фреймы до конца соединения и разбудить диалог при обрыве."""
        try:
            while True:
                frame = await read_frame(self._reader, timeout=self._settings.idle_timeout or None)
                if frame is None:
                    self._stats.hangup_reason = "peer_closed"
                    break
                if frame.kind in AUDIO_KINDS:
                    self._on_audio(frame)
                elif frame.kind == FrameKind.DTMF:
                    self._on_dtmf(frame)
                elif frame.kind == FrameKind.TERMINATE:
                    self._stats.hangup_reason = "terminated"
                    break
                elif frame.kind == FrameKind.ERROR:
                    self._stats.hangup_reason = "asterisk_error"
                    self._log.warning(
                        "вызов %s: ошибка AudioSocket от Asterisk: %s",
                        self.session_id,
                        frame.payload.hex() or "без кода",
                    )
                    break
                else:
                    self._log.debug("вызов %s: неизвестный фрейм 0x%02x", self.session_id, frame.kind)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._stats.hangup_reason = "idle_timeout"
            self._log.warning(
                "вызов %s: нет аудио дольше %.0f с — завершаем",
                self.session_id,
                self._settings.idle_timeout,
            )
        except (ConnectionError, OSError) as exc:
            self._stats.hangup_reason = "connection_error"
            self._log.warning("вызов %s: соединение потеряно: %s", self.session_id, exc)
        finally:
            self._closed = True
            self._player.stop()
            self._digits.close()

    def _on_audio(self, frame: Frame) -> None:
        pcm = frame.payload
        rate = frame.sample_rate or SAMPLE_RATE
        if rate != SAMPLE_RATE:
            pcm = resample(pcm, rate, SAMPLE_RATE)
        if not pcm:
            return
        if self._stats.audio_frames == 0:
            self._log.info("вызов %s: пошёл звук от абонента", self.session_id)
        self._stats.audio_frames += 1
        self._stats.audio_bytes += len(pcm)
        if self._recorder is not None:
            self._recorder.write(pcm)

    def push_digit(self, digit: str) -> None:
        """Принять DTMF-цифру: из фрейма AudioSocket или события ARI."""
        if self._closed or not digit:
            return
        self._stats.dtmf_digits.append(digit)
        self._log.info("вызов %s: DTMF %r", self.session_id, digit)
        if self._player.is_playing:
            self._player.stop()
        self._digits.push(digit)

    def _on_dtmf(self, frame: Frame) -> None:
        digit = frame.as_dtmf()
        if digit is None:
            self._log.debug("вызов %s: некорректный DTMF-фрейм %r", self.session_id, frame.payload)
            return
        if self._pending is not None:
            # В ARI-режиме Asterisk присылает цифру дважды: фреймом AudioSocket и
            # событием ChannelDtmfReceived. Источник истины — ARI, иначе шаг
            # сработает повторно на возврате в меню.
            self._log.debug(
                "вызов %s: DTMF %r из AudioSocket пропущен (используем событие ARI)",
                self.session_id,
                digit,
            )
            return
        self.push_digit(digit)

    async def _send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise ConnectionAbortedError("вызов завершён")
        self._writer.write(encode_audio(pcm))
        await self._writer.drain()

    # ------------------------------------------------------------- диалог

    async def _run_dialog(self) -> None:
        """Пройти по шагам сценария, начиная с initial."""
        current: str | None = self._scenario.initial
        while current and not self._closed:
            step = self._scenario.steps.get(current)
            if step is None:
                self._log.error("вызов %s: шаг %r отсутствует в сценарии", self.session_id, current)
                break
            self._stats.steps.append(current)
            self._log.info("вызов %s: шаг %s (%s)", self.session_id, current, step.label)
            current = await self._run_step(step)
            if step.hangup:
                self._stats.hangup_reason = "hangup_step"
                break

    async def _run_step(self, step: Step) -> str | None:
        """Проиграть шаг и вернуть имя следующего шага (None — конец диалога)."""
        prompt = await self._resolve_prompt(step)
        attempts = 0
        while True:
            if self._closed:
                raise ConnectionAbortedError("вызов завершён")
            await self._play_prompt(step, prompt)
            if step.hangup:
                return None
            if not step.dtmf:
                return step.next

            if self._closed:
                raise ConnectionAbortedError("вызов завершён")
            attempts += 1
            digit = await self._wait_digit(step)

            if digit is not None and digit in step.dtmf:
                target = step.dtmf[digit]
                self._log.info(
                    "вызов %s: шаг %s, цифра %r -> %s", self.session_id, step.name, digit, target
                )
                return target

            # Исчерпали попытки — уходим по fallback-переходу.
            if attempts >= step.max_attempts:
                target = step.on_invalid if digit is not None else step.on_timeout
                if target is None:
                    target = step.on_timeout
                self._log.info(
                    "вызов %s: шаг %s, попытки исчерпаны (%s) -> %s",
                    self.session_id,
                    step.name,
                    "неверная цифра" if digit is not None else "нет ввода",
                    target or "завершение вызова",
                )
                return target

            self._log.info(
                "вызов %s: шаг %s, %s (попытка %d из %d)",
                self.session_id,
                step.name,
                "неверная цифра" if digit is not None else "нет ввода",
                attempts,
                step.max_attempts,
            )

    async def _play_prompt(self, step: Step, prompt: str | None = None) -> None:
        if self._closed:
            raise ConnectionAbortedError("вызов завершён")
        name = prompt if prompt is not None else step.prompt
        if not name:
            return
        pcm = self._prompts.get(name)
        if not pcm:
            self._log.debug(
                "вызов %s: промпт %r не загружен — шаг %s без звука",
                self.session_id,
                name,
                step.name,
            )
            return
        completed = await self._player.play(pcm)
        if not completed:
            self._log.debug("вызов %s: промпт шага %s прерван", self.session_id, step.name)

    # ------------------------------------------------------------- биллинг

    async def _resolve_prompt(self, step: Step) -> str | None:
        """Выбрать промпт шага: обычный или по результату запроса в биллинг."""
        if not step.billing:
            return step.prompt
        outcome, _subscriber = await self._lookup_billing(step)
        if outcome == "found":
            return step.prompt
        if outcome == "not_found":
            return step.prompt_not_found or step.prompt
        return step.prompt_error or step.prompt

    async def _lookup_billing(self, step: Step) -> tuple[str, Subscriber | None]:
        """Спросить биллинг: ("found" | "not_found" | "error", абонент)."""
        user_id = self._billing_user_id(step)
        if self._billing is None:
            self._log.error(
                "вызов %s: шаг %s, биллинг не настроен "
                "(нужны BILLING_BASE_URL/BILLING_TOKEN или BILLING_MOCK)",
                self.session_id,
                step.name,
            )
            self._stats.billing = {"outcome": "error", "user_id": user_id, "reason": "not_configured"}
            return "error", None
        if not user_id:
            self._log.warning(
                "вызов %s: шаг %s, не удалось определить userId для биллинга "
                "(в режиме ARI нужен номер звонящего)",
                self.session_id,
                step.name,
            )
            self._stats.billing = {"outcome": "not_found", "user_id": None, "reason": "no_user_id"}
            return "not_found", None
        try:
            subscriber = await self._billing.fetch_subscriber(user_id)
        except BillingError as exc:
            self._log.error("вызов %s: шаг %s, биллинг: %s", self.session_id, step.name, exc)
            self._stats.billing = {"outcome": "error", "user_id": user_id, "reason": str(exc)}
            return "error", None
        if subscriber is None:
            self._log.info(
                "вызов %s: шаг %s, биллинг: абонент %r не найден",
                self.session_id,
                step.name,
                user_id,
            )
            self._stats.billing = {"outcome": "not_found", "user_id": user_id}
            return "not_found", None
        self._log.info(
            "вызов %s: шаг %s, биллинг: %s — %s",
            self.session_id,
            step.name,
            user_id,
            subscriber.summary,
        )
        self._stats.billing = {"outcome": "found", "user_id": user_id, **subscriber.to_dict()}
        return "found", subscriber

    def _billing_user_id(self, step: Step) -> str:
        """`userId` из шага: "caller" — номер звонящего, иначе — буквальное значение."""
        source = (step.billing or "").strip()
        if source.lower() in {"caller", "ani", "caller_number"}:
            number = self._pending.caller_number if self._pending else None
            source = number or ""
        return "".join(ch for ch in source if ch.isalnum() or ch in {"-", "_"})

    async def _wait_digit(self, step: Step) -> str | None:
        return await self._digits.get(max(0.0, step.timeout_ms / 1000.0))

    # ----------------------------------------------- запись и метаданные

    def _open_recorder(self) -> None:
        if not self._settings.record_calls or self.session_id is None:
            return
        recorder = WavRecorder(self._settings.recordings_dir / f"{self.session_id}.wav")
        try:
            recorder.open()
        except OSError as exc:
            self._log.error(
                "вызов %s: не удалось открыть запись %s: %s", self.session_id, recorder.path, exc
            )
            return
        self._recorder = recorder
        self._log.debug("вызов %s: пишем входящий звук в %s", self.session_id, recorder.path)

    def _close_recorder(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        try:
            recorder.close()
        except OSError as exc:
            self._log.warning("вызов %s: ошибка закрытия записи: %s", self.session_id, exc)
            return
        self._recording_path = recorder.path
        self._log.info(
            "вызов %s: запись сохранена %s (%.1f с входящего звука)",
            self.session_id,
            recorder.path,
            recorder.duration_ms / 1000.0,
        )

    def _write_metadata(self) -> None:
        """Положить рядом с записью JSON с ходом диалога."""
        if self.session_id is None or self._recording_path is None:
            return
        duration = time.monotonic() - self._stats.started_at
        payload = {
            "session_id": self.session_id,
            "channel_id": self._channel_id,
            "caller": self._pending.caller_number if self._pending else None,
            "scenario": self._scenario.name,
            "peer": self._peer,
            "started_at": self._stats.started_wall.isoformat(),
            "duration_seconds": round(duration, 3),
            "audio_frames": self._stats.audio_frames,
            "audio_seconds": round(self._stats.audio_bytes / (SAMPLE_RATE * 2), 3),
            "dtmf": "".join(self._stats.dtmf_digits),
            "steps": list(self._stats.steps),
            "hangup_reason": self._stats.hangup_reason,
            "billing": self._stats.billing,
            "recording": self._recording_path.name,
        }
        path = self._recording_path.with_suffix(".json")
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._log.warning("вызов %s: не удалось записать %s: %s", self.session_id, path, exc)

    # ----------------------------------------------------------- завершение

    async def _shutdown(self) -> None:
        self._closed = True
        self._player.stop()
        self._digits.close()

        if self._read_task is not None:
            task, self._read_task = self._read_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - не мешаем завершению вызова
                self._log.exception("вызов %s: ошибка фонового чтения", self.session_id)

        self._close_recorder()
        self._write_metadata()

        if not self._writer.is_closing():
            with contextlib.suppress(Exception):
                self._writer.write(encode_terminate())
                await asyncio.wait_for(self._writer.drain(), timeout=1.0)
        with contextlib.suppress(Exception):
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)

        if self._pending is not None:
            # Завершаем вызов на стороне Asterisk: DELETE каналов и бриджа.
            with contextlib.suppress(Exception):
                await self._pending.close(self._stats.hangup_reason)

        self._log.info(
            "вызов %s: завершён (канал=%s, причина=%s, шаги=%s, DTMF=%s, аудио=%.1f с, запись=%s)",
            self.session_id,
            self._channel_id or "-",
            self._stats.hangup_reason,
            "->".join(self._stats.steps) or "-",
            "".join(self._stats.dtmf_digits) or "-",
            self._stats.audio_bytes / (SAMPLE_RATE * 2),
            str(self._recording_path) if self._recording_path else "нет",
        )
