"""Сквозные тесты: TCP-соединение AudioSocket → диалог → запись."""

from __future__ import annotations

import asyncio
import json
import uuid
import wave
from pathlib import Path

from app.audiosocket.protocol import (
    Frame,
    FrameKind,
    encode_audio,
    encode_dtmf,
    encode_frame,
    read_frame,
)
from app.audiosocket.server import AudioSocketServer
from app.config import Settings
from app.dialog.prompts import PromptMap, load_prompts
from app.dialog.scenario import Scenario


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
