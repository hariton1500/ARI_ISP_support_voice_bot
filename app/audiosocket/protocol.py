"""Протокол AudioSocket (Asterisk).

Формат: 3-байтовый заголовок (1 байт типа + 2 байта длины payload, big-endian)
и payload. Минимальное сообщение — 0x00 0x00 0x00 (завершение соединения).

Типы фреймов:
    0x00 — завершение соединения;
    0x01 — UUID канала (16 байт бинарного представления);
    0x03 — DTMF-цифра (1 байт ASCII);
    0x10..0x18 — PCM 16-бит, моно, little-endian, с частотой 8..192 кГц;
    0xff — ошибка (payload — код ошибки).

Документация: https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum

HEADER_SIZE = 3
MAX_PAYLOAD = 0xFFFF
_HEADER = struct.Struct(">BH")

# DTMF, который принимает Asterisk.
DTMF_ALPHABET = frozenset("0123456789*#ABCD")


class FrameKind(IntEnum):
    TERMINATE = 0x00
    UUID = 0x01
    DTMF = 0x03
    AUDIO_8K = 0x10
    AUDIO_12K = 0x11
    AUDIO_16K = 0x12
    AUDIO_24K = 0x13
    AUDIO_32K = 0x14
    AUDIO_44K = 0x15
    AUDIO_48K = 0x16
    AUDIO_96K = 0x17
    AUDIO_192K = 0x18
    ERROR = 0xFF


#: Частота дискретизации для аудио-фреймов.
AUDIO_SAMPLE_RATES: dict[int, int] = {
    FrameKind.AUDIO_8K: 8000,
    FrameKind.AUDIO_12K: 12000,
    FrameKind.AUDIO_16K: 16000,
    FrameKind.AUDIO_24K: 24000,
    FrameKind.AUDIO_32K: 32000,
    FrameKind.AUDIO_44K: 44100,
    FrameKind.AUDIO_48K: 48000,
    FrameKind.AUDIO_96K: 96000,
    FrameKind.AUDIO_192K: 192000,
}

AUDIO_KINDS = frozenset(AUDIO_SAMPLE_RATES)

_KIND_NAMES = {
    FrameKind.TERMINATE: "TERMINATE",
    FrameKind.UUID: "UUID",
    FrameKind.DTMF: "DTMF",
    FrameKind.AUDIO_8K: "AUDIO_8K",
    FrameKind.AUDIO_12K: "AUDIO_12K",
    FrameKind.AUDIO_16K: "AUDIO_16K",
    FrameKind.AUDIO_24K: "AUDIO_24K",
    FrameKind.AUDIO_32K: "AUDIO_32K",
    FrameKind.AUDIO_44K: "AUDIO_44K",
    FrameKind.AUDIO_48K: "AUDIO_48K",
    FrameKind.AUDIO_96K: "AUDIO_96K",
    FrameKind.AUDIO_192K: "AUDIO_192K",
    FrameKind.ERROR: "ERROR",
}


def kind_name(kind: int) -> str:
    name = _KIND_NAMES.get(kind)  # type: ignore[arg-type]
    return name if name is not None else f"UNKNOWN_0x{int(kind):02x}"


@dataclass(frozen=True, slots=True)
class Frame:
    """Один фрейм AudioSocket."""

    kind: int
    payload: bytes = b""

    @property
    def kind_name(self) -> str:
        return kind_name(self.kind)

    @property
    def sample_rate(self) -> int | None:
        return AUDIO_SAMPLE_RATES.get(self.kind)

    def as_uuid(self) -> uuid.UUID | None:
        """UUID канала из фрейма 0x01 (None, если payload не 16 байт)."""
        if self.kind != FrameKind.UUID or len(self.payload) != 16:
            return None
        return uuid.UUID(bytes=self.payload)

    def as_dtmf(self) -> str | None:
        """DTMF-цифра из фрейма 0x03 (None, если payload некорректен)."""
        if self.kind != FrameKind.DTMF or len(self.payload) != 1:
            return None
        try:
            digit = self.payload.decode("ascii").upper()
        except UnicodeDecodeError:
            return None
        return digit if digit in DTMF_ALPHABET else None


def encode_frame(kind: int, payload: bytes = b"") -> bytes:
    """Собрать фрейм AudioSocket."""
    if not 0 <= int(kind) <= 0xFF:
        raise ValueError(f"тип фрейма вне диапазона: {kind!r}")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload слишком велик: {len(payload)} байт")
    return _HEADER.pack(int(kind), len(payload)) + payload


def encode_audio(pcm: bytes) -> bytes:
    """Фрейм с PCM 8 кГц, 16 бит, моно."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return encode_frame(FrameKind.AUDIO_8K, pcm)


def encode_dtmf(digit: str) -> bytes:
    """Фрейм DTMF (нужен в основном для тестов и сценариев отправки цифр)."""
    if len(digit) != 1 or digit.upper() not in DTMF_ALPHABET:
        raise ValueError(f"некорректная DTMF-цифра: {digit!r}")
    return encode_frame(FrameKind.DTMF, digit.upper().encode("ascii"))


def encode_terminate() -> bytes:
    return encode_frame(FrameKind.TERMINATE)


async def read_frame(
    reader: asyncio.StreamReader, *, timeout: float | None = None
) -> Frame | None:
    """Прочитать один фрейм.

    Возвращает None при штатном конце потока (EOF). Таймаут пробрасывается
    как asyncio.TimeoutError, неполный фрейм — как ConnectionError.
    """
    header = await _read_exactly(reader, HEADER_SIZE, timeout)
    if header is None:
        return None
    kind, length = _HEADER.unpack(header)
    payload = b""
    if length:
        chunk = await _read_exactly(reader, length, timeout)
        if chunk is None:
            raise ConnectionError("поток оборвался посреди фрейма AudioSocket")
        payload = chunk
    return Frame(kind=kind, payload=payload)


async def read_uuid_frame(reader: asyncio.StreamReader) -> uuid.UUID | None:
    """Дождаться первого осмысленного фрейма: UUID канала или завершение.

    Всё, что приходит до UUID (аудио, DTMF), игнорируется. None означает, что
    соединение закрыто или Asterisk сообщил об ошибке.
    """
    while True:
        frame = await read_frame(reader)
        if frame is None:
            return None
        if frame.kind == FrameKind.UUID:
            channel_uuid = frame.as_uuid()
            if channel_uuid is None:
                raise ConnectionError("фрейм UUID пришёл с некорректной длиной payload")
            return channel_uuid
        if frame.kind in {FrameKind.TERMINATE, FrameKind.ERROR}:
            return None


async def _read_exactly(
    reader: asyncio.StreamReader, size: int, timeout: float | None
) -> bytes | None:
    try:
        if timeout is not None:
            return await asyncio.wait_for(reader.readexactly(size), timeout)
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError:
        return None
    except asyncio.TimeoutError:
        raise
    except (ConnectionError, OSError):
        if timeout is None:
            return None
        raise




