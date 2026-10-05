"""The setup flow's wait for the module after its socket B was set at Home Assistant (PLAN §6.8): does it dial in?

The port is opened before the set, so the module's first dial already counts. A connection from the module's IP ends
the wait; every connection is closed at once, and the module dials again, to the entry's port once that is open. When
the entry being reconfigured already listens on the port, its listener is watched instead: a new session from the
module's IP ends the wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .renac_protocol import Listener
from .renac_protocol.const import DEFAULT_BIND

LOGGER = logging.getLogger(__name__)

# How often the entry's listener is looked at while waiting.
WATCH_S = 0.2


class ModuleWait:
    """One wait for the module at `module_ip` on `port`; `listener` is the entry's when it holds the port. `open()`
    before the set, then `wait()`, then `close()`."""

    def __init__(self, module_ip: str, port: int, listener: Listener | None = None) -> None:
        self.module_ip = module_ip
        self.port = port
        self.listener = listener
        self._sessions = listener.sessions if listener is not None else 0
        self._dialled = asyncio.Event()
        self._server: asyncio.Server | None = None

    async def open(self) -> None:
        """Opens the port, unless the entry's listener holds it. Raises OSError when it cannot be opened."""
        if self.listener is None:
            self._server = await asyncio.start_server(self._on_connection, DEFAULT_BIND, self.port)

    async def wait(self, timeout_s: float) -> bool:
        """True when the module dials in within `timeout_s`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._module_came():
            left = deadline - loop.time()
            if left <= 0:
                return False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._dialled.wait(), min(left, WATCH_S))
        return True

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    def _module_came(self) -> bool:
        if self._dialled.is_set():
            return True
        listener = self.listener
        if listener is None or listener.sessions == self._sessions or listener.session is None:
            return False
        return listener.session.name.startswith(f"{self.module_ip}:")

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if isinstance(peer, tuple) and peer else None
        if ip == self.module_ip:
            LOGGER.info("the module %s dialled port %d", ip, self.port)
            self._dialled.set()
        else:
            LOGGER.info("a connection from %s to port %d while waiting for the module %s: closed", ip, self.port, self.module_ip)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
