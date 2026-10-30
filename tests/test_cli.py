"""Тесты командной строки."""

from __future__ import annotations

from pathlib import Path

from app.__main__ import main


def test_check_mode_validates_scenario_and_prompts(
    scenario, prompts_dir: Path, tmp_path: Path
) -> None:
    code = main(
        [
            "--check",
            "--scenario",
            str(scenario.source),
            "--prompts-dir",
            str(prompts_dir),
            "--recordings-dir",
            str(tmp_path / "recordings"),
            "--no-record",
        ]
    )
    assert code == 0


def test_check_mode_fails_on_missing_scenario(tmp_path: Path) -> None:
    assert main(["--check", "--scenario", str(tmp_path / "nope.json")]) == 2


def test_bad_settings_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUDIOSOCKET_PORT", "не число")
    assert main(["--check", "--scenario", str(tmp_path / "s.json")]) == 2
