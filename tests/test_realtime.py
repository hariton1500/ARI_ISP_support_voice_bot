"""Тесты отдельного режима Yandex Realtime без сетевых вызовов."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.__main__ import main
from app.audiosocket.protocol import FrameKind, encode_frame
from app.audiosocket.server import AudioSocketServer
from app.billing.client import Subscriber
from app.config import Settings
from app.dialog.prompts import load_prompts
from app.llm.prompts import LLMPromptSet
from app.llm.tools import VoiceTools
from app.realtime.client import YandexRealtimeClient, realtime_tool_schemas
from app.realtime.session import RealtimeCallSession
from tests.helpers import wait_until


def _realtime_settings(settings: Settings) -> Settings:
    return settings.with_overrides(
        voice_provider="yandex_realtime",
        yandex_api_key="test-yandex-key",
        yandex_folder_id="test-folder",
        playback_lead_in_ms=0,
        record_calls=False,
    )


def _prompt_set() -> LLMPromptSet:
    return LLMPromptSet(system="Ты голосовой помощник", base="Ты голосовой помощник", knowledge={})


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


class _FakeHTTP:
    def __init__(self) -> None:
        self.closed = False
        self.websocket = _FakeWebSocket()
        self.url: str | None = None
        self.headers: dict[str, str] = {}
        self.kwargs: dict[str, Any] = {}

    async def ws_connect(self, url: str, **kwargs: Any) -> _FakeWebSocket:
        self.url = url
        self.headers = kwargs.pop("headers")
        self.kwargs = kwargs
        return self.websocket

    async def close(self) -> None:
        self.closed = True


class _FakeRealtime:
    def __init__(self) -> None:
        self.started = False
        self.configured = False
        self.closed = False
        self.greetings = 0
        self.appended: list[bytes] = []
        self.function_outputs: list[tuple[str, str]] = []
        self.response_requests = 0

    async def start(self) -> None:
        self.started = True

    async def configure(self) -> None:
        self.configured = True

    async def append_audio(self, pcm: bytes) -> None:
        self.appended.append(pcm)

    async def request_greeting(self) -> None:
        self.greetings += 1

    async def send_function_output(self, call_id: str, output: str) -> None:
        self.function_outputs.append((call_id, output))

    async def request_response(self) -> None:
        self.response_requests += 1

    async def events(self):
        if False:
            yield {}

    async def close(self) -> None:
        self.closed = True


class _FakeBilling:
    async def fetch_subscriber(self, user_id: str) -> Subscriber | None:
        return Subscriber(
            user_id=user_id,
            balance=227.23,
            packet_name="Интернет 100 Мбит",
            days_left=12,
        )


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _session(settings: Settings, scenario, realtime: _FakeRealtime, billing=None):
    writer = _Writer()
    session = RealtimeCallSession(
        asyncio.StreamReader(),
        writer,
        _realtime_settings(settings),
        scenario,
        {},
        _prompt_set(),
        channel_uuid=uuid.uuid4(),
        billing=billing,
        realtime=realtime,
    )
    session.session_id = "realtime-test"
    return session, writer


def test_realtime_settings_defaults_env_and_masking(monkeypatch) -> None:
    for name in (
        "VOICE_PROVIDER",
        "YANDEX_API_KEY",
        "YANDEX_FOLDER_ID",
        "YANDEX_DATA_LOGGING",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.voice_provider == "aitunnel"
    assert defaults.realtime_enabled is False
    assert defaults.yandex_realtime_model == "speech-realtime-max-tts-live/latest"
    assert defaults.yandex_realtime_voice == "sofia"
    assert defaults.yandex_data_logging is False

    monkeypatch.setenv("VOICE_PROVIDER", "yandex_realtime")
    monkeypatch.setenv("YANDEX_API_KEY", "secret-yandex")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder-1")
    monkeypatch.setenv("YANDEX_DATA_LOGGING", "true")
    configured = Settings.from_env()
    assert configured.realtime_enabled is True
    assert configured.masked()["yandex_api_key"] == "***"
    assert configured.yandex_data_logging is True


def test_invalid_voice_provider_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="VOICE_PROVIDER"):
        settings.with_overrides(voice_provider="unknown")


def test_cli_requires_yandex_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    assert main(
        [
            "--env-file",
            str(tmp_path / "empty.env"),
            "--voice-provider",
            "yandex_realtime",
            "--check",
        ]
    ) == 2


def test_client_configuration_headers_and_pcm(settings: Settings) -> None:
    async def scenario() -> None:
        http = _FakeHTTP()
        client = YandexRealtimeClient(
            _realtime_settings(settings),
            "system prompt",
            VoiceTools(None).schemas(),
            http_session=http,  # type: ignore[arg-type]
        )
        await client.start()
        await client.configure()
        pcm = b"\x01\x00" * 320
        await client.append_audio(pcm)
        await client.request_greeting()
        await client.close()

        assert http.url == "wss://ai.api.cloud.yandex.net/v1/realtime/openai"
        assert http.kwargs["params"] == {
            "model": "gpt://test-folder/speech-realtime-max-tts-live/latest"
        }
        assert http.headers == {
            "Authorization": "Api-Key test-yandex-key",
            "x-data-logging-enabled": "false",
        }
        update = http.websocket.sent[0]
        session = update["session"]
        assert update["type"] == "session.update"
        assert session["model"] == "gpt://test-folder/speech-realtime-max-tts-live/latest"
        assert session["instructions"] == "system prompt"
        assert session["output_modalities"] == ["audio"]
        assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
        assert session["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 700
        assert session["audio"]["output"] == {
            "format": {"type": "audio/pcm", "rate": 16000},
            "voice": "sofia",
        }
        assert all("function" not in tool for tool in session["tools"])
        append = http.websocket.sent[1]
        assert append["type"] == "input_audio_buffer.append"
        assert base64.b64decode(append["audio"]) == pcm
        assert http.websocket.sent[2]["type"] == "response.create"
        assert http.websocket.closed

    asyncio.run(scenario())


def test_realtime_tool_schemas_flatten_openai_format() -> None:
    tools = realtime_tool_schemas(VoiceTools(None).schemas())
    assert [tool["name"] for tool in tools] == [
        "get_billing_info",
        "transfer_to_operator",
        "end_call",
    ]
    assert tools[0]["parameters"]["required"] == ["user_id"]


def test_input_audio_is_streamed_as_16khz(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime)
        task = asyncio.create_task(session._send_input())
        try:
            for _ in range(20):
                session._feed_voice(b"\x01\x00" * 160)  # 20 мс, 8 кГц
            # SoXR VHQ имеет небольшую задержку фильтра: первые блоки выходят
            # до flush, но их число не обязано совпадать с числом входных.
            assert await wait_until(lambda: len(realtime.appended) >= 1)
            assert sum(map(len, realtime.appended)) <= 20 * 640
            assert all(len(block) == 640 for block in realtime.appended)
            assert session._realtime_input_bytes == sum(map(len, realtime.appended))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session._shutdown()

    asyncio.run(flow())


def test_output_audio_transcript_and_latency(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, writer = _session(settings, scenario, realtime)
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            await session._handle_event({"type": "input_audio_buffer.speech_stopped"})
            pcm16 = b"\x01\x00" * 640  # 20 мс, 16 кГц
            await session._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm16).decode("ascii"),
                }
            )
            await session._handle_event(
                {"type": "response.output_audio_transcript.done", "transcript": "Готово"}
            )
            await session._handle_event(
                {"type": "response.output_audio.done", "response_id": "r1"}
            )
            await session._handle_event(
                {"type": "response.done", "response": {"id": "r1"}}
            )
            assert await wait_until(
                lambda: session._playback_task is not None and session._playback_task.done()
            )
            assert writer.data, "аудио не отправлено в AudioSocket"
            assert session.stats.llm_replies == ["Готово"]
            assert session.stats.tts_characters == len("Готово")
            assert len(session._realtime_latencies_ms) == 1
            assert session._realtime_latencies_ms[0] >= 0
            assert session._realtime_output_bytes == len(pcm16)
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_realtime_usage_and_audio_are_aggregated_in_metadata(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime)
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            pcm16 = b"\x01\x00" * 320  # 20 мс, 16 кГц
            await session._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm16).decode("ascii"),
                }
            )
            await session._handle_event(
                {
                    "type": "response.done",
                    "response": {
                        "id": "r1",
                        "usage": {
                            "total_tokens": 1000,
                            "input_tokens": 800,
                            "input_token_details": {
                                "text_tokens": 600,
                                "audio_tokens": 150,
                                "cached_tokens": 500,
                                "tool_tokens": 50,
                                "cached_tokens_details": {
                                    "text_tokens": 450,
                                    "audio_tokens": 50,
                                },
                            },
                            "output_tokens": 200,
                            "output_token_details": {
                                "text_tokens": 20,
                                "audio_tokens": 180,
                            },
                        },
                    },
                }
            )
            await session._handle_event({"type": "response.created", "response": {"id": "r2"}})
            await session._handle_event(
                {
                    "type": "response.done",
                    "response": {
                        "id": "r2",
                        "usage": {
                            "total_tokens": 500,
                            "input_tokens": 400,
                            "input_token_details": {
                                "text_tokens": 300,
                                "audio_tokens": 100,
                                "cached_tokens": 200,
                                "tools_tokens": 20,
                                "cached_tokens_details": {
                                    "text_tokens": 180,
                                    "audio_tokens": 20,
                                },
                            },
                            "output_tokens": 100,
                            "output_token_details": {
                                "text_tokens": 10,
                                "audio_tokens": 90,
                            },
                        },
                    },
                }
            )

            metadata = session._metadata_extra()
            assert metadata["realtime_model"] == "speech-realtime-max-tts-live/latest"
            assert metadata["realtime_system_prompt_characters"] == len(_prompt_set().system)
            assert metadata["realtime_input_audio_seconds"] == 0
            assert metadata["realtime_output_audio_seconds"] == pytest.approx(0.02)
            assert metadata["realtime_usage"] == {
                "responses": 2,
                "tool_tokens": 70,
                "total_tokens": 1500,
                "input_tokens": 1200,
                "input_text_tokens": 900,
                "input_audio_tokens": 250,
                "cached_input_tokens": 700,
                "cached_input_text_tokens": 630,
                "cached_input_audio_tokens": 70,
                "output_tokens": 300,
                "output_text_tokens": 30,
                "output_audio_tokens": 270,
                "uncached_input_tokens": 500,
            }
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_billing_function_returns_output_and_requests_response(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime, billing=_FakeBilling())
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "get_billing_info",
                        "call_id": "call-1",
                        "arguments": json.dumps({"user_id": "1"}),
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r1"}})
            assert await wait_until(lambda: realtime.response_requests == 1)
            assert len(realtime.function_outputs) == 1
            call_id, output = realtime.function_outputs[0]
            result = json.loads(output)
            assert call_id == "call-1"
            assert result["found"] is True
            assert result["user"]["extra_account"] == pytest.approx(227.23)
            assert session.stats.llm_tool_calls == 1
            assert session.stats.billing["outcome"] == "found"
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_transfer_is_rejected_until_billing_result_is_spoken(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime, billing=_FakeBilling())
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "get_billing_info",
                        "call_id": "call-billing",
                        "arguments": json.dumps({"user_id": "20"}),
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r1"}})
            assert await wait_until(lambda: realtime.response_requests == 1)

            await session._handle_event({"type": "response.created", "response": {"id": "r2"}})
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "transfer_to_operator",
                        "call_id": "call-early-transfer",
                        "arguments": "{}",
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r2"}})
            assert await wait_until(lambda: realtime.response_requests == 2)
            rejected = json.loads(realtime.function_outputs[-1][1])
            assert rejected["error"] == "billing_answer_required"
            assert not session._dialog_done.is_set()
            assert session.stats.hangup_reason == "dialog_end"

            await session._handle_event({"type": "response.created", "response": {"id": "r3"}})
            await session._handle_event(
                {
                    "type": "response.output_audio_transcript.done",
                    "transcript": "Баланс 227 рублей 23 копейки.",
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r3"}})
            assert await wait_until(lambda: not session._finalizer_tasks)

            await session._handle_event({"type": "response.created", "response": {"id": "r4"}})
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "transfer_to_operator",
                        "call_id": "call-requested-transfer",
                        "arguments": "{}",
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r4"}})
            assert await wait_until(lambda: session._dialog_done.is_set())
            assert session.stats.hangup_reason == "realtime_transfer"
            assert json.loads(realtime.function_outputs[-1][1])["status"] == "transferring"
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_end_call_waits_for_audio_playback(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, writer = _session(settings, scenario, realtime)
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            pcm16 = b"\x01\x00" * 3200  # 100 мс, 16 кГц
            await session._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm16).decode("ascii"),
                }
            )
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "end_call",
                        "call_id": "call-end",
                        "arguments": "{}",
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r1"}})
            assert not session._dialog_done.is_set(), "звонок завершён до проигрывания"
            assert await wait_until(lambda: session._dialog_done.is_set())
            assert writer.data
            assert realtime.response_requests == 0
            assert session.stats.hangup_reason == "realtime_end_call"
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_echo_guard_blocks_input_during_playback_and_tail(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime)
        pcm = b"\x01\x00" * 160
        try:
            session._bot_speaking = True
            session._feed_voice(pcm)
            assert session._realtime_input.qsize() == 0

            session._bot_speaking = False
            session._echo_guard_until = float("inf")
            session._feed_voice(pcm)
            assert session._realtime_input.qsize() == 0

            session._echo_guard_until = 0.0
            session._feed_voice(pcm)
            assert session._realtime_input.get_nowait() == pcm
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_echo_guard_filters_transcript_and_disables_barge_in(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime)
        try:
            session._remember_bot_text("Здравствуйте! Меня зовут ботовый ассистент.")
            await session._handle_event(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Меня зовут",
                }
            )
            assert session.stats.ignored_echo == 1
            assert session.stats.transcripts == []
            assert session.stats.llm_turns == 0

            session._response_has_audio = True
            await session._handle_event({"type": "input_audio_buffer.speech_started"})
            assert session.stats.barge_ins == 0
            assert session._response_has_audio is True
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_barge_in_discards_pending_response_audio(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(
            settings.with_overrides(voice_echo_guard=False), scenario, realtime
        )
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            pcm16 = b"\x01\x00" * 8000  # 250 мс, 16 кГц
            await session._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm16).decode("ascii"),
                }
            )
            await asyncio.sleep(0.03)
            await session._handle_event({"type": "input_audio_buffer.speech_started"})
            assert session.stats.barge_ins == 1
            assert session._active_response_id is None
            assert session._realtime_output is not None
            assert session._realtime_output.qsize() == 1  # только sentinel
            # Поздняя дельта прерванного ответа не должна вернуться в очередь.
            await session._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm16).decode("ascii"),
                }
            )
            assert session._realtime_output.qsize() <= 1
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_transfer_finishes_without_requesting_another_response(settings: Settings, scenario) -> None:
    async def flow() -> None:
        realtime = _FakeRealtime()
        session, _writer = _session(settings, scenario, realtime)
        try:
            await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
            await session._handle_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "transfer_to_operator",
                        "call_id": "call-transfer",
                        "arguments": "{}",
                    },
                }
            )
            await session._handle_event({"type": "response.done", "response": {"id": "r1"}})
            assert await wait_until(lambda: session._dialog_done.is_set())
            assert session.stats.hangup_reason == "realtime_transfer"
            assert realtime.response_requests == 0
            assert json.loads(realtime.function_outputs[0][1])["number"] == "508"
        finally:
            await session._shutdown()

    asyncio.run(flow())


def test_realtime_error_closes_connection_and_tasks(settings: Settings, scenario) -> None:
    class ErrorRealtime(_FakeRealtime):
        async def events(self):
            yield {"type": "error", "error": {"message": "test failure"}}

    async def flow() -> None:
        realtime = ErrorRealtime()
        session, writer = _session(settings, scenario, realtime)
        await asyncio.wait_for(session.run(), timeout=3.0)
        assert realtime.started and realtime.configured and realtime.closed
        assert realtime.greetings == 1
        assert session.is_closed
        assert session.stats.hangup_reason == "realtime_error"
        assert session.stats.llm_errors == 1
        assert session._input_task is None and session._event_task is None
        assert writer.closed

    asyncio.run(flow())


def test_server_selects_realtime_only_for_explicit_provider(
    settings: Settings, scenario, prompts_dir: Path, monkeypatch
) -> None:
    created: list[Any] = []

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append((args, kwargs))

        async def run(self) -> None:
            return None

        def abort(self) -> None:
            return None

    async def flow() -> None:
        monkeypatch.setattr("app.audiosocket.server.RealtimeCallSession", FakeSession)
        server = AudioSocketServer(
            _realtime_settings(settings),
            scenario,
            load_prompts(scenario, prompts_dir),
            llm_prompts=_prompt_set(),
        )
        await server.start()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
            writer.write(encode_frame(FrameKind.UUID, uuid.uuid4().bytes))
            await writer.drain()
            assert await wait_until(lambda: bool(created))
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

    asyncio.run(flow())
    assert len(created) == 1
