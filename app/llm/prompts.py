"""Загрузка markdown-промптов для LLM.

Основной системный промпт — `support_evp.md`. Рядом лежат справочные
материалы, которые добавляются в system-сообщение целиком:

* `billing_account_data.md` — как объяснять данные биллинга;
* `connection_terms.md` — условия нового подключения;
* `internet_private.md` — тарифы и адреса.

На этом этапе справочники передаются модели целиком. Отдельный RAG с поиском
по фрагментам появится позже, когда документов станет больше контекста модели.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "BASE_PROMPT",
    "KNOWLEDGE_PROMPTS",
    "LLMPromptError",
    "LLMPromptSet",
    "load_llm_prompts",
]

BASE_PROMPT = "support_evp.md"
KNOWLEDGE_PROMPTS: tuple[str, ...] = (
    "billing_account_data.md",
    "connection_terms.md",
    "internet_private.md",
)

#: Инструкции по инструментам добавляются после справочников, чтобы модель
#: видела точные имена функций и правила вызова.
TOOL_GUIDE = """
## Инструменты

Тебе доступны функции. Вызывай их только через механизм function calling,
не придумывай результат и не показывай абоненту JSON или имена полей.

1. `get_billing_info` — данные лицевого счёта. Параметр `user_id` — номер
   лицевого счёта, состоящий только из цифр. Вызывай после того, как абонент
   назвал номер. Если номер не назван, сначала попроси его.
2. `transfer_to_operator` — перевод на живого оператора. Вызывай по явной
   просьбе абонента либо когда помочь невозможно. Перед вызовом коротко скажи,
   что переводишь. После успешного `get_billing_info` сначала озвучь запрошенные
   данные: активный ремонт или другое состояние счёта — не причина автоматически
   вызывать перевод.
3. `end_call` — завершение разговора. Вызывай только после того, как
   попрощался с абонентом.

Если инструмент вернул ошибку, извинись, не выдумывай данные и предложи
оператора. Финансовые и технические сведения бери только из результата
`get_billing_info` в текущем звонке.
""".strip()

SYSTEM_TEMPLATE = """{base}

## Справочные материалы

{knowledge}

{tools}
"""


class LLMPromptError(RuntimeError):
    """Промпты LLM отсутствуют или не читаются."""


@dataclass(frozen=True, slots=True)
class LLMPromptSet:
    """Готовый system-промпт и исходные документы (для диагностики)."""

    system: str
    base: str
    knowledge: Mapping[str, str]
    sources: tuple[Path, ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.system)

    @property
    def document_count(self) -> int:
        return 1 + len(self.knowledge)

    @property
    def knowledge_names(self) -> tuple[str, ...]:
        return tuple(self.knowledge)


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LLMPromptError(f"не удалось прочитать промпт {path}: {exc}") from exc
    if not text:
        raise LLMPromptError(f"промпт пустой: {path}")
    return text


def load_llm_prompts(
    directory: str | Path,
    logger: logging.Logger | None = None,
) -> LLMPromptSet:
    """Прочитать системный промпт и справочники из каталога."""
    log = logger or logging.getLogger("app.llm.prompts")
    base_dir = Path(directory).expanduser().resolve()
    if not base_dir.is_dir():
        raise LLMPromptError(f"каталог промптов LLM не найден: {base_dir}")

    base_path = base_dir / BASE_PROMPT
    if not base_path.is_file():
        raise LLMPromptError(f"нет основного промпта LLM: {base_path}")
    base = _read(base_path)

    knowledge: dict[str, str] = {}
    sources = [base_path]
    for name in KNOWLEDGE_PROMPTS:
        path = base_dir / name
        if not path.is_file():
            log.warning("справочный промпт LLM не найден, пропущен: %s", path)
            continue
        knowledge[name] = _read(path)
        sources.append(path)

    if not knowledge:
        log.warning("нет справочных промптов LLM в %s — модель ответит только по основному", base_dir)

    sections = []
    for name, text in knowledge.items():
        sections.append(f"### {name}\n\n{text}")
    system = SYSTEM_TEMPLATE.format(
        base=base,
        knowledge="\n\n".join(sections) or "Справочные материалы не загружены.",
        tools=TOOL_GUIDE,
    ).strip()

    log.info(
        "промпты LLM: %d документ(ов), %d символов (%s)",
        1 + len(knowledge),
        len(system),
        base_dir,
    )
    return LLMPromptSet(
        system=system,
        base=base,
        knowledge=MappingProxyType(knowledge),
        sources=tuple(sources),
    )
