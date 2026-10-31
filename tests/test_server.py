"""Сквозные тесты: TCP-соединение AudioSocket → диалог → запись."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
import wave
from pathlib import Path
from typing import Any

import pytest
import numpy as np

from app.audiosocket.protocol import (
    Frame,
    FrameKind,
    encode_audio,
    encode_dtmf,
    encode_frame,
    read_frame,
)
from app.audiosocket.registry import PendingCall, PendingRegistry
from app.audiosocket.server import AudioSocketServer
from app.billing.client import BillingError, Subscriber
from app.config import Settings
from app.dialog.prompts import PromptMap, load_prompts
from app.dialog.scenario import Scenario
from app.llm.conversation import LLMReply
from tests.conftest import write_wav


async def _read_until_eof(reader: asyncio.StreamReader, timeout: float = 10.0) -> list[Frame]:
    frames: list[Frame] = []
    while True:
        frame = await asyncio.wait_for(read_frame(reader), timeout)
        if frame is None:
            return frames
        frames.append(frame)


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _call_flow(settings: Settings, scenario: Scenario, prompts: PromptMap) -> None:
    server = AudioSocketServer(settings, scenario, prompts)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        channel = uuid.uuid4()
        writer.write(encode_frame(FrameKind.UUID, channel.bytes))
        await writer.drain()

        # «Голос абонента»: эти фреймы должны попасть в запись.
        for _ in range(5):
            writer.write(encode_audio(b"\x01\x00" * 160))
        await writer.drain()

        # В ответ приходит звук приветствия.
        received = b""
        while len(received) < 640:
            frame = await asyncio.wait_for(read_frame(reader), timeout=5.0)
            assert frame is not None, "сервер закрыл соединение до проигрывания промпта"
            assert frame.kind == FrameKind.AUDIO_8K
            assert frame.sample_rate == 8000
            received += frame.payload
        assert len(received) % 2 == 0

        # Цифра 0 — переход к оператору и завершение вызова.
        writer.write(encode_dtmf("0"))
        await writer.drain()

        frames = await _read_until_eof(reader)
        assert any(frame.kind == FrameKind.TERMINATE for frame in frames), frames
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()

    assert server.active_sessions == 0

    recording = settings.recordings_dir / f"{channel}.wav"
    assert recording.is_file(), f"нет записи {recording}"
    with wave.open(str(recording), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8000
        assert wav.getnframes() > 0

    meta_path = recording.with_suffix(".json")
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["dtmf"] == "0"
    assert meta["steps"] == ["greeting", "menu", "operator"]
    assert meta["hangup_reason"] == "hangup_step"
    assert meta["audio_frames"] >= 5
    assert meta["audio_seconds"] > 0
    assert meta["recording"] == recording.name


def test_audio_socket_call_flow(
    settings: Settings, scenario: Scenario, prompts_dir: Path
) -> None:
    prompts = load_prompts(scenario, prompts_dir)
    assert prompts, "промпты должны загрузиться"
    asyncio.run(_call_flow(settings, scenario, prompts))


async def _idle_flow(settings: Settings, scenario: Scenario, prompts: PromptMap) -> None:
    server = AudioSocketServer(settings, scenario, prompts)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(encode_frame(FrameKind.UUID, uuid.uuid4().bytes))
        await writer.drain()

        frames = await _read_until_eof(reader, timeout=5.0)
        assert any(frame.kind == FrameKind.TERMINATE for frame in frames)
        writer.close()
    finally:
        await server.close()


def test_idle_timeout_closes_call(
    settings: Settings, scenario: Scenario, prompts_dir: Path
) -> None:
    prompts = load_prompts(scenario, prompts_dir)
    silent_settings = settings.with_overrides(idle_timeout=0.3)
    asyncio.run(_idle_flow(silent_settings, scenario, prompts))


async def _disconnect_flow(settings: Settings, scenario: Scenario, prompts: PromptMap) -> None:
    server = AudioSocketServer(settings, scenario, prompts)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(encode_frame(FrameKind.UUID, uuid.uuid4().bytes))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        assert await _wait_until(lambda: server.active_sessions == 0), "сессия не закрылась"
        assert server.is_running
    finally:
        await server.close()


def test_client_disconnect_does_not_break_server(
    settings: Settings, scenario: Scenario, prompts_dir: Path
) -> None:
    prompts = load_prompts(scenario, prompts_dir)
    asyncio.run(_disconnect_flow(settings, scenario, prompts))


# ------------------------------------------------------------------ биллинг


class _FakeBilling:
    """Заглушка клиента биллинга: отдаёт абонента, None или ошибку."""

    def __init__(
        self,
        subscriber: Subscriber | None = None,
        error: BillingError | None = None,
    ) -> None:
        self.subscriber = subscriber
        self.error = error
        self.user_ids: list[str] = []

    async def fetch_subscriber(self, user_id: str) -> Subscriber | None:
        self.user_ids.append(user_id)
        if self.error is not None:
            raise self.error
        return self.subscriber


def _billing_scenario() -> Scenario:
    raw = {
        "name": "billing",
        "initial": "greeting",
        "steps": {
            "greeting": {"prompt": "greeting.wav", "next": "balance"},
            "balance": {
                "prompt": "balance.wav",
                "prompt_not_found": "balance_not_found.wav",
                "prompt_error": "balance_error.wav",
                "billing": "caller",
                "next": "operator",
            },
            "operator": {"prompt": "operator.wav", "hangup": True},
        },
    }
    return Scenario.from_dict(raw)


async def _billing_flow(
    tmp_path: Path, billing: _FakeBilling | None
) -> tuple[int, dict[str, Any]]:
    """Прогнать вызов с шагом billing; вернуть (байт промптов, метаданные)."""
    scenario = _billing_scenario()
    prompts_dir = tmp_path / "prompts"
    write_wav(prompts_dir / "greeting.wav", duration_ms=200)
    write_wav(prompts_dir / "balance.wav", duration_ms=100)
    write_wav(prompts_dir / "balance_not_found.wav", duration_ms=400)
    write_wav(prompts_dir / "balance_error.wav", duration_ms=600)
    write_wav(prompts_dir / "operator.wav", duration_ms=200)
    prompts = load_prompts(scenario, prompts_dir)

    settings = Settings(
        audiosocket_host="127.0.0.1",
        audiosocket_port=0,
        scenario_path=tmp_path / "scenario.json",
        prompts_dir=prompts_dir,
        record_calls=True,
        recordings_dir=tmp_path / "recordings",
        init_timeout=3.0,
        idle_timeout=5.0,
        max_call_seconds=20.0,
        log_level="WARNING",
    )

    channel_uuid = uuid.uuid4()
    registry = PendingRegistry()
    registry.register(
        PendingCall(uuid=str(channel_uuid), channel_id="call-1", caller_number="1")
    )
    server = AudioSocketServer(settings, scenario, prompts, registry=registry, billing=billing)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(encode_frame(FrameKind.UUID, channel_uuid.bytes))
        await writer.drain()
        frames = await _read_until_eof(reader)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await server.close()

    total = sum(len(frame.payload) for frame in frames if frame.kind == FrameKind.AUDIO_8K)
    meta = json.loads(
        (settings.recordings_dir / f"{channel_uuid}.json").read_text(encoding="utf-8")
    )
    return total, meta


def test_billing_step_plays_balance_prompt(tmp_path: Path) -> None:
    billing = _FakeBilling(
        subscriber=Subscriber(
            user_id="1", balance=227.23, packet_name="Интернет 100 Мбит", days_left=12
        )
    )
    total, meta = asyncio.run(_billing_flow(tmp_path, billing))

    assert billing.user_ids == ["1"]
    # greeting 200 мс + balance 100 мс + operator 200 мс (8 кГц, 16 бит)
    assert total == 8000
    assert meta["steps"] == ["greeting", "balance", "operator"]
    assert meta["billing"]["outcome"] == "found"
    assert meta["billing"]["user_id"] == "1"
    assert meta["billing"]["balance"] == pytest.approx(227.23)


def test_billing_step_plays_not_found_prompt(tmp_path: Path) -> None:
    total, meta = asyncio.run(_billing_flow(tmp_path, _FakeBilling()))
    # greeting 200 мс + balance_not_found 400 мс + operator 200 мс
    assert total == 12800
    assert meta["billing"]["outcome"] == "not_found"


def test_billing_step_plays_error_prompt(tmp_path: Path) -> None:
    billing = _FakeBilling(error=BillingError("нет связи с биллингом"))
    total, meta = asyncio.run(_billing_flow(tmp_path, billing))
    # greeting 200 мс + balance_error 600 мс + operator 200 мс
    assert total == 16000
    assert meta["billing"]["outcome"] == "error"
    assert "нет связи" in meta["billing"]["reason"]


def test_billing_step_without_client_uses_error_prompt(tmp_path: Path) -> None:
    total, meta = asyncio.run(_billing_flow(tmp_path, None))
    assert total == 16000
    assert meta["billing"]["outcome"] == "error"
    assert meta["billing"]["reason"] == "not_configured"


# ------------------------------------------------------- голос: VAD и barge-in


class _FakeSpeech:
    """Заглушка AITUNNEL: расшифровка и потоковый TTS без сети."""

    def __init__(self, text: str = "Соедините с оператором", tts_pcm: bytes = b"") -> None:
        self.text = text
        self.tts_pcm = tts_pcm
        self.transcriptions: list[bytes] = []
        self.tts_texts: list[str] = []

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int = 8000, language: str | None = None
    ) -> str:
        self.transcriptions.append(pcm)
        return self.text

    async def stream_tts(
        self, text: str, *, voice: str | None = None, model: str | None = None
    ):
        self.tts_texts.append(text)
        yield self.tts_pcm


def _tone_frame(amplitude: float = 0.8) -> bytes:
    count = 160  # 20 мс при 8 кГц
    time = np.arange(count) / 8000
    samples = (np.sin(2 * np.pi * 440 * time) * amplitude * 32767).astype("<i2")
    return samples.tobytes()


def _silence_frame() -> bytes:
    return b"\x00" * 320


def _voice_scenario(text_only: bool = False, llm: bool = False) -> Scenario:
    greeting: dict[str, Any] = (
        {"text": "Здравствуйте! Это служба поддержки.", "hangup": True}
        if text_only
        else {"prompt": "greeting.wav", "next": "menu"}
    )
    return Scenario.from_dict(
        {
            "name": "voice",
            "initial": "greeting",
            "steps": {
                "greeting": greeting,
                "menu": {
                    "prompt": "menu.wav",
                    "llm": llm,
                    "timeout_ms": 3000,
                    "max_attempts": 2,
                    "dtmf": {"0": "operator", "1": "operator"},
                    "voice": {"оператор": "operator", "соедините с оператором": "operator"},
                    "on_timeout": "operator",
                    "on_invalid": "operator",
                },
                "operator": {"prompt": "operator.wav", "hangup": True},
            },
        }
    )


def _voice_settings(tmp_path: Path, prompts_dir: Path) -> Settings:
    return Settings(
        audiosocket_host="127.0.0.1",
        audiosocket_port=0,
        scenario_path=tmp_path / "scenario.json",
        prompts_dir=prompts_dir,
        record_calls=True,
        recordings_dir=tmp_path / "recordings",
        init_timeout=3.0,
        idle_timeout=5.0,
        max_call_seconds=20.0,
        log_level="WARNING",
        vad_start_ms=60,
        vad_silence_ms=200,
        vad_min_speech_ms=60,
        stt_partial_interval_ms=0,
    )


async def _voice_flow(
    tmp_path: Path, speech: _FakeSpeech, llm: Any = None
) -> dict[str, Any]:
    scenario = _voice_scenario(llm=llm is not None)
    prompts_dir = tmp_path / "voice-prompts"
    write_wav(prompts_dir / "greeting.wav", duration_ms=1000)
    write_wav(prompts_dir / "menu.wav", duration_ms=200)
    write_wav(prompts_dir / "operator.wav", duration_ms=100)
    prompts = load_prompts(scenario, prompts_dir)
    settings = _voice_settings(tmp_path, prompts_dir)

    channel_uuid = uuid.uuid4()
    server = AudioSocketServer(settings, scenario, prompts, speech=speech, llm=llm)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(encode_frame(FrameKind.UUID, channel_uuid.bytes))
        await writer.drain()
        await asyncio.sleep(0.1)  # приветствие уже играет

        for _ in range(3):
            writer.write(encode_audio(_silence_frame()))
        for _ in range(15):
            writer.write(encode_audio(_tone_frame()))
        for _ in range(12):
            writer.write(encode_audio(_silence_frame()))
        await writer.drain()

        await _read_until_eof(reader, timeout=10.0)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await server.close()

    return json.loads(
        (settings.recordings_dir / f"{channel_uuid}.json").read_text(encoding="utf-8")
    )


def test_voice_barge_in_and_transcript_transition(tmp_path: Path) -> None:
    speech = _FakeSpeech()
    meta = asyncio.run(_voice_flow(tmp_path, speech))

    assert speech.transcriptions, "STT не вызывался"
    assert meta["transcripts"] == ["Соедините с оператором"]
    assert meta["barge_ins"] >= 1
    assert meta["steps"] == ["greeting", "menu", "operator"]
    assert meta["hangup_reason"] == "hangup_step"


async def _tts_flow(tmp_path: Path) -> tuple[_FakeSpeech, list[Frame], dict[str, Any]]:
    scenario = _voice_scenario(text_only=True)
    prompts_dir = tmp_path / "tts-prompts"
    prompts_dir.mkdir(parents=True)
    prompts = load_prompts(scenario, prompts_dir)
    settings = _voice_settings(tmp_path, prompts_dir)
    speech = _FakeSpeech(tts_pcm=b"\x01\x00" * 1600)  # 100 мс при 8 кГц

    channel_uuid = uuid.uuid4()
    server = AudioSocketServer(settings, scenario, prompts, speech=speech)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(encode_frame(FrameKind.UUID, channel_uuid.bytes))
        await writer.drain()
        frames = await _read_until_eof(reader, timeout=10.0)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await server.close()

    meta = json.loads(
        (settings.recordings_dir / f"{channel_uuid}.json").read_text(encoding="utf-8")
    )
    return speech, frames, meta


def test_tts_text_step_streams_audio(tmp_path: Path) -> None:
    speech, frames, meta = asyncio.run(_tts_flow(tmp_path))

    assert speech.tts_texts == ["Здравствуйте! Это служба поддержки."]
    total = sum(len(frame.payload) for frame in frames if frame.kind == FrameKind.AUDIO_8K)
    assert total == 3200
    assert meta["tts_characters"] == len("Здравствуйте! Это служба поддержки.")
    assert meta["hangup_reason"] == "hangup_step"


# --------------------------------------------------------------- LLM-диалог


class _FakeLLMConversation:
    """Заглушка разговора: запоминает реплики и сразу завершает звонок."""

    def __init__(self, backend: _FakeLLM) -> None:
        self._backend = backend
        self.tool_calls = 0

    async def ask(self, text: str) -> LLMReply:
        self._backend.questions.append(text)
        self._backend.tools.append(self._backend.last_tools)
        return LLMReply(
            text="Тарифы уточняются по адресу. Всего доброго!",
            end_call=True,
        )


class _FakeLLM:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.last_tools: Any = None
        self.tools: list[Any] = []

    def conversation(self, tools: Any) -> _FakeLLMConversation:
        self.last_tools = tools
        return _FakeLLMConversation(self)


def test_llm_step_answers_free_speech_and_ends_call(tmp_path: Path) -> None:
    text = "Расскажите, какие у вас тарифы"
    speech = _FakeSpeech(text=text, tts_pcm=b"\x01\x00" * 80)
    llm = _FakeLLM()

    meta = asyncio.run(_voice_flow(tmp_path, speech, llm=llm))

    assert llm.questions == [text]
    assert llm.tools and llm.tools[0] is not None
    assert meta["transcripts"] == [text]
    assert meta["llm_turns"] == 1
    assert meta["llm_replies"] == ["Тарифы уточняются по адресу. Всего доброго!"]
    assert meta["llm_tool_calls"] == 0
    assert meta["hangup_reason"] == "llm_end_call"
    assert meta["steps"] == ["greeting", "menu"]
    assert meta["tts_characters"] == len("Тарифы уточняются по адресу. Всего доброго!")
