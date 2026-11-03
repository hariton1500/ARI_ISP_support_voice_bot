"""Тесты ARI-слоя: REST-клиент, WebSocket, оркестрация вызова."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.ari.client import AriClient, AriError
from app.ari.events import AriEventStream
from app.ari.manager import AriCallManager
from app.audiosocket.protocol import Frame, FrameKind, encode_frame, read_frame
from app.audiosocket.registry import PendingRegistry
from app.audiosocket.server import AudioSocketServer
from app.config import Settings
from app.dialog.prompts import load_prompts
from app.dialog.scenario import Scenario
from tests.conftest import write_wav
from tests.fake_ari import FakeAri
from tests.helpers import wait_until


def _settings(tmp_path: Path, fake: FakeAri, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "audiosocket_host": "127.0.0.1",
        "audiosocket_port": 0,
        "scenario_path": tmp_path / "scenario.json",
        "prompts_dir": tmp_path / "prompts",
        "record_calls": True,
        "recordings_dir": tmp_path / "recordings",
        "init_timeout": 3.0,
        "idle_timeout": 5.0,
        "max_call_seconds": 20.0,
        "log_level": "WARNING",
        "ari_url": fake.base_url,
        "ari_user": "ivr_ai",
        "ari_password": "secret",
        "ari_app": "ivr_ai",
        "external_media_host": "127.0.0.1",
        "external_media_timeout": 3.0,
        "allow_unknown_sessions": False,
    }
    base.update(overrides)
    return Settings(**base)


async def _noop_event(event: dict[str, Any]) -> None:
    return None


def test_client_methods_and_auth(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAri()
        await fake.start()
        try:
            settings = _settings(tmp_path, fake)
            async with AriClient(settings) as client:
                info = await client.asterisk_info()
                assert info["system"]["version"].startswith("22.5")

                await client.answer("call-1")
                bridge = await client.create_bridge()
                assert bridge == "bridge-1"
                await client.add_channels(bridge, ["call-1", "em-1"])
                media = await client.create_external_media(
                    app="ivr_ai", external_host="example.org:9092", data="uuid-1"
                )
                assert media["id"] == "em-1"
                await client.delete_channel("em-1")
                await client.delete_bridge(bridge)

                with pytest.raises(AriError) as not_found:
                    await client.get_channel("missing")
                assert not_found.value.status == 404

            assert ("POST", "/ari/channels/call-1/answer", {}) in fake.calls
            assert fake.params_of("/addChannel")[0]["channel"] == "call-1,em-1"
            em = fake.em_requests[0]
            assert em["external_host"] == "example.org:9092"
            assert em["encapsulation"] == "audiosocket"
            assert em["transport"] == "tcp"
            assert em["format"] == "slin"
            assert em["data"] == "uuid-1"
            assert "/ari/channels/em-1" in fake.paths("DELETE")
            assert "/ari/bridges/bridge-1" in fake.paths("DELETE")
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_client_reports_unauthorized(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAri(password="secret")
        await fake.start()
        try:
            settings = _settings(tmp_path, fake, ari_password="wrong")
            async with AriClient(settings) as client:
                with pytest.raises(AriError) as exc_info:
                    await client.asterisk_info()
                assert exc_info.value.status == 401
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_event_stream_reconnects() -> None:
    async def scenario() -> None:
        fake = FakeAri()
        fake.close_ws_on_connect = 1  # первый коннект сразу закрываем
        await fake.start()
        stream = AriEventStream(
            fake.ws_url, "ivr_ai", "secret", _noop_event, reconnect_min=0.05, reconnect_max=0.2
        )
        task = asyncio.create_task(stream.run(), name="test-ari-stream")
        try:
            assert await wait_until(
                lambda: fake.ws_connections >= 2 and stream.is_connected, timeout=5.0
            )
        finally:
            await stream.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await fake.stop()

    asyncio.run(scenario())


def _scenario(tmp_path: Path) -> Scenario:
    prompts_dir = tmp_path / "prompts"
    for name in ("greeting", "menu", "bye"):
        write_wav(prompts_dir / f"{name}.wav", duration_ms=150)
    raw = {
        "name": "ari-test",
        "initial": "greeting",
        "steps": {
            "greeting": {"prompt": "greeting.wav", "next": "menu"},
            "menu": {
                "prompt": "menu.wav",
                "timeout_ms": 300,
                "max_attempts": 1,
                "voice": {"завершить": "bye"},
                "on_timeout": "bye",
            },
            "bye": {"prompt": "bye.wav", "hangup": True},
        },
    }
    return Scenario.from_dict(raw, source=tmp_path / "scenario.json")


async def _read_audio(reader: asyncio.StreamReader, minimum: int) -> int:
    received = 0
    while received < minimum:
        frame: Frame | None = await asyncio.wait_for(read_frame(reader), timeout=5.0)
        assert frame is not None, "соединение закрылось до проигрывания промпта"
        if frame.kind == FrameKind.AUDIO_8K:
            received += len(frame.payload)
    return received


def test_full_call_flow(tmp_path: Path) -> None:
    """StasisStart → бридж → ExternalMedia → AudioSocket → голосовой шаг → очистка."""

    async def scenario() -> None:
        fake = FakeAri()
        await fake.start()
        dialog = _scenario(tmp_path)
        prompts = load_prompts(dialog, tmp_path / "prompts")
        settings = _settings(tmp_path, fake)
        registry = PendingRegistry()
        server = AudioSocketServer(settings, dialog, prompts, registry=registry)
        await server.start()
        try:
            port = server.bound_port
            assert port
            settings = settings.with_overrides(external_media_port=port)
            manager = AriCallManager(settings, registry)
            await manager.start()
            try:
                assert await wait_until(lambda: manager.is_connected, timeout=5.0), "нет WS"

                await fake.send_event(
                    {
                        "type": "StasisStart",
                        "application": "ivr_ai",
                        "channel": {"id": "call-1", "caller": {"number": "79001112233"}},
                    }
                )
                assert await wait_until(lambda: fake.em_requests, timeout=5.0), fake.calls
                media = fake.em_requests[0]
                assert media["external_host"] == f"127.0.0.1:{port}"
                session_uuid = uuid.UUID(media["data"])

                # «Asterisk» подключается к нашему AudioSocket с UUID из data.
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(encode_frame(FrameKind.UUID, session_uuid.bytes))
                await writer.drain()
                assert await wait_until(lambda: len(registry) == 0, timeout=5.0)

                await _read_audio(reader, 640)
                while True:
                    frame = await asyncio.wait_for(read_frame(reader), timeout=5.0)
                    if frame is None:
                        break
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

                assert await wait_until(lambda: manager.active_calls == 0, timeout=5.0)
                assert await wait_until(
                    lambda: "/ari/channels/call-1" in fake.paths("DELETE"), 5.0
                )

                assert ("POST", "/ari/channels/call-1/answer", {}) in fake.calls
                assert fake.params_of("/addChannel")[0]["channel"] == "call-1,em-1"
                deletes = set(fake.paths("DELETE"))
                expected = {"/ari/channels/em-1", "/ari/channels/call-1", "/ari/bridges/bridge-1"}
                assert expected <= deletes, deletes
            finally:
                await manager.stop()
        finally:
            await server.close()
            await fake.stop()

        recording = settings.recordings_dir / f"{session_uuid}.wav"
        assert recording.is_file(), "запись не создана"
        meta = json.loads(recording.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["steps"] == ["greeting", "menu", "bye"]
        assert "dtmf" not in meta
        assert meta["channel_id"] == "call-1"
        assert meta["caller"] == "79001112233"
        assert meta["hangup_reason"] == "hangup_step"

    asyncio.run(scenario())


def test_unknown_uuid_rejected(tmp_path: Path) -> None:
    """В ARI-режиме неизвестный UUID не должен создавать сессию."""

    async def scenario() -> None:
        fake = FakeAri()
        await fake.start()
        dialog = _scenario(tmp_path)
        prompts = load_prompts(dialog, tmp_path / "prompts")
        settings = _settings(tmp_path, fake)
        registry = PendingRegistry()
        server = AudioSocketServer(settings, dialog, prompts, registry=registry)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
            writer.write(encode_frame(FrameKind.UUID, uuid.uuid4().bytes))
            await writer.drain()
            assert await asyncio.wait_for(read_frame(reader), timeout=5.0) is None
            assert server.active_sessions == 0
            writer.close()
        finally:
            await server.close()
            await fake.stop()

    asyncio.run(scenario())
