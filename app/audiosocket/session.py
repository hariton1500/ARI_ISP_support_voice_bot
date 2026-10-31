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
from typing import Any

from app.audio.pcm import FRAME_MS, SAMPLE_RATE, resample
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
from app.dialog.intents import match_voice
from app.dialog.prompts import PromptMap
from app.dialog.scenario import Scenario, Step
from app.llm.conversation import LLMBackend
from app.llm.errors import LLMError
from app.llm.tools import VoiceTools
from app.speech.client import SpeechBackend, SpeechError
from app.speech.streaming import StreamingVoiceInput
from app.speech.vad import VadConfig, VadEventKind

#: Что сказать, если LLM/AITUNNEL недоступны: без технических подробностей.
LLM_ERROR_TEXT = "Извините, произошла техническая ошибка. Перевожу вас на оператора."


class InputBuffer:
    """Очередь ввода (DTMF-цифры или распознанные фразы).

    asyncio.Queue не подходит: отмена get() может «съесть» уже пришедший
    элемент. Здесь deque + Event, а признак закрытия позволяет разбудить
    ожидающего и корректно завершить диалог.
    """

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._ready = asyncio.Event()
        self._closed = False

    def push(self, item: str) -> None:
        if self._closed or not item:
            return
        self._items.append(item)
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    @property
    def pending(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def changed(self) -> asyncio.Event:
        return self._ready

    def pop_nowait(self) -> str | None:
        return self._items.popleft() if self._items else None


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
    #: Финальные распознанные реплики (AITUNNEL STT).
    transcripts: list[str] = field(default_factory=list)
    #: Промежуточные гипотезы STT (для диагностики задержек).
    partial_transcripts: list[str] = field(default_factory=list)
    barge_ins: int = 0
    tts_characters: int = 0
    speech_errors: int = 0
    #: LLM: число реплик, вызовов инструментов, ошибок и озвученных ответов.
    llm_turns: int = 0
    llm_tool_calls: int = 0
    llm_errors: int = 0
    llm_replies: list[str] = field(default_factory=list)
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
        speech: SpeechBackend | None = None,
        llm: LLMBackend | None = None,
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
        self._speech = speech
        self._llm = llm
        self._channel_id: str | None = None

        self.session_id: str | None = None
        self._digits = InputBuffer()
        self._transcripts = InputBuffer()
        self._input_changed = asyncio.Event()
        self._player = AudioPlayer(self._send_audio)
        self._voice: StreamingVoiceInput | None = None
        if speech is not None and settings.voice_input:
            self._voice = StreamingVoiceInput(
                VadConfig(
                    frame_ms=FRAME_MS,
                    start_ms=settings.vad_start_ms,
                    silence_ms=settings.vad_silence_ms,
                    min_speech_ms=settings.vad_min_speech_ms,
                    rms_threshold=settings.vad_rms_threshold,
                    noise_multiplier=settings.vad_noise_multiplier,
                ),
                partial_interval_ms=settings.stt_partial_interval_ms,
            )
        self._stt_tasks: set[asyncio.Task[None]] = set()
        self._partial_task: asyncio.Task[None] | None = None
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
                "вызов %s: диалог начат (канал=%s, peer=%s, голос=%s)",
                self.session_id,
                self._channel_id or "-",
                self._peer or "?",
                "вкл" if self._voice is not None else "выкл",
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
        self._transcripts.close()
        self._input_changed.set()
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
            self._transcripts.close()
            self._input_changed.set()

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
        self._feed_voice(pcm)

    # ------------------------------------------------------- речь: VAD/STT

    def _feed_voice(self, pcm: bytes) -> None:
        """Прогнать звук через VAD, собрать реплику и запустить STT."""
        voice = self._voice
        if voice is None:
            return
        event = voice.feed(pcm)
        if event is not None and event.kind is VadEventKind.START:
            self._log.info("вызов %s: VAD: начало речи", self.session_id)
            if self._player.is_playing and self._settings.voice_barge_in:
                # Голос абонента перебивает промпт/TTS — это и есть barge-in.
                self._player.stop()
                self._stats.barge_ins += 1
                self._log.info(
                    "вызов %s: barge-in по голосу — воспроизведение остановлено",
                    self.session_id,
                )
        elif event is not None and event.kind is VadEventKind.END:
            self._log.info(
                "вызов %s: VAD: конец речи (%.0f мс)", self.session_id, event.duration_ms
            )
            if event.duration_ms < self._settings.vad_min_speech_ms:
                self._log.debug(
                    "вызов %s: реплика короче %d мс — пропущена",
                    self.session_id,
                    self._settings.vad_min_speech_ms,
                )
                return
            task, self._partial_task = self._partial_task, None
            if task is not None and not task.done():
                task.cancel()
            self._spawn_stt(self._transcribe(event.pcm, final=True))
        elif voice.is_speaking and voice.partial_due():
            self._request_partial()

    def _request_partial(self) -> None:
        """Отправить накопленную речь в STT, не дожидаясь конца реплики."""
        voice = self._voice
        if voice is None or self._speech is None:
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        pcm = voice.partial_audio()
        if len(pcm) < SAMPLE_RATE * 2 * self._settings.vad_min_speech_ms // 1000:
            return
        voice.mark_partial()
        self._partial_task = self._spawn_stt(self._transcribe(pcm, final=False))

    def _spawn_stt(self, coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=f"stt-{self.session_id}")
        self._stt_tasks.add(task)
        task.add_done_callback(self._stt_tasks.discard)
        return task

    async def _transcribe(self, pcm: bytes, *, final: bool) -> None:
        speech = self._speech
        if speech is None or self._closed:
            return
        try:
            text = await speech.transcribe(pcm, language=self._settings.speech_language)
        except asyncio.CancelledError:
            raise
        except SpeechError as exc:
            self._stats.speech_errors += 1
            if final:
                self._log.warning("вызов %s: STT: %s", self.session_id, exc)
            return
        except Exception:  # noqa: BLE001 - STT не должен ронять вызов
            self._stats.speech_errors += 1
            self._log.exception("вызов %s: неожиданная ошибка STT", self.session_id)
            return
        text = " ".join(text.split())
        if not text:
            return
        if final:
            self._stats.transcripts.append(text)
            self._log.info("вызов %s: STT: %r", self.session_id, text)
            self._transcripts.push(text)
            self._notify_input()
        else:
            self._stats.partial_transcripts.append(text)
            # В sidecar храним только последние гипотезы, чтобы JSON не пух.
            del self._stats.partial_transcripts[:-20]
            self._log.debug("вызов %s: STT (частично): %r", self.session_id, text)

    def _notify_input(self) -> None:
        self._input_changed.set()

    def push_transcript(self, text: str) -> None:
        """Принять готовую расшифровку (используется тестами и внешним STT)."""
        if self._closed or not text:
            return
        self._transcripts.push(text)
        self._notify_input()

    def push_digit(self, digit: str) -> None:
        """Принять DTMF-цифру: из фрейма AudioSocket или события ARI."""
        if self._closed or not digit:
            return
        self._stats.dtmf_digits.append(digit)
        self._log.info("вызов %s: DTMF %r", self.session_id, digit)
        if self._player.is_playing:
            self._player.stop()
        self._digits.push(digit)
        self._notify_input()

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
        if step.llm:
            return await self._run_llm_step(step, prompt)
        attempts = 0
        while True:
            if self._closed:
                raise ConnectionAbortedError("вызов завершён")
            await self._play_prompt(step, prompt)
            if step.hangup:
                return None
            if not step.dtmf and not step.voice:
                return step.next

            if self._closed:
                raise ConnectionAbortedError("вызов завершён")
            attempts += 1
            digit, text = await self._wait_input(step)

            target: str | None = None
            if digit is not None:
                target = step.dtmf.get(digit)
            elif text is not None:
                target = match_voice(text, step.dtmf, step.voice)

            if target is not None:
                if digit is not None:
                    self._log.info(
                        "вызов %s: шаг %s, цифра %r -> %s",
                        self.session_id,
                        step.name,
                        digit,
                        target,
                    )
                else:
                    self._log.info(
                        "вызов %s: шаг %s, фраза %r -> %s",
                        self.session_id,
                        step.name,
                        text,
                        target,
                    )
                return target

            has_input = digit is not None or text is not None
            if has_input:
                description = f"неверная цифра {digit!r}" if digit else f"фраза не распознана {text!r}"
            else:
                description = "нет ввода"

            # Исчерпали попытки — уходим по fallback-переходу.
            if attempts >= step.max_attempts:
                target = step.on_invalid if has_input else step.on_timeout
                if target is None:
                    target = step.on_timeout
                self._log.info(
                    "вызов %s: шаг %s, попытки исчерпаны (%s) -> %s",
                    self.session_id,
                    step.name,
                    description,
                    target or "завершение вызова",
                )
                return target

            self._log.info(
                "вызов %s: шаг %s, %s (попытка %d из %d)",
                self.session_id,
                step.name,
                description,
                attempts,
                step.max_attempts,
            )

    async def _run_llm_step(self, step: Step, prompt: str | None) -> str | None:
        """Промпт шага, затем свободный диалог с LLM и инструментами.

        DTMF и `voice`-команды сценария остаются быстрыми переходами. Всё
        остальное уходит модели; её ответ озвучивается через TTS, а barge-in
        работает так же, как для обычного промпта.
        """
        await self._play_prompt(step, prompt)
        if self._closed:
            raise ConnectionAbortedError("вызов завершён")
        if step.hangup:
            return None
        if self._llm is None:
            self._log.warning(
                "вызов %s: шаг %s, LLM не настроена (нужен AITUNNEL_API_KEY) — "
                "работают только DTMF/voice",
                self.session_id,
                step.name,
            )
            return step.on_invalid or step.on_timeout

        tools = VoiceTools(self._billing, logger=self._log)
        conversation = self._llm.conversation(tools)

        while not self._closed:
            digit, text = await self._wait_input(step)
            if digit is not None:
                target = step.dtmf.get(digit)
                if target is not None:
                    self._log.info(
                        "вызов %s: шаг %s (LLM), цифра %r -> %s",
                        self.session_id,
                        step.name,
                        digit,
                        target,
                    )
                    return target
                self._log.info(
                    "вызов %s: шаг %s (LLM), цифра %r без перехода",
                    self.session_id,
                    step.name,
                    digit,
                )
                return step.on_invalid or step.on_timeout

            if text is None:
                self._log.info(
                    "вызов %s: шаг %s (LLM), нет ввода за %d мс",
                    self.session_id,
                    step.name,
                    step.timeout_ms,
                )
                return step.on_timeout

            target = match_voice(text, step.dtmf, step.voice)
            if target is not None:
                self._log.info(
                    "вызов %s: шаг %s (LLM), фраза %r -> %s",
                    self.session_id,
                    step.name,
                    text,
                    target,
                )
                return target

            self._log.info("вызов %s: LLM запрос: %r", self.session_id, text)
            before = getattr(conversation, "tool_calls", 0)
            try:
                reply = await conversation.ask(text)
            except asyncio.CancelledError:
                raise
            except LLMError as exc:
                self._stats.llm_errors += 1
                self._log.error("вызов %s: LLM: %s", self.session_id, exc)
                return await self._llm_failure(step)
            except Exception:  # noqa: BLE001 - LLM не должна ронять вызов
                self._stats.llm_errors += 1
                self._log.exception("вызов %s: неожиданная ошибка LLM", self.session_id)
                return await self._llm_failure(step)

            self._stats.llm_turns += 1
            self._stats.llm_tool_calls += max(
                0, getattr(conversation, "tool_calls", 0) - before
            )
            self._stats.llm_replies.append(reply.text)
            del self._stats.llm_replies[:-20]
            self._log.info(
                "вызов %s: LLM ответ: %r (инструментов: %d)",
                self.session_id,
                reply.text,
                getattr(conversation, "tool_calls", 0),
            )
            await self._speak_text(reply.text, f"{step.name}:llm")

            if reply.end_call:
                self._stats.hangup_reason = "llm_end_call"
                self._log.info("вызов %s: LLM завершила разговор", self.session_id)
                return None
            if reply.transfer:
                self._stats.hangup_reason = "llm_transfer"
                self._log.info("вызов %s: LLM переводит на оператора", self.session_id)
                return self._operator_step()

        raise ConnectionAbortedError("вызов завершён")

    async def _llm_failure(self, step: Step) -> str | None:
        """Извиниться и уйти на оператора (или fallback шага)."""
        target = self._operator_step()
        if target is not None:
            await self._speak_text(LLM_ERROR_TEXT, f"{step.name}:llm-error")
            return target
        await self._speak_text(
            "Извините, произошла техническая ошибка. Попробуйте ещё раз.",
            f"{step.name}:llm-error",
        )
        return step.on_invalid or step.on_timeout

    def _operator_step(self) -> str | None:
        """Шаг перевода на оператора, если он есть в сценарии."""
        return "operator" if "operator" in self._scenario.steps else None

    async def _play_prompt(self, step: Step, prompt: str | None = None) -> None:
        if self._closed:
            raise ConnectionAbortedError("вызов завершён")
        name = prompt if prompt is not None else step.prompt
        pcm = self._prompts.get(name) if name else None
        if pcm:
            completed = await self._player.play(pcm)
            if not completed:
                self._log.debug(
                    "вызов %s: промпт шага %s прерван", self.session_id, step.name
                )
            return
        if step.text:
            await self._speak_text(step.text, step.name)
            return
        if name:
            self._log.debug(
                "вызов %s: промпт %r не загружен — шаг %s без звука",
                self.session_id,
                name,
                step.name,
            )

    async def _speak_text(self, text: str, step_name: str) -> None:
        """Озвучить текст через AITUNNEL TTS потоком (без ожидания всего файла)."""
        speech = self._speech
        if speech is None:
            self._log.warning(
                "вызов %s: шаг %s, TTS не настроен (нужен AITUNNEL_API_KEY) — текст: %s",
                self.session_id,
                step_name,
                text,
            )
            return
        self._stats.tts_characters += len(text)
        try:
            completed = await self._player.play_stream(speech.stream_tts(text))
        except asyncio.CancelledError:
            raise
        except SpeechError as exc:
            self._stats.speech_errors += 1
            self._log.error("вызов %s: шаг %s, TTS: %s", self.session_id, step_name, exc)
            return
        except Exception:  # noqa: BLE001 - TTS не должен ронять вызов
            self._stats.speech_errors += 1
            self._log.exception(
                "вызов %s: шаг %s, неожиданная ошибка TTS", self.session_id, step_name
            )
            return
        if not completed:
            self._log.debug(
                "вызов %s: TTS шага %s прерван (barge-in)", self.session_id, step_name
            )

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

    async def _wait_input(self, step: Step) -> tuple[str | None, str | None]:
        """Дождаться DTMF или распознанной фразы.

        Возвращает (цифра, текст); (None, None) — таймаут.
        Событие `_input_changed` общее, поэтому отмена ожидания не «съедает»
        уже пришедший ввод.
        """
        loop = asyncio.get_running_loop()
        timeout = max(0.0, step.timeout_ms / 1000.0)
        deadline = loop.time() + timeout
        while True:
            digit = self._digits.pop_nowait()
            if digit is not None:
                return digit, None
            text = self._transcripts.pop_nowait()
            if text is not None:
                return None, text
            if self._closed or self._digits.closed or self._transcripts.closed:
                raise ConnectionAbortedError("вызов завершён")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None, None
            self._input_changed.clear()
            try:
                await asyncio.wait_for(self._input_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # Проверим буферы ещё раз: ввод мог прийти до таймаута.
                digit = self._digits.pop_nowait()
                if digit is not None:
                    return digit, None
                text = self._transcripts.pop_nowait()
                if text is not None:
                    return None, text
                return None, None

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
            "transcripts": list(self._stats.transcripts),
            "partial_transcripts": list(self._stats.partial_transcripts),
            "barge_ins": self._stats.barge_ins,
            "tts_characters": self._stats.tts_characters,
            "speech_errors": self._stats.speech_errors,
            "llm_turns": self._stats.llm_turns,
            "llm_tool_calls": self._stats.llm_tool_calls,
            "llm_errors": self._stats.llm_errors,
            "llm_replies": list(self._stats.llm_replies),
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
        self._transcripts.close()
        self._input_changed.set()
        if self._voice is not None:
            self._voice.reset()

        if self._read_task is not None:
            task, self._read_task = self._read_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - не мешаем завершению вызова
                self._log.exception("вызов %s: ошибка фонового чтения", self.session_id)

        tasks = list(self._stt_tasks)
        self._partial_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stt_tasks.clear()

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
            "вызов %s: завершён (канал=%s, причина=%s, шаги=%s, DTMF=%s, "
            "речь=%s, barge-in=%d, аудио=%.1f с, запись=%s)",
            self.session_id,
            self._channel_id or "-",
            self._stats.hangup_reason,
            "->".join(self._stats.steps) or "-",
            "".join(self._stats.dtmf_digits) or "-",
            " | ".join(self._stats.transcripts) or "-",
            self._stats.barge_ins,
            self._stats.audio_bytes / (SAMPLE_RATE * 2),
            str(self._recording_path) if self._recording_path else "нет",
        )
