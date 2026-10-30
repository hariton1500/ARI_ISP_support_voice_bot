"""Загрузка промптов сценария в PCM-формате AudioSocket."""

from __future__ import annotations

import logging
from pathlib import Path

from app.audio.pcm import load_prompt
from app.dialog.scenario import Scenario

#: Промпты в PCM: имя из сценария -> байты 8 кГц, 16 бит, моно.
PromptMap = dict[str, bytes]


def load_prompts(
    scenario: Scenario,
    prompts_dir: str | Path,
    logger: logging.Logger | None = None,
) -> PromptMap:
    """Прочитать все промпты сценария.

    Отсутствующие или битые файлы не роняют бота: шаг просто прозвучит
    «в тишину», а в лог уйдёт предупреждение.
    """
    log = logger or logging.getLogger("app.dialog.prompts")
    base = Path(prompts_dir).resolve()
    loaded: PromptMap = {}
    names = scenario.prompt_names
    if not names:
        return loaded

    if not base.is_dir():
        log.warning("каталог промптов не найден: %s", base)
        return loaded

    for name in names:
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            log.error("промпт %r указывает за пределы каталога промптов — пропущен", name)
            continue
        if not path.is_file():
            log.warning("промпт не найден: %s (шаг прозвучит без звука)", path)
            continue
        try:
            pcm = load_prompt(path)
        except (OSError, ValueError) as exc:
            log.error("не удалось загрузить промпт %s: %s", path, exc)
            continue
        if not pcm:
            log.warning("промпт пустой: %s", path)
            continue
        loaded[name] = pcm

    log.info("промпты: загружено %d из %d", len(loaded), len(names))
    return loaded
