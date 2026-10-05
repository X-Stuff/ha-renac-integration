"""Server mode's port: the one the module dials, as it dials the cloud.

One session at a time: an accepted stream becomes a `Connection` handed to `on_session`, its owner's, the poller; a new
connection closes the one before, "replaced". The listener keeps the current session only to close it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .channel import Channel, ChannelError
from .connection import Connection
from .const import DEFAULT_BIND, DEFAULT_SERVER_PORT, IDLE_TIMEOUT_S, REPLY_TIMEOUT_S, REQUEST_GAP_S, RETRIES

LOGGER = logging.getLogger(__name__)


class Listener:
    """The port the module dials; `port` 0 takes a free one, known after `start()`."""

    def __init__(
        self,
        bind: str = DEFAULT_BIND,
        port: int = DEFAULT_SERVER_PORT,
        on_session: Callable[[Connection], None] | None = None,
        *,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        request_gap_s: float = REQUEST_GAP_S,
        retries: int = RETRIES,
    ) -> None:
        self.bind = bind
        self.port = port
        # Called with every accepted session's connection.
        self.on_session = on_session
        self.idle_timeout_s = idle_timeout_s
        self.reply_timeout_s = reply_timeout_s
        self.request_gap_s = request_gap_s
        self.retries = retries
        # The current session's connection, None before the first.
        self.session: Connection | None = None
        # Sessions accepted so far.
        self.sessions = 0
        self._server: asyncio.AbstractServer | None = None
        self._accept_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return f"{self.bind}:{self.port}"

    async def start(self) -> None:
        """Opens the port. Raises ChannelError when it cannot be opened."""
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(self._accept, self.bind, self.port)
        except OSError as error:
            raise ChannelError(f"open {self.name}: {error}") from error
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Closes the port and the current session."""
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        if self.session is not None:
            await self.session.close("server stopped")
        if server is not None:
            await server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        address = writer.get_extra_info("peername")
        peer = f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) >= 2 else str(address)
        async with self._accept_lock:
            if self._server is None:
                writer.close()
                return
            previous = self.session
            if previous is not None and previous.is_connected:
                await previous.close(f"replaced by a new connection from {peer}")
            self.sessions += 1
            channel = Channel(reader, writer, name=peer, idle_timeout_s=self.idle_timeout_s)
            connection = Connection(channel, reply_timeout_s=self.reply_timeout_s, request_gap_s=self.request_gap_s, retries=self.retries)
            self.session = connection
            LOGGER.info("module connected from %s, session %d", peer, self.sessions)
        if self.on_session is not None:
            self.on_session(connection)
