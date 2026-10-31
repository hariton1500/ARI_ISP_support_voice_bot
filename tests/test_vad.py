"""Тесты VAD, эндпоинтинга и потокового сборщика реплик."""

from __future__ import annotations

import numpy as np

from app.audio.pcm import FRAME_BYTES, FRAME_MS, SAMPLE_RATE
from app.speech.streaming import StreamingVoiceInput
from app.speech.vad import VadConfig, VadEventKind, VoiceActivityDetector, rms


def _tone_frame(amplitude: float = 0.8, freq: int = 440) -> bytes:
    count = SAMPLE_RATE * FRAME_MS // 1000
    time = np.arange(count) / SAMPLE_RATE
    samples = (np.sin(2 * np.pi * freq * time) * amplitude * 32767).astype("<i2")
    return samples.tobytes()


def _silence_frame() -> bytes:
    return b"\x00" * FRAME_BYTES


def test_rms_of_silence_and_tone() -> None:
    assert rms(_silence_frame()) == 0.0
    assert rms(_tone_frame(0.5)) > 5000


def test_vad_detects_start_and_end_with_silence() -> None:
    vad = VoiceActivityDetector(
        VadConfig(start_ms=60, silence_ms=100, min_speech_ms=60, rms_threshold=300)
    )
    assert vad.feed(_silence_frame()) is None
    assert vad.feed(_tone_frame()) is None
    assert vad.feed(_tone_frame()) is None
    event = vad.feed(_tone_frame())
    assert event is not None and event.kind is VadEventKind.START
    assert vad.is_speaking is True

    for _ in range(4):
        assert vad.feed(_silence_frame()) is None
    end = vad.feed(_silence_frame())
    assert end is not None and end.kind is VadEventKind.END
    assert end.duration_ms >= 100
    assert vad.is_speaking is False


def test_vad_detects_moderate_speech_without_raising_threshold() -> None:
    vad = VoiceActivityDetector(
        VadConfig(start_ms=200, silence_ms=100, min_speech_ms=0, rms_threshold=300)
    )
    # RMS ≈ 1150: заметно выше порога, но в 10 раз тише тестового «крика».
    quiet_speech = _tone_frame(amplitude=0.05)
    event = None
    for _ in range(10):
        event = vad.feed(quiet_speech)
        if event is not None:
            break
    assert event is not None and event.kind is VadEventKind.START
    assert vad.threshold == 300.0


def test_vad_force_ends_too_long_utterance() -> None:
    vad = VoiceActivityDetector(
        VadConfig(
            start_ms=20,
            silence_ms=1000,
            min_speech_ms=0,
            max_speech_ms=80,
            rms_threshold=300,
        )
    )
    first = vad.feed(_tone_frame())
    assert first is not None and first.kind is VadEventKind.START
    events = [vad.feed(_tone_frame()) for _ in range(4)]
    assert any(event is not None and event.kind is VadEventKind.END for event in events)


def test_streaming_voice_keeps_preroll_and_full_utterance() -> None:
    voice = StreamingVoiceInput(
        VadConfig(start_ms=60, silence_ms=100, min_speech_ms=40, rms_threshold=300),
        partial_interval_ms=40,
    )
    now = 100.0
    for _ in range(3):
        assert voice.feed(_silence_frame(), now=now) is None
        now += 0.02

    start = None
    for _ in range(3):
        start = voice.feed(_tone_frame(), now=now)
        now += 0.02
    assert start is not None and start.kind is VadEventKind.START
    assert voice.is_speaking
    assert len(voice.utterance) >= FRAME_BYTES * 3

    assert voice.partial_due(now=now + 1.0) is True
    partial = voice.partial_audio()
    assert partial == voice.utterance
    voice.mark_partial(now=now)

    end = None
    for _ in range(5):
        end = voice.feed(_silence_frame(), now=now)
        now += 0.02
    assert end is not None and end.kind is VadEventKind.END
    assert end.pcm
    assert end.duration_ms >= 100
    assert not voice.is_speaking
    assert voice.utterance == b""


def test_streaming_voice_buffers_partial_frames() -> None:
    voice = StreamingVoiceInput(VadConfig(start_ms=40, silence_ms=100))
    # Половина кадра не должна попасть в VAD и потеряться при следующем вызове.
    half = FRAME_BYTES // 2
    voice.feed(_silence_frame()[:half])
    voice.feed(_silence_frame()[half:])
    assert voice.feed(_tone_frame()) is None
