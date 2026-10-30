"""Тесты бенчмарка TTS: медиана, сохранение 8 кГц WAV, замер TTFB/RTF."""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

from app.audio.pcm import SAMPLE_RATE
from tools.tts_benchmark import (
    Engine,
    EngineResult,
    Measurement,
    _median,
    _save_audio,
    measure,
)


def _pcm(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    samples = np.zeros(int(rate * seconds), dtype="<i2")
    return samples.tobytes()


class FakeEngine(Engine):
    """Движок-заглушка: «синтезирует» 0,5 с тишины за 10 мс."""

    name = "fake"
    voice_name = "silence"

    def __init__(self) -> None:
        self.calls = 0

    def load(self) -> None:
        self.voice_name = "silence"

    def synthesize(self, text: str) -> EngineResult:
        self.calls += 1
        time.sleep(0.01)
        return EngineResult(
            pcm=_pcm(0.5),
            sample_rate=SAMPLE_RATE,
            first_chunk_at=time.perf_counter(),
        )


def test_median_ignores_errors_and_outliers() -> None:
    base = Measurement(
        engine="fake",
        voice="v",
        phrase="p",
        chars=10,
        load_ms=5.0,
        ttfb_ms=10.0,
        total_ms=20.0,
        audio_ms=100.0,
        rtf=0.2,
        sample_rate=SAMPLE_RATE,
    )
    slow = Measurement(**{**base.__dict__, "ttfb_ms": 100.0})
    middle = Measurement(**{**base.__dict__, "ttfb_ms": 20.0})
    failed = Measurement(
        **{
            **base.__dict__,
            "ttfb_ms": 0.0,
            "total_ms": 0.0,
            "audio_ms": None,
            "rtf": None,
            "error": "boom",
        }
    )

    result = _median([base, slow, middle, failed])
    assert result.ttfb_ms == 20.0
    assert result.error is None


def test_save_audio_resamples_to_8k(tmp_path: Path) -> None:
    source = EngineResult(pcm=_pcm(0.5, rate=22050), sample_rate=22050)
    path = _save_audio(tmp_path / "piper_0", source)

    assert path.suffix == ".wav"
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        # 0,5 с при 8 кГц — 4000 сэмплов (допуск на округление ресемплера).
        assert abs(wav.getnframes() - 4000) <= 2


def test_save_audio_keeps_mp3_container(tmp_path: Path) -> None:
    path = _save_audio(
        tmp_path / "edge_0",
        EngineResult(pcm=b"ID3fake", sample_rate=24000, container="mp3"),
    )
    assert path.suffix == ".mp3"
    assert path.read_bytes() == b"ID3fake"


def test_measure_reports_ttfb_and_rtf(tmp_path: Path) -> None:
    engine = FakeEngine()
    results, load_ms = measure(
        engine,
        [("short", "раз"), ("two", "два")],
        tmp_path,
        repeat=2,
        warmup=True,
        log=lambda _message: None,
    )

    assert engine.calls == 5  # прогрев + две фразы по два прогона
    assert load_ms >= 0
    assert [item.phrase for item in results] == ["short", "two"]
    for item in results:
        assert item.error is None
        assert item.audio_ms == 500.0
        assert item.ttfb_ms >= 10.0
        assert item.rtf is not None and item.rtf < 0.1
        assert item.audio_file is not None
        assert Path(item.audio_file).is_file()
