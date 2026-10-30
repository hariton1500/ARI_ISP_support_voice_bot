"""REST-клиент Asterisk ARI (Asterisk 22).

Все запросы — обычные HTTP-вызовы с Basic Auth на тот же адрес и порт, что и
WebSocket событий. Параметры передаются в query string, как требует ARI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from app.config import Settings


class AriError(RuntimeError):
    """ARI вернул ошибку или запрос не удался."""

    def __init__(self, method: str, path: str, status: int | None, detail: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        super().__init__(f"ARI {method} {path}: {status or 'сеть'} {detail}".strip())


class AriClient:
    """Тонкая обёртка над REST ARI."""

    def __init__(
        self,
        settings: Settings,
        *,
        logger: logging.Logger | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not settings.ari_url or not settings.ari_user or not settings.ari_password:
            raise ValueError("ARI не настроен: нужны ARI_URL, ARI_USER и ARI_PASSWORD")
        self._base = settings.ari_url.rstrip("/")
        self._headers = {
            "Authorization": aiohttp.encode_basic_auth(settings.ari_user, settings.ari_password)
        }
        self._timeout = timeout
        self._log = logger or logging.getLogger("app.ari.client")
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=self._timeout, connect=min(self._timeout, 5.0)),
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def __aenter__(self) -> AriClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _request(
        self, method: str, path: str, params: dict[str, str] | None = None
    ) -> Any:
        if self._session is None or self._session.closed:
            raise AriError(method, path, None, "клиент не запущен")
        url = f"{self._base}{path}"
        try:
            async with self._session.request(method, url, params=params) as response:
                text = await response.text()
                if response.status >= 400:
                    raise AriError(method, path, response.status, text.strip()[:500])
                if not text.strip():
                    return None
                try:
                    return json.loads(text)
                except ValueError:
                    self._log.debug("ARI %s %s: не-JSON ответ", method, path)
                    return text
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AriError(method, path, None, str(exc)) from exc

    async def asterisk_info(self) -> dict[str, Any]:
        data = await self._request("GET", "/asterisk/info")
        return data if isinstance(data, dict) else {}

    async def list_channels(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/channels")
        return data if isinstance(data, list) else []

    async def list_bridges(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/bridges")
        return data if isinstance(data, list) else []

    @property
    def base_url(self) -> str:
        return self._base

    # ------------------------------------------------- управление вызовом

    async def answer(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{_quote_id(channel_id)}/answer")

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/channels/{_quote_id(channel_id)}")
        return data if isinstance(data, dict) else {}

    async def create_bridge(self, bridge_type: str = "mixing", name: str | None = None) -> str:
        params = {"type": bridge_type}
        if name:
            params["name"] = name
        data = await self._request("POST", "/bridges", params)
        if not isinstance(data, dict) or not data.get("id"):
            raise AriError("POST", "/bridges", None, f"некорректный ответ: {data!r}")
        return str(data["id"])

    async def delete_bridge(self, bridge_id: str) -> None:
        await self._request("DELETE", f"/bridges/{_quote_id(bridge_id)}")

    async def add_channels(self, bridge_id: str, channel_ids: list[str]) -> None:
        if not channel_ids:
            return
        await self._request(
            "POST",
            f"/bridges/{_quote_id(bridge_id)}/addChannel",
            {"channel": ",".join(channel_ids)},
        )

    async def delete_channel(self, channel_id: str) -> None:
        await self._request("DELETE", f"/channels/{_quote_id(channel_id)}")

    async def create_external_media(
        self,
        *,
        app: str,
        external_host: str,
        data: str,
        media_format: str = "slin",
        transport: str = "tcp",
        encapsulation: str = "audiosocket",
    ) -> dict[str, Any]:
        params = {
            "app": app,
            "external_host": external_host,
            "encapsulation": encapsulation,
            "transport": transport,
            "format": media_format,
            "data": data,
        }
        result = await self._request("POST", "/channels/externalMedia", params)
        if not isinstance(result, dict) or not result.get("id"):
            raise AriError("POST", "/channels/externalMedia", None, f"ответ: {result!r}")
        return result


def _quote_id(channel_id: str) -> str:
    """Экранировать идентификатор канала/бриджа для URL."""
    return quote(channel_id, safe="")


async def close_quietly(client: AriClient, channel_id: str) -> None:
    """Удалить канал, не мешая завершению вызова."""
    try:
        await client.delete_channel(channel_id)
    except (AriError, asyncio.TimeoutError, aiohttp.ClientError):
        pass
