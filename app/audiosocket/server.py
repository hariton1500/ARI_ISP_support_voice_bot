"""TCP-сервер AudioSocket.

Asterisk (приложение `AudioSocket(uuid,service)` в диалплане) подключается к
этому серверу и передаёт звук, DTMF и UUID канала. Каждое соединение
обслуживается отдельным экземпляром CallSession, поэтому вызовы независимы.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.audiosocket.session import CallSession
from app.config import Settings
from app.dialog.prompts import PromptMap
from app.dialog.scenario import Scenario


class AudioSocketServer:
    """Слушает порт и раздаёт соединения по сессиям."""

    def __init__(
        self,
        settings: Settings,
        scenario: Scenario,
        prompts: PromptMap,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._scenario = scenario
        self._prompts = prompts
        self._log = logger or logging.getLogger("app.audiosocket.server")
        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[CallSession] = set()

    @property
    def bound_port(self) -> int | None:
        """Фактический порт (полезно при AUDIOSOCKET_PORT=0 в тестах)."""
        if self._server is None:
            return None
        for sock in self._server.sockets or ():
            return int(sock.getsockname()[1])
        return None

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._settings.audiosocket_host,
            port=self._settings.audiosocket_port,
        )
        addresses = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or ())
        self._log.info("AudioSocket слушает: %s", addresses)

    async def close(self) -> None:
        """Закрыть порт и аккуратно завершить активные вызовы."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            self._log.info("AudioSocket остановлен")

        sessions = list(self._sessions)
        for session in sessions:
            session.abort()

        deadline = asyncio.get_running_loop().time() + 2.0
        while self._sessions and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if self._sessions:
            self._log.warning("не все вызовы завершились: %d", len(self._sessions))

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_name = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) >= 2 else None
        self._log.info("новое AudioSocket-соединение от %s", peer_name or "?")
        session = CallSession(
            reader,
            writer,
            self._settings,
            self._scenario,
            self._prompts,
            logger=self._log,
            peer=peer_name,
        )
        self._sessions.add(session)
        try:
            await session.run()
        except asyncio.CancelledError:
            raise
        finally:
            self._sessions.discard(session)
            with contextlib.suppress(Exception):
                writer.close()
