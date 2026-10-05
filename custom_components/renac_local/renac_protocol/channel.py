"""One TCP stream and its framer: the frames pulled by its one consumer, the bytes written, the connection closed.

`frames()` is an async iterator; the idle timeout for a partial frame is inside it. `from_master` says whose bytes come
in: a master's, requests (the cloud), or a device's, replies (the module). Every chunk in and out goes to the logger
`renac_protocol.raw` at debug level, which is how a hexdump is had. Nothing here calls back: when the connection ends
`closed` is set, with `close_reason`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from .const import CONNECT_TIMEOUT_S, IDLE_TIMEOUT_S
from .framer import Framer
from .frames import Frame

READ_SIZE = 4096

# Every chunk of bytes in and out, at debug level: `<name> -> <n> B  <hex>` out, `<name> <- <n> B  <hex>` in.
RAW_LOGGER = logging.getLogger("renac_protocol.raw")

# Opens a TCP connection to (host, port) and returns its streams; asyncio.open_connection by default.
Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class ChannelError(Exception):
    """The channel could not do what was asked."""


class ChannelClosed(ChannelError):
    """The channel is not connected."""


@dataclass
class ChannelCounters:
    """What went through the channel."""

    bytes_in: int = 0
    bytes_out: int = 0
    frames_in: int = 0


async def open_stream(
    host: str, port: int, *, timeout_s: float = CONNECT_TIMEOUT_S, opener: Opener | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connects to host:port, through `opener` when given. Raises ChannelError when it cannot."""
    try:
        return await asyncio.wait_for((opener or asyncio.open_connection)(host, port), timeout_s)
    except (OSError, TimeoutError) as error:
        raise ChannelError(f"connect to {host}:{port}: {error or 'timeout'}") from error


class Channel:
    """One connection's stream and framer, alive as long as the connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        name: str,
        from_master: bool = False,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        pass_bad_crc: bool = False,
    ) -> None:
        # The peer's address, for logs and errors.
        self.name = name
        self.framer = Framer(from_master=from_master, idle_timeout_s=idle_timeout_s, pass_bad_crc=pass_bad_crc)
        self.counters = ChannelCounters()
        self.connected_at = time.monotonic()
        # Set when the connection has ended, either way; the reason in `close_reason`.
        self.closed = asyncio.Event()
        self.close_reason: str | None = None
        self._reader = reader
        self._writer: asyncio.StreamWriter | None = writer

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def frames(self) -> AsyncIterator[Frame]:
        """The frames as they arrive, until the connection ends; then `closed` is set with the reason."""
        reason = "closed by peer"
        try:
            while True:
                timeout = self.framer.idle_timeout_s if self.framer.pending_bytes else None
                try:
                    data = await asyncio.wait_for(self._reader.read(READ_SIZE), timeout)
                except TimeoutError:
                    self.framer.flush_idle(time.monotonic())
                    continue
                if not data:
                    break
                self.counters.bytes_in += len(data)
                if RAW_LOGGER.isEnabledFor(logging.DEBUG):
                    RAW_LOGGER.debug("%s <- %3d B  %s", self.name, len(data), data.hex(" "))
                for frame in self.framer.feed(data, time.monotonic()):
                    self.counters.frames_in += 1
                    yield frame
        except OSError as error:
            reason = str(error)
        self._end(reason)

    def write(self, data: bytes) -> None:
        """Writes the bytes; the transport buffers them, and a connection that fails shows on the reader. Raises
        ChannelClosed when not connected."""
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ChannelClosed(f"{self.name} is not connected")
        if RAW_LOGGER.isEnabledFor(logging.DEBUG):
            RAW_LOGGER.debug("%s -> %3d B  %s", self.name, len(data), data.hex(" "))
        writer.write(data)
        self.counters.bytes_out += len(data)

    async def close(self, reason: str = "closed") -> None:
        """Closes the connection; nothing happens when it is closed already."""
        writer = self._writer
        if writer is None:
            return
        self._end(reason)
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    def _end(self, reason: str) -> None:
        if self.close_reason is None:
            self.close_reason = reason
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self.closed.set()
