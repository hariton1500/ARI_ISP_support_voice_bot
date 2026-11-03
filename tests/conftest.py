"""Общие фикстуры тестов: настройки, сценарий и WAV-промпты во временном каталоге."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.dialog.scenario import Scenario

SAMPLE_RATE = 8000


def write_wav(
    path: Path,
    *,
    duration_ms: int = 200,
    freq: int = 440,
    rate: int = SAMPLE_RATE,
    channels: int = 1,
) -> Path:
    """Сгенерировать тон заданной длительности в WAV."""
    length = int(rate * duration_ms / 1000)
    time = np.arange(length) / rate
    tone = (np.sin(2 * np.pi * freq * time) * 0.25 * 32767).astype("<i2")
    if channels > 1:
        tone = np.repeat(tone, channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(tone.tobytes())
    return path


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "prompts"
    for name in ("greeting", "menu", "balance", "operator", "invalid", "fallback"):
        write_wav(directory / f"{name}.wav")
    return directory


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    raw = {
        "name": "test",
        "initial": "greeting",
        "steps": {
            "greeting": {"prompt": "greeting.wav", "next": "menu"},
            "menu": {"prompt": "menu.wav", "next": "operator"},
            "operator": {"prompt": "operator.wav", "hangup": True},
            "fallback": {"prompt": "fallback.wav", "hangup": True},
        },
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return Scenario.load(path)


@pytest.fixture
def settings(tmp_path: Path, scenario: Scenario, prompts_dir: Path) -> Settings:
    return Settings(
        audiosocket_host="127.0.0.1",
        audiosocket_port=0,
        scenario_path=scenario.source or tmp_path / "scenario.json",
        prompts_dir=prompts_dir,
        record_calls=True,
        recordings_dir=tmp_path / "recordings",
        init_timeout=3.0,
        idle_timeout=5.0,
        max_call_seconds=30.0,
        log_level="WARNING",
    )
