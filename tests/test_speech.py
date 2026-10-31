"""Тесты клиента AITUNNEL: STT multipart, TTS-поток, ошибки."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import numpy as np
import pytest
from aiohttp import web

from app.audio.pcm import SAMPLE_RATE, to_wav
from app.config import Settings
from app.speech.client import SpeechClient, SpeechError

TOKEN = "sk-aitunnel-test"


class FakeAitunnel:
    """Мини-сервер AITUNNEL: STT отвечает JSON, TTS — PCM 24 кГц."""

    def __init__(self) -> None:
        self.stt_status = 200
        self.stt_body: Any = {"text": "  Оператор  "}
        self.tts_status = 200
        self.tts_body: bytes = b""
        self.tts_chunks: list[bytes] = []
        self.stt_headers: dict[str, str] = {}
        self.stt_fields: dict[str, str] = {}
        self.stt_file: bytes = b""
        self.tts_payload: dict[str, Any] = {}
        self._socket: socket.socket | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", self._handle_stt)
        app.router.add_post("/v1/audio/speech", self._handle_tts)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(64)
        self.port = int(self._socket.getsockname()[1])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.SockSite(self._runner, self._socket).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def _handle_stt(self, request: web.Request) -> web.StreamResponse:
        self.stt_headers = dict(request.headers)
        form = await request.post()
        self.stt_fields = {
            key: value for key, value in form.items() if isinstance(value, str)
        }
        file_field = form.get("file")
        if file_field is not None and hasattr(file_field, "file"):
            self.stt_file = file_field.file.read()
        if self.stt_status != 200:
            return web.Response(status=self.stt_status, text="stt failed")
        return web.json_response(self.stt_body)

    async def _handle_tts(self, request: web.Request) -> web.StreamResponse:
        self.tts_payload = await request.json()
        if self.tts_status != 200:
            return web.Response(status=self.tts_status, text="tts failed")
        if self.tts_chunks:
            response = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
            await response.prepare(request)
            for chunk in self.tts_chunks:
                await response.write(chunk)
            await response.write_eof()
            return response
        return web.Response(body=self.tts_body, content_type="audio/pcm")


def _settings(tmp_path, fake: FakeAitunnel, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "audiosocket_host": "127.0.0.1",
        "audiosocket_port": 0,
        "scenario_path": tmp_path / "scenario.json",
        "prompts_dir": tmp_path / "prompts",
        "record_calls": False,
        "recordings_dir": tmp_path / "recordings",
        "init_timeout": 3.0,
        "idle_timeout": 5.0,
        "max_call_seconds": 30.0,
        "log_level": "WARNING",
        "ai_tunnel_api_key": TOKEN,
        "ai_tunnel_base_url": fake.url,
        "stt_model": "whisper-large-v3-turbo",
        "tts_model": "gpt-4o-mini-tts",
        "tts_voice": "nova",
        "tts_format": "pcm",
        "tts_pcm_rate": 24000,
        "speech_language": "ru",
        "speech_timeout": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


def test_transcribe_sends_wav_multipart_and_returns_text(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeAitunnel()
        await fake.start()
        try:
            settings = _settings(tmp_path, fake)
            pcm = (np.ones(800, dtype="<i2") * 100).tobytes()
            async with SpeechClient(settings) as client:
                text = await client.transcribe(pcm)
            assert text == "Оператор"
            assert fake.stt_fields == {
                "model": "whisper-large-v3-turbo",
                "response_format": "json",
                "language": "ru",
            }
            assert fake.stt_file.startswith(b"RIFF")
            assert fake.stt_headers["Authorization"] == f"Bearer {TOKEN}"
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_transcribe_rejects_non_json_and_http_error(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeAitunnel()
        await fake.start()
        try:
            settings = _settings(tmp_path, fake)
            pcm = b"\x01\x00" * 160
            fake.stt_body = "<html>oops</html>"
            async with SpeechClient(settings) as client:
                with pytest.raises(SpeechError, match="JSON"):
                    await client.transcribe(pcm)

                fake.stt_status = 500
                with pytest.raises(SpeechError, match="HTTP 500"):
                    await client.transcribe(pcm)
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_stream_tts_resamples_pcm_to_8k(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeAitunnel()
        await fake.start()
        try:
            # 100 мс при 24 кГц -> 100 мс при 8 кГц (1600 байт).
            source = b"\x01\x00" * 2400
            fake.tts_chunks = [source[:1000], source[1000:3000], source[3000:]]
            settings = _settings(tmp_path, fake)
            async with SpeechClient(settings) as client:
                chunks = [chunk async for chunk in client.stream_tts("Проверка связи")]
            pcm = b"".join(chunks)
            assert abs(len(pcm) - 1600) <= 2
            assert fake.tts_payload == {
                "model": "gpt-4o-mini-tts",
                "input": "Проверка связи",
                "voice": "nova",
                "response_format": "pcm",
            }
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_stream_tts_raises_on_provider_error(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeAitunnel()
        await fake.start()
        fake.tts_status = 400
        try:
            settings = _settings(tmp_path, fake)
            async with SpeechClient(settings) as client:
                with pytest.raises(SpeechError, match="HTTP 400"):
                    async for _ in client.stream_tts("текст"):
                        pass
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_stream_tts_rejects_unsupported_format(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeAitunnel()
        await fake.start()
        try:
            settings = _settings(tmp_path, fake, tts_format="mp3")
            async with SpeechClient(settings) as client:
                with pytest.raises(SpeechError, match="TTS_FORMAT"):
                    async for _ in client.stream_tts("текст"):
                        pass
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_client_requires_api_key(tmp_path) -> None:
    async def scenario() -> None:
        settings = Settings(
            audiosocket_host="127.0.0.1",
            audiosocket_port=0,
            scenario_path=tmp_path / "s.json",
            prompts_dir=tmp_path,
            record_calls=False,
            recordings_dir=tmp_path,
            init_timeout=1,
            idle_timeout=1,
            max_call_seconds=1,
            log_level="WARNING",
        )
        client = SpeechClient(settings)
        assert client.enabled is False
        with pytest.raises(SpeechError, match="AITUNNEL_API_KEY"):
            await client.start()

    asyncio.run(scenario())


def test_to_wav_header() -> None:
    data = to_wav(b"\x00\x00" * 80)
    assert data.startswith(b"RIFF")
    assert data[24:28] == SAMPLE_RATE.to_bytes(4, "little")

