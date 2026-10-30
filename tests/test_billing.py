"""Тесты клиента биллинга: контракт, ошибки, mock-режим."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest
from aiohttp import web

from app.billing.client import BillingClient, BillingError, Subscriber
from app.config import Settings

TOKEN = "test-token"


class FakeBilling:
    """Мини-сервер биллинга: отвечает так же, как реальный сервис."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], str]] = []
        self.status = 200
        self.body: Any = {"found": False, "user": None}
        self._socket: socket.socket | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/integration/subscriber/by-id"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/integration/subscriber/by-id", self._handle)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(64)
        self.port = int(self._socket.getsockname()[1])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.SockSite(self._runner, self._socket).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        text = await request.text()
        self.requests.append((request.path, dict(request.headers), text))
        if isinstance(self.body, str):
            return web.Response(status=self.status, text=self.body)
        return web.json_response(self.body, status=self.status)


def _settings(tmp_path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "audiosocket_host": "127.0.0.1",
        "audiosocket_port": 0,
        "scenario_path": tmp_path / "scenario.json",
        "prompts_dir": tmp_path / "prompts",
        "record_calls": False,
        "recordings_dir": tmp_path / "recordings",
        "init_timeout": 3.0,
        "idle_timeout": 5.0,
        "max_call_seconds": 10.0,
        "log_level": "WARNING",
        "billing_timeout": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


SUBSCRIBER_BODY = {
    "found": True,
    "user": {
        "id": "1",
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


def test_fetch_subscriber_found(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeBilling()
        await fake.start()
        fake.body = SUBSCRIBER_BODY
        settings = _settings(tmp_path, billing_base_url=fake.url, billing_token=TOKEN)
        try:
            async with BillingClient(settings) as client:
                subscriber = await client.fetch_subscriber("1")
        finally:
            await fake.stop()

        assert isinstance(subscriber, Subscriber)
        assert subscriber.user_id == "1"
        assert subscriber.balance == pytest.approx(227.23)
        assert subscriber.packet_name == "Интернет 100 Мбит"
        assert subscriber.days_left == 12
        assert subscriber.packet_seconds == 1065600
        assert subscriber.network_state == "green"
        assert subscriber.active_repair is False
        assert "227.23" in subscriber.summary

        path, headers, text = fake.requests[0]
        assert path == "/integration/subscriber/by-id"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(text) == {"userId": "1"}

    asyncio.run(scenario())


def test_fetch_subscriber_not_found(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeBilling()
        await fake.start()
        settings = _settings(tmp_path, billing_base_url=fake.url, billing_token=TOKEN)
        try:
            async with BillingClient(settings) as client:
                assert await client.fetch_subscriber("503") is None
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_fetch_subscriber_rejects_bad_request(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeBilling()
        await fake.start()
        fake.status = 400
        fake.body = {"found": False, "error": "missing_user_id"}
        settings = _settings(tmp_path, billing_base_url=fake.url, billing_token=TOKEN)
        try:
            async with BillingClient(settings) as client:
                with pytest.raises(BillingError, match="missing_user_id"):
                    await client.fetch_subscriber("1")
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_fetch_subscriber_server_error(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeBilling()
        await fake.start()
        fake.status = 503
        fake.body = "service unavailable"
        settings = _settings(tmp_path, billing_base_url=fake.url, billing_token=TOKEN)
        try:
            async with BillingClient(settings) as client:
                with pytest.raises(BillingError, match="503"):
                    await client.fetch_subscriber("1")
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_fetch_subscriber_bad_json(tmp_path) -> None:
    async def scenario() -> None:
        fake = FakeBilling()
        await fake.start()
        fake.body = "<html>ошибка</html>"
        settings = _settings(tmp_path, billing_base_url=fake.url, billing_token=TOKEN)
        try:
            async with BillingClient(settings) as client:
                with pytest.raises(BillingError, match="не-JSON"):
                    await client.fetch_subscriber("1")
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_fetch_subscriber_without_user_id(tmp_path) -> None:
    async def scenario() -> None:
        settings = _settings(
            tmp_path, billing_base_url="http://127.0.0.1:1/x", billing_token=TOKEN
        )
        async with BillingClient(settings) as client:
            with pytest.raises(BillingError, match="userId"):
                await client.fetch_subscriber("   ")

    asyncio.run(scenario())


def test_fetch_subscriber_requires_configuration(tmp_path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path)
        client = BillingClient(settings)
        assert client.enabled is False
        with pytest.raises(BillingError, match="не настроен"):
            await client.fetch_subscriber("1")

    asyncio.run(scenario())


def test_mock_mode_does_not_use_network(tmp_path) -> None:
    async def scenario() -> None:
        settings = _settings(
            tmp_path, billing_base_url="http://127.0.0.1:1/none", billing_mock=True
        )
        async with BillingClient(settings) as client:
            assert client.enabled is True
            subscriber = await client.fetch_subscriber("42")
        assert subscriber is not None
        assert subscriber.user_id == "42"
        assert subscriber.balance == pytest.approx(227.23)
        assert subscriber.network_state == "green"

    asyncio.run(scenario())
