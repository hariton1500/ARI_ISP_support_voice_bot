"""Клиент биллинга Евпанет (`BILLING_BASE_URL` из .env).

Контракт проверен на реальном сервисе:

    POST {BILLING_BASE_URL}
    Authorization: Bearer {BILLING_TOKEN}
    Content-Type: application/json
    {"userId": "<лицевой счёт или номер абонента>"}

Успех (HTTP 200):

    {"found": true, "user": {"id": "1", "extra_account": 227.23,
     "packet_name": "Интернет 100 Мбит", "days_left": 12,
     "network_status": {"switch_state": "green", ...}, ...}}

    {"found": false, "user": null}

Ошибка валидации (HTTP 400): {"found": false, "error": "missing_user_id"}

`extra_account` — баланс в рублях, `packet_name` — тариф, `days_left` — сколько
дней осталось, `network_status.switch_state` — состояние сети абонента.
`BILLING_MOCK=1` отдаёт те же данные без обращения к сети.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

import aiohttp

from app.config import Settings


class BillingError(RuntimeError):
    """Биллинг недоступен или вернул некорректный ответ."""


@dataclass(frozen=True, slots=True)
class Subscriber:
    """Абонент из биллинга (только то, что нужно боту)."""

    user_id: str
    balance: float | None = None
    packet_name: str | None = None
    packet_sum: float | None = None
    packet_seconds: int | None = None
    days_left: int | None = None
    network_state: str | None = None
    network_reason: str | None = None
    in_failure_zone: bool = False
    active_repair: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Короткая строка для логов: «баланс 227.23 ₽, тариф «…», осталось 12 дн.»."""
        parts: list[str] = []
        if self.balance is not None:
            parts.append(f"баланс {self.balance:.2f} руб.")
        if self.packet_name:
            parts.append(f"тариф «{self.packet_name}»")
        if self.packet_sum is not None:
            parts.append(f"абонплата {self.packet_sum:.2f} руб.")
        if self.days_left is not None:
            parts.append(f"осталось {self.days_left} дн.")
        if self.network_state:
            parts.append(f"сеть {self.network_state}")
        if self.in_failure_zone:
            parts.append("аварийная зона")
        if self.active_repair:
            parts.append("есть заявка на ремонт")
        return ", ".join(parts) or "нет данных"

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "balance": self.balance,
            "packet_name": self.packet_name,
            "packet_sum": self.packet_sum,
            "packet_seconds": self.packet_seconds,
            "days_left": self.days_left,
            "network_state": self.network_state,
            "network_reason": self.network_reason,
            "in_failure_zone": self.in_failure_zone,
            "active_repair": self.active_repair,
            "raw": self.raw,
        }


class BillingLookup(Protocol):
    """Минимальный интерфейс, нужный диалогу (удобно подменять в тестах)."""

    async def fetch_subscriber(self, user_id: str) -> Subscriber | None: ...


class BillingClient:
    """Тонкая обёртка над REST биллинга."""

    def __init__(
        self,
        settings: Settings,
        *,
        logger: logging.Logger | None = None,
        timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._url = (settings.billing_base_url or "").strip()
        self._token = (settings.billing_token or "").strip()
        self._timeout = timeout if timeout is not None else settings.billing_timeout
        self._log = logger or logging.getLogger("app.billing")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        """Биллинг можно использовать (заданы адрес и токен либо включён MOCK)."""
        return bool(self._url and self._token) or self._settings.billing_mock

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self._timeout, connect=min(self._timeout, 5.0)),
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def __aenter__(self) -> BillingClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def fetch_subscriber(self, user_id: str) -> Subscriber | None:
        """Найти абонента по `userId`.

        Возвращает `None`, если абонент не найден, и бросает `BillingError`,
        если сервис недоступен или ответ некорректный.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            raise BillingError("пустой userId")

        if self._settings.billing_mock:
            self._log.info("биллинг: BILLING_MOCK=1, тестовые данные для %r", user_id)
            return _parse_subscriber(_mock_payload(user_id))

        if not self._url or not self._token:
            raise BillingError("биллинг не настроен: нужны BILLING_BASE_URL и BILLING_TOKEN")

        await self.start()
        assert self._session is not None
        try:
            async with self._session.post(self._url, json={"userId": user_id}) as response:
                status = response.status
                text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BillingError(f"нет связи с биллингом: {exc}") from exc

        if status == 400:
            raise BillingError(f"биллинг отклонил запрос: {_error_text(text)}")
        if status >= 400:
            raise BillingError(f"биллинг вернул HTTP {status}: {_error_text(text)}")

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise BillingError(f"биллинг вернул не-JSON: {text[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise BillingError(f"неожиданный ответ биллинга: {payload!r}")
        if not payload.get("found"):
            return None
        return _parse_subscriber(payload)


def _parse_subscriber(payload: Mapping[str, Any]) -> Subscriber:
    user = payload.get("user")
    if not isinstance(user, Mapping):
        raise BillingError(f"в ответе нет объекта user: {dict(payload)!r}")
    network = user.get("network_status")
    network = network if isinstance(network, Mapping) else {}
    return Subscriber(
        user_id=_as_text(user.get("id")) or "",
        balance=_as_float(user.get("extra_account")),
        packet_name=_as_text(user.get("packet_name")),
        packet_sum=_as_float(user.get("packet_sum")),
        packet_seconds=_as_int(user.get("packet_secs")),
        days_left=_as_int(user.get("days_left")),
        network_state=_as_text(network.get("switch_state")),
        network_reason=_as_text(network.get("reason")),
        in_failure_zone=bool(network.get("is_in_network_failure_zone")),
        active_repair=bool(user.get("active_repair")),
        raw=dict(user),
    )


def _mock_payload(user_id: str) -> dict[str, Any]:
    """Ответ реального сервиса для абонента id=1 — для BILLING_MOCK=1 и тестов."""
    return {
        "found": True,
        "user": {
            "id": user_id,
            "extra_account": 227.23,
            "packet_id": "242",
            "packet_name": "Интернет 100 Мбит",
            "packet_secs": 1065600,
            "packet_sum": 200,
            "days_left": 12,
            "network_status": {
                "switch_state": "green",
                "is_in_network_failure_zone": False,
                "reason": "Абонент привязан к активному SNMP-коммутатору, связь с ним есть.",
            },
            "active_repair": False,
        },
    }


def _error_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return text.strip()[:200] or "без тела"
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("message") or payload)[:200]
    return str(payload)[:200]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None
