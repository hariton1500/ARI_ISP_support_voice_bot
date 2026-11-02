"""Тесты работы с PCM: ресемплинг, WAV, проигрыватель, запись."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np

from app.audio.pcm import (
    FRAME_BYTES,
    SAMPLE_RATE,
    PcmStreamResampler,
    chunk_pcm,
    duration_ms,
    load_prompt,
    read_wav,
    resample,
    silence_ms,
    to_wav,
)
from app.audio.playback import AudioPlayer
from app.audio.recorder import WavRecorder
from tests.conftest import write_wav


def test_chunk_pcm_splits_and_keeps_data() -> None:
    pcm = bytes(range(10))
    chunks = list(chunk_pcm(pcm, 4))
    assert chunks == [pcm[:4], pcm[4:8], pcm[8:]]
    assert b"".join(chunks) == pcm
    with np.testing.assert_raises(ValueError):
        list(chunk_pcm(pcm, 0))


def test_silence_and_duration() -> None:
    pcm = silence_ms(100)
    assert len(pcm) == FRAME_BYTES * 5
    assert duration_ms(pcm) == 100.0
    assert pcm == b"\x00" * len(pcm)


def test_resample_16k_to_8k_halves_length() -> None:
    source = (np.zeros(1600, dtype="<i2")).tobytes()  # 100 мс при 16 кГц
    result = resample(source, 16000, 8000)
    assert abs(len(result) - 1600) <= 2
    assert resample(source, 16000, 16000) is source
    assert resample(b"", 8000, 16000) == b""


def test_read_wav_converts_stereo_and_rate(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "stereo.wav", duration_ms=100, rate=16000, channels=2)
    pcm = read_wav(path)
    assert abs(len(pcm) - 8000 * 2 // 10) <= 4  # 100 мс при 8 кГц


def test_load_prompt_supports_raw_sln(tmp_path: Path) -> None:
    path = tmp_path / "prompt.sln"
    path.write_bytes(b"\x01\x00" * 800)
    assert load_prompt(path) == b"\x01\x00" * 800


def test_load_prompt_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "prompt.mp3"
    path.write_bytes(b"")
    with np.testing.assert_raises(ValueError):
        load_prompt(path)


def test_recorder_writes_playable_wav(tmp_path: Path) -> None:
    path = tmp_path / "rec" / "test.wav"
    pcm = (np.ones(800, dtype="<i2") * 100).tobytes()  # 100 мс при 8 кГц
    with WavRecorder(path) as recorder:
        assert recorder.write(pcm) == len(pcm)
        assert recorder.write(b"\x01") == 0  # неполный сэмпл отбрасывается
    assert recorder.bytes_written == len(pcm)
    assert abs(recorder.duration_ms - 100.0) < 0.001

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnframes() == 800


def test_player_sends_paced_chunks_and_stops() -> None:
    received: list[bytes] = []

    async def write(chunk: bytes) -> None:
        received.append(chunk)

    async def scenario() -> bool:
        player = AudioPlayer(write)
        pcm = b"\x00" * (FRAME_BYTES * 3)  # ровно 3 фрейма по 20 мс
        return await player.play(pcm)

    assert asyncio.run(scenario()) is True
    assert len(received) == 3
    assert all(len(chunk) == FRAME_BYTES for chunk in received)


def test_player_stop_interrupts_playback() -> None:
    async def scenario() -> tuple[bool, int]:
        received: list[bytes] = []

        async def write(chunk: bytes) -> None:
            received.append(chunk)

        player = AudioPlayer(write)
        pcm = b"\x00" * (FRAME_BYTES * 100)
        task = asyncio.create_task(player.play(pcm))
        await asyncio.sleep(0.02)
        player.stop()
        completed = await asyncio.wait_for(task, timeout=1.0)
        return completed, len(received)

    completed, chunks = asyncio.run(scenario())
    assert completed is False
    assert chunks < 100


def test_pcm_stream_resampler_handles_arbitrary_chunks() -> None:
    source = (np.ones(2400, dtype="<i2") * 7).tobytes()  # 100 мс при 24 кГц
    resampler = PcmStreamResampler(24000, 8000)
    chunks: list[bytes] = []
    for offset in range(0, len(source), 333):
        produced = resampler.feed(source[offset : offset + 333])
        assert all(len(chunk) == FRAME_BYTES for chunk in produced)
        chunks.extend(produced)
    output = b"".join(chunks) + resampler.flush()
    assert abs(len(output) - 1600) <= 4
    samples = np.frombuffer(output, dtype="<i2")
    assert samples.size > 0
    # У IIR/FIR-фильтра есть небольшой переходный процесс на краях сигнала.
    steady = samples[20:-20]
    assert steady.max() - steady.min() <= 2


def test_pcm_stream_resampler_matches_one_shot_with_irregular_chunks() -> None:
    time = np.arange(2400) / 24000
    source_samples = (
        9000 * np.sin(2 * np.pi * 440 * time)
        + 2500 * np.sin(2 * np.pi * 2500 * time)
    ).astype("<i2")
    source = source_samples.tobytes()
    expected = np.frombuffer(resample(source, 24000, 8000), dtype="<i2")

    resampler = PcmStreamResampler(24000, 8000)
    chunks: list[bytes] = []
    for offset in range(0, len(source), 353):
        chunks.extend(resampler.feed(source[offset : offset + 353]))
    chunks.append(resampler.flush())
    actual = np.frombuffer(b"".join(chunks), dtype="<i2")

    assert actual.size == expected.size
    # Streaming и one-shot режимы SoXR могут различаться только округлением PCM16.
    np.testing.assert_allclose(actual, expected, rtol=0, atol=2)


def test_resample_suppresses_aliasing() -> None:
    # 7 кГц выше частоты Найквиста для 8 кГц: без anti-alias-фильтра
    # такой тон заворачивается в слышимый 1 кГц и даёт металлический призвук.
    time = np.arange(2400) / 24000
    source = (12000 * np.sin(2 * np.pi * 7000 * time)).astype("<i2").tobytes()
    result = resample(source, 24000, 8000)
    samples = np.frombuffer(result, dtype="<i2").astype(np.float64)
    rms = np.sqrt(np.mean(samples**2))
    assert rms < 100  # более чем на 40 дБ ниже исходного тона


def test_to_wav_roundtrip() -> None:
    pcm = (np.ones(800, dtype="<i2") * 100).tobytes()
    data = to_wav(pcm)
    assert data.startswith(b"RIFF")
    assert read_wav_bytes(data) == pcm


def read_wav_bytes(data: bytes) -> bytes:
    import io
    import wave

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == 1
        return wav.readframes(wav.getnframes())


def test_player_plays_async_stream() -> None:
    received: list[bytes] = []

    async def write(chunk: bytes) -> None:
        received.append(chunk)

    async def chunks():
        yield b"\x00" * FRAME_BYTES
        yield b"\x00" * FRAME_BYTES

    async def scenario() -> bool:
        player = AudioPlayer(write)
        return await player.play_stream(chunks())

    assert asyncio.run(scenario()) is True
    assert received == [b"\x00" * FRAME_BYTES, b"\x00" * FRAME_BYTES]
