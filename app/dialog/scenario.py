"""Модель сценария диалога.

Сценарий — это JSON-файл с именованными шагами. Шаг умеет:
  * проиграть промпт (`prompt` — имя файла относительно PROMPTS_DIR);
  * озвучить `text` через AITUNNEL TTS, если WAV-промпта нет;
  * принять DTMF-цифру и перейти на другой шаг (`dtmf`);
  * принять распознанную фразу (`voice`: фраза -> шаг) или числительное;
  * вести свободный диалог через LLM (`llm`: true), если фраза не совпала
    с `voice`/DTMF;
  * перейти на шаг без ожидания ввода (`next`);
  * обработать отсутствие ввода (`on_timeout`) и неверный ввод (`on_invalid`);
  * завершить вызов (`hangup`);
  * запросить абонента в биллинге перед промптом (`billing`: `"caller"` или
    конкретный `userId`) и выбрать промпт по результату
    (`prompt_not_found` / `prompt_error`).

Логика обработки ввода:
  1. цифра есть в `dtmf` или фраза есть в `voice` — переход по карте;
  2. ввода нет за `timeout_ms` или он не распознан — счётчик попыток растёт,
     шаг повторяется (промпт играется заново);
  3. после `max_attempts` попыток — переход на `on_invalid` (неверный ввод)
     или `on_timeout` (нет ввода); если нужного перехода нет, берётся
     `on_timeout`, а если нет и его — вызов завершается.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

DEFAULT_TIMEOUT_MS = 8000
DEFAULT_MAX_ATTEMPTS = 2
DTMF_ALPHABET = frozenset("0123456789*#ABCD")


class ScenarioError(ValueError):
    """Сценарий не прошёл валидацию."""


@dataclass(frozen=True, slots=True)
class Step:
    """Один шаг диалога."""

    name: str
    label: str
    prompt: str | None
    next: str | None
    dtmf: Mapping[str, str]
    on_timeout: str | None
    on_invalid: str | None
    timeout_ms: int
    max_attempts: int
    hangup: bool
    #: Запрос в биллинг перед промптом: "caller" (номер звонящего) или конкретный userId.
    billing: str | None = None
    #: Промпт, если абонент в биллинге не найден (по умолчанию — `prompt`).
    prompt_not_found: str | None = None
    #: Промпт, если биллинг недоступен (по умолчанию — `prompt`).
    prompt_error: str | None = None
    #: Текст для TTS, если WAV-промпта нет (AITUNNEL /audio/speech).
    text: str | None = None
    #: Голосовые команды: нормализованная фраза -> имя шага.
    voice: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: Свободный диалог через LLM после промпта. DTMF и `voice` имеют приоритет,
    #: остальные распознанные фразы уходят модели.
    llm: bool = False


@dataclass(frozen=True, slots=True)
class Scenario:
    """Набор шагов и точка входа."""

    name: str
    description: str
    initial: str
    steps: Mapping[str, Step]
    source: Path | None = None

    def __post_init__(self) -> None:
        if not self.steps:
            raise ScenarioError("сценарий не содержит шагов")
        if self.initial not in self.steps:
            raise ScenarioError(f"начальный шаг {self.initial!r} отсутствует в сценарии")

    @property
    def prompt_names(self) -> tuple[str, ...]:
        names: set[str] = set()
        for step in self.steps.values():
            for name in (step.prompt, step.prompt_not_found, step.prompt_error):
                if name:
                    names.add(name)
        return tuple(sorted(names))

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        """Загрузить и провалидировать сценарий из файла."""
        source = Path(path)
        try:
            raw_text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScenarioError(f"не удалось прочитать сценарий {source}: {exc}") from exc
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ScenarioError(f"некорректный JSON в {source}: {exc}") from exc
        return cls.from_dict(raw, source=source)

    @classmethod
    def from_dict(cls, raw: Any, *, source: Path | None = None) -> Scenario:
        """Собрать сценарий из уже разобранного JSON."""
        if not isinstance(raw, dict):
            raise ScenarioError("корень сценария должен быть JSON-объектом")

        name = _as_str(raw.get("name"), "сценарий", "name", default=source.stem if source else "scenario")
        description = _as_str(raw.get("description"), "сценарий", "description", default="")
        initial = _as_str(raw.get("initial"), "сценарий", "initial", default="")

        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, dict) or not raw_steps:
            raise ScenarioError("поле steps должно быть непустым объектом")

        steps: dict[str, Step] = {}
        for step_name, raw_step in raw_steps.items():
            if not isinstance(step_name, str) or not step_name:
                raise ScenarioError(f"некорректное имя шага: {step_name!r}")
            steps[step_name] = _parse_step(step_name, raw_step)

        scenario = cls(
            name=name,
            description=description,
            initial=initial or next(iter(steps)),
            steps=MappingProxyType(steps),
            source=source,
        )
        scenario._validate_links()
        return scenario

    def _validate_links(self) -> None:
        errors: list[str] = []
        for step in self.steps.values():
            targets: list[tuple[str, str]] = []
            if step.next:
                targets.append(("next", step.next))
            if step.on_timeout:
                targets.append(("on_timeout", step.on_timeout))
            if step.on_invalid:
                targets.append(("on_invalid", step.on_invalid))
            targets.extend((f"dtmf[{digit}]", target) for digit, target in step.dtmf.items())
            targets.extend(
                (f"voice[{phrase}]", target) for phrase, target in step.voice.items()
            )
            for field_name, target in targets:
                if target not in self.steps:
                    errors.append(
                        f"шаг {step.name!r}: {field_name} ссылается на {target!r}, которого нет"
                    )
        if errors:
            raise ScenarioError("; ".join(errors))


def _parse_step(step_name: str, raw: Any) -> Step:
    if not isinstance(raw, dict):
        raise ScenarioError(f"шаг {step_name!r} должен быть JSON-объектом")
    where = f"шаг {step_name!r}"

    prompt = raw.get("prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ScenarioError(f"шаг {step_name!r}: prompt должен быть непустой строкой")
        prompt = prompt.strip()

    raw_dtmf = raw.get("dtmf") or {}
    if not isinstance(raw_dtmf, dict):
        raise ScenarioError(f"шаг {step_name!r}: dtmf должен быть объектом")
    dtmf: dict[str, str] = {}
    for digit, target in raw_dtmf.items():
        if not isinstance(digit, str) or len(digit) != 1 or digit.upper() not in DTMF_ALPHABET:
            raise ScenarioError(f"шаг {step_name!r}: некорректная DTMF-цифра {digit!r}")
        if not isinstance(target, str) or not target.strip():
            raise ScenarioError(f"шаг {step_name!r}: для цифры {digit!r} не указан шаг перехода")
        dtmf[digit.upper()] = target.strip()

    timeout_ms = _as_int(raw.get("timeout_ms"), where, "timeout_ms", DEFAULT_TIMEOUT_MS)
    max_attempts = _as_int(raw.get("max_attempts"), where, "max_attempts", DEFAULT_MAX_ATTEMPTS)
    if timeout_ms < 0:
        raise ScenarioError(f"{where}: timeout_ms не может быть отрицательным")
    if max_attempts < 1:
        raise ScenarioError(f"{where}: max_attempts должен быть не меньше 1")

    billing = _as_optional_str(raw.get("billing"), where, "billing")
    prompt_not_found = _as_optional_str(raw.get("prompt_not_found"), where, "prompt_not_found")
    prompt_error = _as_optional_str(raw.get("prompt_error"), where, "prompt_error")
    if (prompt_not_found or prompt_error) and not billing:
        raise ScenarioError(
            f"{where}: prompt_not_found/prompt_error задаются только вместе с billing"
        )

    raw_voice = raw.get("voice") or {}
    if not isinstance(raw_voice, dict):
        raise ScenarioError(f"{where}: voice должен быть объектом")
    voice: dict[str, str] = {}
    for phrase, target in raw_voice.items():
        if not isinstance(phrase, str) or not phrase.strip():
            raise ScenarioError(f"{where}: голосовая фраза должна быть непустой строкой")
        if not isinstance(target, str) or not target.strip():
            raise ScenarioError(f"{where}: для фразы {phrase!r} не указан шаг перехода")
        voice[phrase.strip()] = target.strip()

    return Step(
        name=step_name,
        label=_as_str(raw.get("label"), where, "label", default=step_name),
        prompt=prompt,
        next=_as_optional_str(raw.get("next"), where, "next"),
        dtmf=MappingProxyType(dtmf),
        on_timeout=_as_optional_str(raw.get("on_timeout"), where, "on_timeout"),
        on_invalid=_as_optional_str(raw.get("on_invalid"), where, "on_invalid"),
        timeout_ms=timeout_ms,
        max_attempts=max_attempts,
        hangup=_as_bool(raw.get("hangup"), where, "hangup", False),
        billing=billing,
        prompt_not_found=prompt_not_found,
        prompt_error=prompt_error,
        text=_as_optional_str(raw.get("text"), where, "text"),
        voice=MappingProxyType(voice),
        llm=_as_bool(raw.get("llm"), where, "llm", False),
    )


def _as_str(value: Any, where: str, fieldname: str, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ScenarioError(f"{where}: {fieldname} должен быть строкой")
    return value


def _as_optional_str(value: Any, where: str, fieldname: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScenarioError(f"{where}: {fieldname} должен быть строкой")
    return value.strip() or None


def _as_int(value: Any, where: str, fieldname: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(f"{where}: {fieldname} должен быть целым числом")
    return value


def _as_bool(value: Any, where: str, fieldname: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ScenarioError(f"{where}: {fieldname} должен быть true/false")
    return value
