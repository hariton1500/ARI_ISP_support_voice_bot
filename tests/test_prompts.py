"""Тесты загрузки промптов."""

from __future__ import annotations

import logging
from pathlib import Path

from app.dialog.prompts import load_prompts
from app.dialog.scenario import Scenario
from tests.conftest import write_wav


def _scenario(tmp_path: Path, *prompts: str) -> Scenario:
    steps = {f"s{i}": {"prompt": name, "next": f"s{i + 1}"} for i, name in enumerate(prompts)}
    steps[f"s{len(prompts)}"] = {"hangup": True}
    return Scenario.from_dict({"name": "t", "steps": steps}, source=tmp_path / "s.json")


def test_loads_existing_prompts(tmp_path: Path) -> None:
    directory = tmp_path / "prompts"
    write_wav(directory / "a.wav", duration_ms=100)
    prompts = load_prompts(_scenario(tmp_path, "a.wav"), directory)
    assert set(prompts) == {"a.wav"}
    assert len(prompts["a.wav"]) > 0


def test_missing_prompt_is_skipped_with_warning(tmp_path: Path, caplog) -> None:
    directory = tmp_path / "prompts"
    write_wav(directory / "a.wav", duration_ms=100)
    with caplog.at_level(logging.WARNING):
        prompts = load_prompts(_scenario(tmp_path, "a.wav", "b.wav"), directory)
    assert set(prompts) == {"a.wav"}
    assert "b.wav" in caplog.text


def test_missing_prompts_dir_returns_empty(tmp_path: Path) -> None:
    prompts = load_prompts(_scenario(tmp_path, "a.wav"), tmp_path / "absent")
    assert prompts == {}


def test_path_escape_is_rejected(tmp_path: Path, caplog) -> None:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (tmp_path / "secret.wav").write_bytes(b"\x00\x00")
    with caplog.at_level(logging.ERROR):
        prompts = load_prompts(_scenario(tmp_path, "../secret.wav"), directory)
    assert prompts == {}
    assert "за пределы" in caplog.text


def test_broken_prompt_does_not_break_loading(tmp_path: Path, caplog) -> None:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "bad.mp3").write_bytes(b"not audio")
    with caplog.at_level(logging.ERROR):
        prompts = load_prompts(_scenario(tmp_path, "bad.mp3"), directory)
    assert prompts == {}
    assert "bad.mp3" in caplog.text
