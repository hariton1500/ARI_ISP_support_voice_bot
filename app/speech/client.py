"""Клиент AITUNNEL для распознавания (STT) и синтеза речи (TTS).

AITUNNEL — OpenAI-совместимый шлюз, base URL `https://api.aitunnel.ru/v1`:

* `POST /audio/transcriptions` — multipart c файлом WAV и `language=ru`;
* `POST /audio/speech` — JSON, ответ — сырой аудиопоток (`response_format=pcm`).

SSE/WebSocket для STT у провайдера нет, поэтому потоковость делает
`app.speech.streaming` (VAD + промежуточные запросы), а TTS читается из
HTTP-ответа по мере генерации и сразу уходит в AudioSocket.
"""

from __future__ import annotations

import io
import json
import logging
import wave
from collections.abc import AsyncIterator
from typing import Protocol

import aiohttp

from app.audio.pcm import SAMPLE_RATE, PcmStreamResampler, to_wav
from app.config import Settings

__all__ = ["SpeechBackend", "SpeechClient", "SpeechError"]


class SpeechError(RuntimeError):
    """AITUNNEL недоступен или вернул некорректный ответ."""


class SpeechBackend(Protocol):
    """Интерфейс, который нужен диалогу (подменяется в тестах)."""

    async def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int = SAMPLE_RATE,
        language: str | None = None,
    ) -> str: ...

    def stream_tts(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[bytes]: ...


class SpeechClient:
    """Тонкая обёртка над AITUNNEL с ресемплингом TTS в 8 кГц."""

    def __init__(
        self,
        settings: Settings,
        *,
        logger: logging.Logger | None = None,
        timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.ai_tunnel_base_url.rstrip("/")
        self._key = (settings.ai_tunnel_api_key or "").strip()
        self._timeout = timeout if timeout is not None else settings.speech_timeout
        self._log = logger or logging.getLogger("app.speech")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        if not self.enabled:
            raise SpeechError("AITUNNEL не настроен: задайте AITUNNEL_API_KEY")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json, audio/*",
        }
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=self._timeout, connect=min(self._timeout, 5.0)
            ),
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def __aenter__(self) -> SpeechClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int = SAMPLE_RATE,
        language: str | None = None,
    ) -> str:
        """Распознать PCM16 моно; вернуть текст (пустая строка — тишина)."""
        if not pcm:
            return ""
        await self.start()
        assert self._session is not None
        form = aiohttp.FormData()
        form.add_field(
            "file",
            to_wav(pcm, sample_rate=sample_rate),
            filename="utterance.wav",
            content_type="audio/wav",
        )
        form.add_field("model", self._settings.stt_model)
        form.add_field("response_format", "json")
        lang = language if language is not None else self._settings.speech_language
        if lang:
            form.add_field("language", lang)

        try:
            async with self._session.post(
                self._url("/audio/transcriptions"), data=form
            ) as response:
                body = await response.read()
                if response.status != 200:
                    raise SpeechError(
                        f"STT: HTTP {response.status}: {self._error_text(body)}"
                    )
        except aiohttp.ClientError as exc:
            raise SpeechError(f"STT: сеть недоступна: {exc}") from exc
        except TimeoutError as exc:  # pragma: no cover - зависит от сети
            raise SpeechError("STT: таймаут запроса") from exc

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechError(f"STT: ожидался JSON, получено {body[:120]!r}") from exc
        if not isinstance(payload, dict):
            raise SpeechError("STT: ответ должен быть JSON-объектом")
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise SpeechError("STT: поле text должно быть строкой")
        return text.strip()

    async def stream_tts(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Синтезировать текст и отдавать PCM 8 кГц по мере готовности."""
        text = text.strip()
        if not text:
            return
        audio_format = self._settings.tts_format.lower()
        if audio_format not in {"pcm", "wav"}:
            raise SpeechError(
                f"TTS: TTS_FORMAT={self._settings.tts_format!r} не поддержан "
                "(ожидался pcm или wav)"
            )
        await self.start()
        assert self._session is not None
        payload = {
            "model": model or self._settings.tts_model,
            "input": text,
            "voice": voice or self._settings.tts_voice,
            "response_format": audio_format,
        }
        try:
            async with self._session.post(
                self._url("/audio/speech"), json=payload
            ) as response:
                if response.status != 200:
                    body = await response.read()
                    raise SpeechError(
                        f"TTS: HTTP {response.status}: {self._error_text(body)}"
                    )
                if audio_format == "wav":
                    body = await response.read()
                    for chunk in self._wav_chunks(body):
                        yield chunk
                    return

                resampler = PcmStreamResampler(self._settings.tts_pcm_rate, SAMPLE_RATE)
                async for chunk in response.content.iter_chunked(4096):
                    for block in resampler.feed(chunk):
                        yield block
                tail = resampler.flush()
                if tail:
                    yield tail
        except aiohttp.ClientError as exc:
            raise SpeechError(f"TTS: сеть недоступна: {exc}") from exc
        except TimeoutError as exc:  # pragma: no cover - зависит от сети
            raise SpeechError("TTS: таймаут запроса") from exc

    def _wav_chunks(self, body: bytes) -> list[bytes]:
        """WAV-ответ (не потоковый): разобрать заголовок и ресемплировать."""
        try:
            with wave.open(io.BytesIO(body), "rb") as wav:
                rate = wav.getframerate()
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            raise SpeechError(f"TTS: некорректный WAV в ответе: {exc}") from exc
        if width != 2 or channels != 1:
            raise SpeechError("TTS: поддержан только WAV PCM16 моно")
        if rate == SAMPLE_RATE:
            return [frames]
        resampler = PcmStreamResampler(rate, SAMPLE_RATE)
        chunks = resampler.feed(frames)
        tail = resampler.flush()
        if tail:
            chunks.append(tail)
        return chunks

    @staticmethod
    def _error_text(body: bytes) -> str:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - диагностика не должна падать
            return repr(body[:160])
        return " ".join(text.split())[:300] or "пустой ответ"

