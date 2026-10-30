"""Мини-сервер ARI для тестов: REST-эндпоинты и WebSocket событий.

Повторяет только то, что использует бот, и записывает все вызовы, чтобы
проверить порядок и параметры команд.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any

from aiohttp import WSMsgType, web

ASTERISK_INFO = {
    "system": {"version": "22.5.2-test", "entity_id": "test"},
    "build": {"os": "test"},
}


class FakeAri:
    """Фейковый Asterisk ARI на случайном порту."""

    def __init__(self, username: str = "ivr_ai", password: str = "secret") -> None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._expected_auth = f"Basic {token}"
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.em_requests: list[dict[str, str]] = []
        self.ws: web.WebSocketResponse | None = None
        self.ws_ready = asyncio.Event()
        self.ws_connections = 0
        self.close_ws_on_connect = 0
        self._runner: web.AppRunner | None = None
        self._socket: socket.socket | None = None
        self.port = 0

        app = web.Application()
        app.router.add_route("*", "/ari/{tail:.*}", self._handle)
        self._app = app

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/ari"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ari/events?app=ivr_ai"

    async def start(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self.port = int(self._socket.getsockname()[1])

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        await web.SockSite(self._runner, self._socket).start()

    async def stop(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    async def send_event(self, event: dict[str, Any]) -> None:
        if self.ws is None or self.ws.closed:
            raise RuntimeError("нет WebSocket-соединения с ARI")
        await self.ws.send_str(json.dumps(event, ensure_ascii=False))

    def paths(self, method: str | None = None) -> list[str]:
        return [path for m, path, _ in self.calls if method is None or m == method]

    def params_of(self, path_suffix: str) -> list[dict[str, str]]:
        return [params for _, path, params in self.calls if path.endswith(path_suffix)]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != self._expected_auth:
            return web.json_response({"message": "Unauthorized"}, status=401)

        if request.path == "/ari/events":
            return await self._events(request)

        params = dict(request.query)
        self.calls.append((request.method, request.path, params))

        if request.method == "GET" and request.path == "/ari/asterisk/info":
            return web.json_response(ASTERISK_INFO)

        if request.method == "GET" and request.path == "/ari/channels":
            return web.json_response([])

        if request.method == "POST" and request.path.endswith("/answer"):
            return web.json_response({"id": _channel_id(request.path)})

        if request.method == "POST" and request.path == "/ari/bridges":
            return web.json_response({"id": "bridge-1", "type": params.get("type")})

        if request.method == "POST" and request.path.endswith("/addChannel"):
            return web.json_response({"id": request.path.split("/")[3]})

        if request.method == "POST" and request.path == "/ari/channels/externalMedia":
            self.em_requests.append(params)
            return web.json_response({"id": "em-1", "state": "Down"})

        if request.method == "DELETE":
            return web.json_response({})

        return web.json_response({"message": "not found"}, status=404)

    async def _events(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.ws = ws
        self.ws_connections += 1
        self.ws_ready.set()
        if self.close_ws_on_connect:
            self.close_ws_on_connect -= 1
            await ws.close()
            return ws
        async for message in ws:
            if message.type is WSMsgType.ERROR:
                break
        return ws


def _channel_id(path: str) -> str:
    # /ari/channels/<id>/answer
    parts = path.strip("/").split("/")
    return parts[2] if len(parts) > 2 else ""
