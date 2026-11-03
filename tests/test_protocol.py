"""Тесты протокола AudioSocket."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.audiosocket.protocol import (
    Frame,
    FrameKind,
    encode_audio,
    encode_frame,
    encode_terminate,
    kind_name,
    read_frame,
)


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_encode_frame_header_is_big_endian() -> None:
    packet = encode_frame(FrameKind.UUID, bytes(range(16)))
    assert packet[:3] == b"\x01\x00\x10"
    assert packet[3:] == bytes(range(16))


def test_encode_audio_drops_half_sample() -> None:
    packet = encode_audio(b"\x01\x02\x03")
    assert packet[:3] == b"\x10\x00\x02"
    assert packet[3:] == b"\x01\x02"


def test_encode_terminate_is_three_zero_bytes() -> None:
    assert encode_terminate() == b"\x00\x00\x00"


def test_frame_payload_too_large() -> None:
    with pytest.raises(ValueError):
        encode_frame(0x10, b"\x00" * 0x10000)


def test_read_frame_round_trip() -> None:
    channel = uuid.uuid4()
    data = encode_frame(FrameKind.UUID, channel.bytes) + encode_audio(b"\x00\x00")

    async def scenario() -> tuple[Frame | None, Frame | None, Frame | None]:
        reader = _reader_with(data)
        first = await read_frame(reader)
        second = await read_frame(reader)
        third = await read_frame(reader)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first is not None and first.as_uuid() == channel
    assert second is not None and second.kind == FrameKind.AUDIO_8K
    assert second.sample_rate == 8000
    assert third is None


def test_read_frame_partial_payload_raises() -> None:
    async def scenario() -> None:
        reader = _reader_with(b"\x10\x00\x04\x01\x02")
        await read_frame(reader)

    with pytest.raises(ConnectionError):
        asyncio.run(scenario())


def test_read_frame_timeout() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        await read_frame(reader, timeout=0.05)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(scenario())


def test_uuid_frame_with_bad_length() -> None:
    assert Frame(FrameKind.UUID, b"\x00" * 15).as_uuid() is None


def test_kind_names() -> None:
    assert kind_name(FrameKind.AUDIO_8K) == "AUDIO_8K"
    assert kind_name(0x7A) == "UNKNOWN_0x7a"
