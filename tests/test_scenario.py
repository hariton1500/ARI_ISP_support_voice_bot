"""Тесты модели сценария."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.dialog.scenario import Scenario, ScenarioError

DEFAULT_SCENARIO = PROJECT_ROOT / "scenarios" / "default.json"
LLM_FIRST_SCENARIO = PROJECT_ROOT / "scenarios" / "llm-first.json"


def _raw(**steps: dict) -> dict:
    return {"name": "t", "initial": next(iter(steps)), "steps": steps}


def test_default_scenario_is_live_dialog() -> None:
    scenario = Scenario.load(DEFAULT_SCENARIO)
    assert scenario.name == "default"
    assert scenario.initial == "greeting"
    assert scenario.steps["greeting"].next == "dialog"
    assert scenario.steps["dialog"].llm is True
    assert scenario.steps["dialog"].voice["оператор"] == "operator"
    assert scenario.steps["operator"].hangup is True
    assert "menu" not in scenario.steps
    assert "dialog.wav" in scenario.prompt_names
    assert len(scenario.prompt_names) == len(set(scenario.prompt_names))


def test_llm_first_scenario_is_valid() -> None:
    scenario = Scenario.load(LLM_FIRST_SCENARIO)
    assert scenario.name == "llm-first"
    assert scenario.initial == "greeting"
    assert "menu" not in scenario.steps
    assert scenario.steps["greeting"].next == "dialog"

    dialog = scenario.steps["dialog"]
    assert dialog.llm is True
    assert dialog.voice["оператор"] == "operator"
    assert dialog.on_timeout == "no_input"
    assert scenario.steps["no_input"].next == "fallback"
    assert scenario.steps["operator"].hangup is True
    assert "dialog.wav" in scenario.prompt_names


def test_live_scenarios_do_not_use_static_billing_steps() -> None:
    for path in (DEFAULT_SCENARIO, LLM_FIRST_SCENARIO):
        scenario = Scenario.load(path)
        assert all(step.billing is None for step in scenario.steps.values())
        assert all(not hasattr(step, "dtmf") for step in scenario.steps.values())


def test_billing_step_is_parsed() -> None:
    scenario = Scenario.from_dict(
        _raw(
            a={
                "prompt": "a.wav",
                "prompt_not_found": "nf.wav",
                "prompt_error": "err.wav",
                "billing": "caller",
                "next": "a",
            }
        )
    )
    step = scenario.steps["a"]
    assert step.billing == "caller"
    assert step.prompt_not_found == "nf.wav"
    assert step.prompt_error == "err.wav"
    assert {"a.wav", "nf.wav", "err.wav"} <= set(scenario.prompt_names)


def test_billing_prompts_require_billing() -> None:
    with pytest.raises(ScenarioError, match="billing"):
        Scenario.from_dict(_raw(a={"prompt": "a.wav", "prompt_not_found": "nf.wav"}))


def test_step_defaults() -> None:
    scenario = Scenario.from_dict(_raw(step={"prompt": "a.wav"}))
    step = scenario.steps["step"]
    assert step.timeout_ms == 8000
    assert step.max_attempts == 2
    assert step.llm is False
    assert step.next is None
    assert step.hangup is False


def test_initial_defaults_to_first_step() -> None:
    scenario = Scenario.from_dict({"steps": {"a": {}, "b": {}}})
    assert scenario.initial == "a"


def test_unknown_transition_is_rejected() -> None:
    with pytest.raises(ScenarioError, match="next"):
        Scenario.from_dict(_raw(a={"next": "missing"}))


def test_dtmf_field_is_rejected() -> None:
    with pytest.raises(ScenarioError, match="dtmf"):
        Scenario.from_dict(_raw(a={"dtmf": {"1": "a"}}))


def test_bad_max_attempts_is_rejected() -> None:
    with pytest.raises(ScenarioError, match="max_attempts"):
        Scenario.from_dict(_raw(a={"llm": True, "max_attempts": 0}))


def test_negative_timeout_is_rejected() -> None:
    with pytest.raises(ScenarioError, match="timeout_ms"):
        Scenario.from_dict(_raw(a={"timeout_ms": -1}))


def test_empty_steps_rejected() -> None:
    with pytest.raises(ScenarioError):
        Scenario.from_dict({"initial": "a", "steps": {}})


def test_links_are_validated_after_parsing() -> None:
    raw = _raw(a={"voice": {"оператор": "b"}, "on_timeout": "x"}, b={"next": "a"})
    with pytest.raises(ScenarioError) as exc_info:
        Scenario.from_dict(raw)
    assert "on_timeout" in str(exc_info.value)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="прочитать"):
        Scenario.load(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ScenarioError, match="JSON"):
        Scenario.load(path)


def test_steps_mapping_is_read_only() -> None:
    scenario = Scenario.load(DEFAULT_SCENARIO)
    with pytest.raises(TypeError):
        scenario.steps["new"] = scenario.steps["dialog"]  # type: ignore[index]


def test_llm_step_is_parsed() -> None:
    scenario = Scenario.from_dict(
        _raw(
            dialog={"prompt": "dialog.wav", "llm": True},
            operator={"prompt": "operator.wav", "hangup": True},
        )
    )
    assert scenario.steps["dialog"].llm is True


def test_voice_commands_and_text_are_parsed() -> None:
    scenario = Scenario.from_dict(
        _raw(
            dialog={
                "text": "Чем я могу помочь?",
                "llm": True,
                "voice": {"оператор": "operator", "соедините с оператором": "operator"},
            },
            operator={"prompt": "operator.wav", "hangup": True},
        )
    )
    step = scenario.steps["dialog"]
    assert step.text == "Чем я могу помочь?"
    assert step.voice["оператор"] == "operator"
    assert "operator.wav" in scenario.prompt_names


def test_voice_target_is_validated() -> None:
    with pytest.raises(ScenarioError, match="voice"):
        Scenario.from_dict(_raw(a={"voice": {"оператор": "missing"}}))


def test_voice_must_be_object() -> None:
    with pytest.raises(ScenarioError, match="voice"):
        Scenario.from_dict(_raw(a={"voice": ["оператор"]}))
