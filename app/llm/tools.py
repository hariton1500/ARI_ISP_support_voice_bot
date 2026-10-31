"""Инструменты (function calling) голосового бота.

`get_billing_info` — единственный источник баланса, тарифа и состояния сети.
`transfer_to_operator` и `end_call` управляют завершением разговора; решение
принимает модель, а сессия лишь исполняет его после озвучки ответа.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.billing.client import BillingError, BillingLookup, Subscriber

__all__ = ["CallControl", "VoiceTools"]

_ACCOUNT_INVALID = re.compile(r"[\s\-()+]+")

#: Поля абонента, которые разрешено передавать модели. Технические
#: идентификаторы (packet_id, access_hwid, snmp_*) намеренно не попадают в LLM.
_USER_FIELDS = frozenset(
    {
        "id",
        "extra_account",
        "packet_name",
        "packet_sum",
        "days_left",
        "auto_activation_enabled",
        "network_status",
        "active_repair",
        "active_repair_comment",
        "active_repair_date",
    }
)


@dataclass(slots=True)
class CallControl:
    """Состояние управления вызовом, которое меняют инструменты."""

    end_call: bool = False
    transfer: bool = False


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


class VoiceTools:
    """Исполнитель инструментов одного разговора."""

    def __init__(
        self,
        billing: BillingLookup | None,
        *,
        logger: logging.Logger | None = None,
        operator_number: str = "508",
    ) -> None:
        self._billing = billing
        self._log = logger or logging.getLogger("app.llm.tools")
        self.operator_number = operator_number

    def schemas(self) -> list[dict[str, Any]]:
        """Описания функций в формате OpenAI tools."""
        return [
            _tool(
                "get_billing_info",
                "Получить данные лицевого счёта: баланс, тариф, абонплату, "
                "остаток дней, состояние сети и активный ремонт. Вызывать только "
                "после того, как абонент назвал номер лицевого счёта.",
                {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "Номер лицевого счёта, только цифры",
                        }
                    },
                    "required": ["user_id"],
                    "additionalProperties": False,
                },
            ),
            _tool(
                "transfer_to_operator",
                "Перевести абонента на живого оператора. Использовать, когда "
                "абонент просит оператора или вопрос выходит за рамки инструкций.",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            _tool(
                "end_call",
                "Завершить звонок. Вызывать только после прощания с абонентом.",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        ]

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | str | None,
        control: CallControl,
    ) -> str:
        """Выполнить инструмент и вернуть JSON-строку для role=tool."""
        args = self._arguments(arguments)
        if name == "get_billing_info":
            result = await self._get_billing_info(args.get("user_id"))
        elif name == "transfer_to_operator":
            control.transfer = True
            result = {"status": "transferring", "number": self.operator_number}
        elif name == "end_call":
            control.end_call = True
            result = {"status": "ending"}
        else:
            self._log.warning("LLM вызвала неизвестный инструмент %r", name)
            result = {"error": "unknown_tool", "tool": name}
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _arguments(arguments: Mapping[str, Any] | str | None) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, Mapping):
            return dict(arguments)
        if not isinstance(arguments, str):
            return {}
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    async def _get_billing_info(self, raw_user_id: Any) -> dict[str, Any]:
        user_id = self._normalize_user_id(raw_user_id)
        if user_id is None:
            return {"found": False, "error": "invalid_user_id"}

        billing = self._billing
        if billing is None:
            return {"found": False, "error": "billing_not_configured"}
        try:
            subscriber = await billing.fetch_subscriber(user_id)
        except BillingError as exc:
            self._log.warning("LLM: биллинг недоступен для %r: %s", user_id, exc)
            return {"found": False, "error": "billing_unavailable"}
        except Exception:  # noqa: BLE001 - ошибка инструмента не должна ронять вызов
            self._log.exception("LLM: неожиданная ошибка биллинга для %r", user_id)
            return {"found": False, "error": "billing_unavailable"}

        if subscriber is None:
            return {"found": False, "user": None}
        return {"found": True, "user": _subscriber_payload(subscriber)}

    @staticmethod
    def _normalize_user_id(raw: Any) -> str | None:
        if raw is None or isinstance(raw, bool):
            return None
        value = _ACCOUNT_INVALID.sub("", str(raw).strip())
        if not value or not value.isdigit() or len(value) > 24:
            return None
        return value


def _subscriber_payload(subscriber: Subscriber) -> dict[str, Any]:
    """Только разрешённые поля ответа биллинга, без служебных идентификаторов."""
    raw = subscriber.raw
    user: dict[str, Any] = {
        key: value for key, value in raw.items() if key in _USER_FIELDS
    }
    # Нормализованные значения надёжнее сырых (например, extra_account строкой).
    user["id"] = subscriber.user_id
    user["extra_account"] = subscriber.balance
    user["packet_name"] = subscriber.packet_name
    user["packet_sum"] = subscriber.packet_sum
    user["days_left"] = subscriber.days_left
    user["active_repair"] = subscriber.active_repair

    network: dict[str, Any] = {}
    raw_network = raw.get("network_status")
    if isinstance(raw_network, Mapping):
        for key in ("switch_state", "is_in_network_failure_zone", "reason"):
            if key in raw_network:
                network[key] = raw_network[key]
    network.setdefault("switch_state", subscriber.network_state)
    network.setdefault("is_in_network_failure_zone", subscriber.in_failure_zone)
    network.setdefault("reason", subscriber.network_reason)
    user["network_status"] = {
        key: value for key, value in network.items() if value is not None
    }

    return {key: value for key, value in user.items() if value is not None}
