"""TCP-сервер AudioSocket.

В рабочем режиме медиа приходит от ARI: бот создаёт ExternalMedia-канал с
`encapsulation=audiosocket`, Asterisk подключается к этому серверу и первым
фреймом присылает UUID, переданный в параметре `data`. По UUID соединение
связывается с вызовом (см. `app.audiosocket.registry`).

Сервер годится и для автономного режима (без ARI): тогда UUID генерирует
Asterisk, а сессия обслуживается без привязки к вызову.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from app.audiosocket.protocol import read_uuid_frame
from app.audiosocket.registry import PendingRegistry
from app.billing.client import BillingLookup
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
        registry: PendingRegistry | None = None,
        billing: BillingLookup | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._scenario = scenario
        self._prompts = prompts
        self._registry = registry
        self._billing = billing
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
        try:
            channel_uuid = await self._await_uuid(reader, peer_name)
            if channel_uuid is None:
                return

            pending = None
            if self._registry is not None:
                pending = self._registry.pop(str(channel_uuid))
                if pending is None and not self._settings.allow_unknown_sessions:
                    self._log.warning(
                        "соединение %s: UUID %s не связан ни с одним вызовом — отклоняем",
                        peer_name or "?",
                        channel_uuid,
                    )
                    return
                if pending is None:
                    self._log.warning(
                        "соединение %s: UUID %s без ожидающего вызова (автономный режим)",
                        peer_name or "?",
                        channel_uuid,
                    )

            session = CallSession(
                reader,
                writer,
                self._settings,
                self._scenario,
                self._prompts,
                channel_uuid=channel_uuid,
                pending=pending,
                billing=self._billing,
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
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _await_uuid(
        self, reader: asyncio.StreamReader, peer_name: str | None
    ) -> uuid.UUID | None:
        try:
            return await asyncio.wait_for(
                read_uuid_frame(reader), timeout=self._settings.init_timeout
            )
        except asyncio.TimeoutError:
            self._log.warning(
                "соединение %s: UUID не получен за %.1f с — закрываем",
                peer_name or "?",
                self._settings.init_timeout,
            )
            return None
        except (ConnectionError, OSError) as exc:
            self._log.warning("соединение %s: ошибка при чтении UUID: %s", peer_name or "?", exc)
            return None
