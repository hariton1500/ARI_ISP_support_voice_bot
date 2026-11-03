"""Один вызов AudioSocket, обслуживаемый Yandex Realtime.

PCM 8 кГц от Asterisk непрерывно ресемплируется в 16 кГц и отправляется в
Realtime. Аудиодельты ответа идут в обратном направлении через состоятельный
SoXR и paced AudioPlayer. Локальные VAD, STT, LLM и TTS в этом режиме не
создаются: распознавание, диалог и синтез выполняет одна Realtime-сессия.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
import uuid as uuid_module
from collections.abc import AsyncIterator, Mapping
from typing import Any

from app.audio.pcm import SAMPLE_RATE, PcmStreamResampler, silence_ms
from app.audiosocket.registry import PendingCall
from app.audiosocket.session import CallSession
from app.billing.client import BillingLookup
from app.config import Settings
from app.dialog.prompts import PromptMap
from app.dialog.scenario import Scenario
from app.llm.prompts import LLMPromptSet
from app.llm.tools import CallControl, VoiceTools
from app.realtime.client import REALTIME_SAMPLE_RATE, RealtimeBackend, RealtimeError, YandexRealtimeClient

#: После последнего отправленного фрейма эхо ещё некоторое время может
#: возвращаться от телефона/АТС. В это время вход в Realtime не передаём.
REALTIME_ECHO_TAIL_SECONDS = 0.5

#: Нормализованные поля OpenAI-совместимого ``response.usage``. Кешированные
#: токены входят в input_tokens, поэтому отдельно сохраняем их как подмножество.
_REALTIME_USAGE_PATHS = {
    "total_tokens": ("total_tokens",),
    "input_tokens": ("input_tokens",),
    "input_text_tokens": ("input_token_details", "text_tokens"),
    "input_audio_tokens": ("input_token_details", "audio_tokens"),
    "cached_input_tokens": ("input_token_details", "cached_tokens"),
    "cached_input_text_tokens": (
        "input_token_details",
        "cached_tokens_details",
        "text_tokens",
    ),
    "cached_input_audio_tokens": (
        "input_token_details",
        "cached_tokens_details",
        "audio_tokens",
    ),
    "output_tokens": ("output_tokens",),
    "output_text_tokens": ("output_token_details", "text_tokens"),
    "output_audio_tokens": ("output_token_details", "audio_tokens"),
}
_REALTIME_TOOL_TOKEN_PATHS = (
    ("input_token_details", "tool_tokens"),
    ("input_token_details", "tools_tokens"),
    ("tool_tokens",),
)


class RealtimeCallSession(CallSession):
    """Потоковый диалог с серверным VAD и общими функциями ``VoiceTools``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        settings: Settings,
        scenario: Scenario,
        prompts: PromptMap,
        llm_prompts: LLMPromptSet,
        *,
        channel_uuid: uuid_module.UUID,
        pending: PendingCall | None = None,
        billing: BillingLookup | None = None,
        realtime: RealtimeBackend | None = None,
        logger: logging.Logger | None = None,
        peer: str | None = None,
    ) -> None:
        super().__init__(
            reader,
            writer,
            settings,
            scenario,
            prompts,
            channel_uuid=channel_uuid,
            pending=pending,
            billing=billing,
            speech=None,
            llm=None,
            logger=logger,
            peer=peer,
        )
        self._voice_tools = VoiceTools(billing, logger=self._log)
        self._control = CallControl()
        self._realtime = realtime or YandexRealtimeClient(
            settings,
            llm_prompts.system,
            self._voice_tools.schemas(),
            logger=self._log,
        )
        self._realtime_input: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._realtime_output: asyncio.Queue[bytes | None] | None = None
        self._input_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._playback_task: asyncio.Task[None] | None = None
        self._finalizer_tasks: set[asyncio.Task[None]] = set()
        self._dialog_done = asyncio.Event()
        self._active_response_id: str | None = None
        self._response_resampler: PcmStreamResampler | None = None
        self._response_has_audio = False
        self._function_calls: list[dict[str, Any]] = []
        self._speech_stopped_at: float | None = None
        self._billing_answer_pending = False
        self._echo_guard_until = 0.0
        self._realtime_input_bytes = 0
        self._realtime_output_bytes = 0
        self._realtime_latencies_ms: list[float] = []
        self._realtime_usage_responses = 0
        self._realtime_usage = {
            "tool_tokens": 0,
            **{name: 0 for name in _REALTIME_USAGE_PATHS},
        }
        self._system_prompt_characters = len(llm_prompts.system)

    def abort(self) -> None:
        super().abort()
        self._dialog_done.set()
        self._stop_output()

    async def _read_frames(self) -> None:
        try:
            await super()._read_frames()
        finally:
            self._dialog_done.set()
            self._stop_output()

    def _feed_voice(self, pcm: bytes) -> None:
        """Передать звук серверному VAD, не возвращая ему собственное эхо."""
        if self._closed:
            return
        if self._settings.voice_echo_guard and (
            self._bot_speaking or time.monotonic() < self._echo_guard_until
        ):
            return
        self._realtime_input.put_nowait(pcm)

    async def _run_dialog(self) -> None:
        self._stats.steps.append("realtime")
        try:
            await self._realtime.start()
            await self._realtime.configure()
            self._input_task = asyncio.create_task(
                self._send_input(), name=f"realtime-input-{self.session_id}"
            )
            self._event_task = asyncio.create_task(
                self._receive_events(), name=f"realtime-events-{self.session_id}"
            )
            await self._realtime.request_greeting()
            await self._dialog_done.wait()
        except asyncio.CancelledError:
            raise
        except RealtimeError as exc:
            self._stats.llm_errors += 1
            self._stats.hangup_reason = "realtime_error"
            self._log.error("вызов %s: Yandex Realtime: %s", self.session_id, exc)
        finally:
            self._dialog_done.set()

    async def _send_input(self) -> None:
        resampler = PcmStreamResampler(SAMPLE_RATE, REALTIME_SAMPLE_RATE)
        try:
            while True:
                pcm = await self._realtime_input.get()
                if pcm is None:
                    return
                for block in resampler.feed(pcm):
                    await self._realtime.append_audio(block)
                    self._realtime_input_bytes += len(block)
        except asyncio.CancelledError:
            raise
        except RealtimeError as exc:
            self._stats.llm_errors += 1
            if not self._closed:
                self._stats.hangup_reason = "realtime_input_error"
                self._log.warning("вызов %s: отправка звука в Realtime: %s", self.session_id, exc)
            self._dialog_done.set()

    async def _receive_events(self) -> None:
        try:
            async for event in self._realtime.events():
                await self._handle_event(event)
                if self._dialog_done.is_set():
                    return
        except asyncio.CancelledError:
            raise
        except RealtimeError as exc:
            self._stats.llm_errors += 1
            if not self._closed:
                self._stats.hangup_reason = "realtime_error"
                self._log.warning("вызов %s: приём событий Realtime: %s", self.session_id, exc)
        except Exception:  # noqa: BLE001 - один вызов не должен ронять сервер
            self._stats.llm_errors += 1
            self._stats.hangup_reason = "realtime_error"
            self._log.exception("вызов %s: ошибка обработки события Realtime", self.session_id)
        finally:
            self._dialog_done.set()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "input_audio_buffer.speech_started":
            if self._settings.voice_barge_in and not self._settings.voice_echo_guard:
                self._interrupt_response()
        elif kind == "input_audio_buffer.speech_stopped":
            self._speech_stopped_at = time.monotonic()
            self._log.debug("вызов %s: Realtime VAD: конец речи", self.session_id)
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = " ".join(str(event.get("transcript") or "").split())
            if text:
                if self._settings.voice_echo_guard and self._is_echo_text(text):
                    self._stats.ignored_echo += 1
                    self._log.info("вызов %s: Realtime STT-эхо отброшено: %r", self.session_id, text)
                    return
                self._stats.transcripts.append(text)
                self._stats.llm_turns += 1
                self._log.info("вызов %s: Realtime STT: %r", self.session_id, text)
        elif kind == "response.created":
            response = event.get("response") or {}
            response_id = str(response.get("id") or event.get("response_id") or "response")
            self._begin_response(response_id)
        elif kind == "response.output_audio.delta":
            self._receive_audio(event)
        elif kind == "response.output_audio.done":
            self._flush_response_audio(str(event.get("response_id") or ""))
        elif kind == "response.output_audio_transcript.done":
            text = " ".join(str(event.get("transcript") or "").split())
            if text:
                self._stats.llm_replies.append(text)
                del self._stats.llm_replies[:-20]
                self._stats.tts_characters += len(text)
                self._remember_bot_text(text)
                self._billing_answer_pending = False
                self._log.info("вызов %s: Realtime ответ: %r", self.session_id, text)
        elif kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._function_calls.append(item)
        elif kind == "response.done":
            response = event.get("response") or {}
            response_id = str(response.get("id") or event.get("response_id") or "")
            if isinstance(response, Mapping):
                self._record_response_usage(response_id, response.get("usage"))
            self._complete_response(response_id)
        elif kind == "error":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error or event)
            raise RealtimeError(str(message))

    def _begin_response(self, response_id: str) -> None:
        if self._closed:
            return
        previous = self._playback_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._active_response_id = response_id
        self._response_has_audio = False
        self._response_resampler = PcmStreamResampler(REALTIME_SAMPLE_RATE, SAMPLE_RATE)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._realtime_output = queue
        self._playback_task = asyncio.create_task(
            self._play_response(queue), name=f"realtime-playback-{self.session_id}"
        )

    def _response_matches(self, response_id: str) -> bool:
        return (
            self._active_response_id is not None
            and (not response_id or response_id == self._active_response_id)
        )

    def _receive_audio(self, event: dict[str, Any]) -> None:
        response_id = str(event.get("response_id") or "")
        if not self._response_matches(response_id) or self._response_resampler is None:
            return
        try:
            pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RealtimeError("Realtime вернул некорректный base64 в аудиодельте") from exc
        if not pcm:
            return
        if self._speech_stopped_at is not None:
            latency = (time.monotonic() - self._speech_stopped_at) * 1000.0
            self._realtime_latencies_ms.append(round(latency, 1))
            del self._realtime_latencies_ms[:-100]
            self._speech_stopped_at = None
            self._log.info("вызов %s: Realtime первая аудиодельта через %.0f мс", self.session_id, latency)
        self._realtime_output_bytes += len(pcm)
        self._response_has_audio = True
        assert self._realtime_output is not None
        for block in self._response_resampler.feed(pcm):
            self._realtime_output.put_nowait(block)

    def _flush_response_audio(self, response_id: str) -> None:
        if not self._response_matches(response_id) or self._response_resampler is None:
            return
        tail = self._response_resampler.flush()
        self._response_resampler = None
        if tail and self._realtime_output is not None:
            self._realtime_output.put_nowait(tail)

    async def _play_response(self, queue: asyncio.Queue[bytes | None]) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            lead_in = silence_ms(self._settings.playback_lead_in_ms)
            if lead_in:
                yield lead_in
            while True:
                pcm = await queue.get()
                if pcm is None:
                    return
                yield pcm

        self._bot_speaking = True
        try:
            completed = await self._player.play_stream(chunks())
            if not completed:
                self._log.debug("вызов %s: ответ Realtime прерван", self.session_id)
        finally:
            self._bot_speaking = False
            self._echo_guard_until = time.monotonic() + REALTIME_ECHO_TAIL_SECONDS

    def _interrupt_response(self) -> None:
        had_audio = self._player.is_playing or self._response_has_audio
        self._stop_output()
        if had_audio:
            self._stats.barge_ins += 1
            self._log.info("вызов %s: Realtime barge-in — воспроизведение остановлено", self.session_id)

    def _stop_output(self) -> None:
        self._active_response_id = None
        self._response_resampler = None
        self._response_has_audio = False
        queue = self._realtime_output
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(None)
        self._player.stop()

    def _complete_response(self, response_id: str) -> None:
        self._flush_response_audio(response_id)
        queue = self._realtime_output
        if queue is not None:
            queue.put_nowait(None)
        playback = self._playback_task
        calls = self._function_calls
        self._function_calls = []
        self._active_response_id = None
        self._response_resampler = None
        task = asyncio.create_task(
            self._finalize_response(playback, calls),
            name=f"realtime-response-done-{self.session_id}",
        )
        self._finalizer_tasks.add(task)
        task.add_done_callback(self._finalizer_tasks.discard)

    async def _finalize_response(
        self,
        playback: asyncio.Task[None] | None,
        calls: list[dict[str, Any]],
    ) -> None:
        """Доиграть текущий ответ, выполнить функции и только затем управлять вызовом."""
        if playback is not None:
            try:
                await playback
            except asyncio.CancelledError:
                return
            except (ConnectionError, OSError) as exc:
                self._stats.speech_errors += 1
                self._log.warning("вызов %s: воспроизведение Realtime: %s", self.session_id, exc)

        for item in calls:
            name = str(item.get("name") or "")
            call_id = str(item.get("call_id") or "")
            if not name or not call_id:
                self._log.warning("вызов %s: неполный function_call: %s", self.session_id, item)
                continue
            try:
                if name == "transfer_to_operator" and self._billing_answer_pending:
                    output = json.dumps(
                        {
                            "status": "rejected",
                            "error": "billing_answer_required",
                            "message": (
                                "Сначала озвучь абоненту данные из get_billing_info. "
                                "Не вызывай перевод без явной просьбы абонента."
                            ),
                        },
                        ensure_ascii=False,
                    )
                    self._log.info(
                        "вызов %s: перевод отклонён — результат биллинга ещё не озвучен",
                        self.session_id,
                    )
                else:
                    output = await self._voice_tools.execute(
                        name, item.get("arguments"), self._control
                    )
            except Exception:  # noqa: BLE001 - вернуть модели ошибку вместо обрыва
                self._stats.llm_errors += 1
                self._log.exception("вызов %s: функция %s завершилась ошибкой", self.session_id, name)
                output = json.dumps({"error": "tool_execution_failed"}, ensure_ascii=False)
            self._stats.llm_tool_calls += 1
            if name == "get_billing_info":
                self._record_billing_result(output)
            if not self._closed:
                await self._realtime.send_function_output(call_id, output)

        if self._closed:
            return
        if self._control.end_call:
            self._stats.hangup_reason = "realtime_end_call"
            self._log.info("вызов %s: Realtime завершила разговор", self.session_id)
            self._dialog_done.set()
        elif self._control.transfer:
            self._stats.hangup_reason = "realtime_transfer"
            self._log.info("вызов %s: Realtime запросила оператора", self.session_id)
            self._dialog_done.set()
        elif calls:
            await self._realtime.request_response()

    def _record_billing_result(self, output: str) -> None:
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return
        if not isinstance(result, dict):
            return
        found = result.get("found") is True
        self._billing_answer_pending = found
        user = result.get("user") if isinstance(result.get("user"), dict) else {}
        self._stats.billing = {
            "outcome": "found" if found else ("not_found" if result.get("found") is False else "error"),
            "user_id": user.get("id"),
            **user,
        }

    @staticmethod
    def _usage_value(usage: Mapping[str, Any], path: tuple[str, ...]) -> int | None:
        """Прочитать неотрицательное целое по вложенному пути usage."""
        value: Any = usage
        for key in path:
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _record_response_usage(self, response_id: str, usage: Any) -> None:
        """Суммировать статистику ответа; отсутствие usage не портит звонок."""
        if not isinstance(usage, Mapping):
            return
        self._realtime_usage_responses += 1
        for name, path in _REALTIME_USAGE_PATHS.items():
            self._realtime_usage[name] += self._usage_value(usage, path) or 0
        tool_tokens = 0
        for path in _REALTIME_TOOL_TOKEN_PATHS:
            value = self._usage_value(usage, path)
            if value is not None:
                tool_tokens = value
                break
        self._realtime_usage["tool_tokens"] += tool_tokens
        self._log.info(
            "вызов %s: Realtime usage %s: вход=%d (кеш=%d, инструменты=%d), выход=%d, всего=%d",
            self.session_id,
            response_id or "-",
            self._usage_value(usage, ("input_tokens",)) or 0,
            self._usage_value(usage, ("input_token_details", "cached_tokens")) or 0,
            tool_tokens,
            self._usage_value(usage, ("output_tokens",)) or 0,
            self._usage_value(usage, ("total_tokens",)) or 0,
        )

    def _metadata_extra(self) -> dict[str, Any]:
        result = super()._metadata_extra()
        usage = {
            "responses": self._realtime_usage_responses,
            **self._realtime_usage,
        }
        usage["uncached_input_tokens"] = max(
            usage["input_tokens"] - usage["cached_input_tokens"], 0
        )
        result.update(
            {
                "realtime_model": self._settings.yandex_realtime_model,
                "realtime_system_prompt_characters": self._system_prompt_characters,
                "realtime_input_audio_seconds": round(
                    self._realtime_input_bytes / (REALTIME_SAMPLE_RATE * 2), 3
                ),
                "realtime_output_audio_seconds": round(
                    self._realtime_output_bytes / (REALTIME_SAMPLE_RATE * 2), 3
                ),
                "realtime_usage": usage,
                "realtime_first_audio_latency_ms": list(self._realtime_latencies_ms),
            }
        )
        return result

    async def _shutdown(self) -> None:
        self._closed = True
        self._dialog_done.set()
        self._stop_output()
        self._realtime_input.put_nowait(None)

        tasks = [self._input_task, self._event_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        self._input_task = None
        self._event_task = None

        finalizers = list(self._finalizer_tasks)
        for task in finalizers:
            if not task.done():
                task.cancel()
        if finalizers:
            await asyncio.gather(*finalizers, return_exceptions=True)
        self._finalizer_tasks.clear()

        playback = self._playback_task
        if playback is not None and not playback.done():
            playback.cancel()
        if playback is not None:
            await asyncio.gather(playback, return_exceptions=True)
        self._playback_task = None

        with contextlib.suppress(Exception):
            await self._realtime.close()
        await super()._shutdown()
