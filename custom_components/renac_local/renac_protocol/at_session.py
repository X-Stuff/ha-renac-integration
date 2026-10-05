"""The module's AT side on UDP 48899 (PLAN §2.8, §6.11): the discovery, the AT handshake with one module and the
`AT+SOCKB` command, its query and its set. Nothing else is ever sent: on this firmware a command that waits for anything
hangs the module's IP stack until a power cycle.

The set is STATE-CHANGING: socket B, the module's cloud connection, dials the new address at once. It is checked before
it is sent, sent once, never again without a reply, and logged at warning level with the value before and after.

The module can answer one datagram many times; a reply's copies are dropped, never taken as the next command's reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from dataclasses import dataclass
from types import TracebackType

from .const import AT_PORT

LOGGER = logging.getLogger(__name__)

BROADCAST = "255.255.255.255"
GREETING = b"HF-A11ASSISTHREAD"
# After the greeting's answer: the module takes AT commands from this sender from then on; no answer comes.
AT_MODE = b"+ok"
QUIT = b"AT+Q\r"
REPLY_END = "\r\n\r\n"
DISCOVER_TRIES = 3
DISCOVER_TIMEOUT_S = 2.0
# Nothing within this is no reply.
REPLY_TIMEOUT_S = 2.0
# A reply in several datagrams ends when the module has been quiet this long; its copies come within it.
QUIET_S = 0.4
# After a datagram that gets no answer (`+ok`, `AT+Q`), before the next one or before the socket closes.
GAP_S = 0.4
SOCKB_PROTOCOL = "TCP"
SOCKB_HOST_MAX = 100
HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


class AtError(Exception):
    """The module did not answer, or answered with an error or something else than asked for."""


@dataclass(frozen=True)
class FoundModule:
    """A module that answered the greeting: the IP it answered from, its MAC and its hostname as it gave them."""

    ip: str
    mac: str
    hostname: str

    def __str__(self) -> str:
        return f"{self.ip},{self.mac},{self.hostname}"


@dataclass(frozen=True)
class SocketB:
    """Where the module's socket B, its cloud connection, dials: `TCP,<port>,<host>` as `AT+SOCKB` has it."""

    host: str
    port: int
    protocol: str = SOCKB_PROTOCOL

    def __str__(self) -> str:
        return f"{self.protocol},{self.port},{self.host}"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> SocketB:
        """Socket B from `AT+SOCKB`'s value, `TCP,<port>,<host>`. Raises ValueError for anything else."""
        parts = text.split(",")
        if len(parts) != 3 or not parts[0] or not (parts[1].isascii() and parts[1].isdecimal()) or not parts[2]:
            raise ValueError(f"{text!r} is not PROTOCOL,PORT,HOST")
        protocol, port, host = parts
        return cls(host, int(port), protocol)

    @classmethod
    def from_address(cls, text: str) -> SocketB:
        """Socket B from `host:port`, checked as a set's value. Raises ValueError."""
        host, _, port = text.rpartition(":")
        if not (port.isascii() and port.isdecimal()):
            raise ValueError(f"{text!r} is not HOST:PORT")
        value = cls(host, int(port))
        value.check()
        return value

    def check(self) -> None:
        """Raises ValueError unless a set may send this: TCP, a port 1-65535, an IPv4 address or a host name of at most
        100 characters; none of them holds a character the command would have to escape."""
        if self.protocol != SOCKB_PROTOCOL:
            raise ValueError(f"protocol {self.protocol!r}, only {SOCKB_PROTOCOL} is sent")
        if not 0 < self.port < 65536:
            raise ValueError(f"port {self.port} is not 1-65535")
        if not is_host(self.host):
            raise ValueError(f"{self.host!r} is not an IPv4 address or a host name")


def is_host(host: str) -> bool:
    """True for an IPv4 address, or a host name of at most 100 characters whose last label is not a number."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        labels = host.split(".")
        return len(host) <= SOCKB_HOST_MAX and all(HOST_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()
    return True


def parse_greeting_answer(data: bytes, ip: str) -> FoundModule:
    """The module from its answer to the greeting, `ip,mac,hostname`; `ip` is the address it answered from. Raises
    AtError for anything else."""
    parts = data.decode("ascii", "replace").strip().split(",")
    if len(parts) != 3 or not all(parts):
        raise AtError(f"{ip}: {data!r} is not ip,mac,hostname")
    return FoundModule(ip, parts[1], parts[2])


class _Datagrams(asyncio.DatagramProtocol):
    """Queues what arrives with its sender; errors (an ICMP unreachable) are logged, the wait for a reply decides."""

    def __init__(self) -> None:
        self.received: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        LOGGER.debug("AT from %s: %r", addr[0], data)
        self.received.put_nowait((data, addr[0]))

    def error_received(self, exc: Exception) -> None:
        LOGGER.debug("AT socket: %s", exc)

    def drop_waiting(self) -> None:
        while not self.received.empty():
            self.received.get_nowait()

    async def next(self, timeout_s: float) -> tuple[bytes, str] | None:
        """The next datagram, or None when none comes within `timeout_s`."""
        try:
            return await asyncio.wait_for(self.received.get(), timeout_s)
        except TimeoutError:
            return None

    async def wait_quiet(self, quiet_s: float) -> None:
        """Drops what arrives until nothing has for `quiet_s`."""
        while await self.next(quiet_s) is not None:
            pass


async def discover(address: str = BROADCAST, port: int = AT_PORT, *, tries: int = DISCOVER_TRIES, timeout_s: float = DISCOVER_TIMEOUT_S) -> list[FoundModule]:
    """Sends the greeting to `address` (by default a broadcast on the local network) and returns every module that
    answered, each once, in the order they answered. A try is waited for `timeout_s`; the next one goes only when none
    answered. Read-only: the greeting alone puts no module in AT mode."""
    loop = asyncio.get_running_loop()
    transport, datagrams = await loop.create_datagram_endpoint(_Datagrams, local_addr=("0.0.0.0", 0), allow_broadcast=True)
    found: dict[str, FoundModule] = {}
    try:
        for _ in range(tries):
            LOGGER.debug("AT greeting to %s:%d", address, port)
            transport.sendto(GREETING, (address, port))
            deadline = loop.time() + timeout_s
            while (left := deadline - loop.time()) > 0 and (received := await datagrams.next(left)) is not None:
                data, sender = received
                try:
                    module = parse_greeting_answer(data, sender)
                except AtError as error:
                    LOGGER.debug("discovery: %s", error)
                    continue
                found.setdefault(sender, module)
            if found:
                break
    finally:
        transport.close()
    return list(found.values())


class AtSession:
    """One module's AT session: the greeting and `+ok` open it, `AT+Q` ends it; one command at a time, its reply read
    before the next goes. As a context manager: opened on entry, ended on exit."""

    def __init__(self, ip: str, port: int = AT_PORT, *, reply_timeout_s: float = REPLY_TIMEOUT_S, quiet_s: float = QUIET_S, gap_s: float = GAP_S) -> None:
        self.ip = ip
        self.port = port
        self.reply_timeout_s = reply_timeout_s
        self.quiet_s = quiet_s
        self.gap_s = gap_s
        # The module as it answered the greeting, once the session is open.
        self.module: FoundModule | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._datagrams: _Datagrams | None = None

    async def __aenter__(self) -> AtSession:
        await self.open()
        return self

    async def __aexit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        await self.close()

    async def open(self) -> FoundModule:
        """The AT handshake: the greeting, its answer, then `+ok`. Raises AtError when the module does not answer or
        cannot be reached."""
        loop = asyncio.get_running_loop()
        try:
            self._transport, self._datagrams = await loop.create_datagram_endpoint(_Datagrams, remote_addr=(self.ip, self.port))
        except OSError as error:
            raise AtError(f"{self.ip}: {error}") from error
        try:
            data = await self._ask(GREETING, whole=False)
            self.module = parse_greeting_answer(data, self.ip)
            self._send(AT_MODE)
            await asyncio.sleep(self.gap_s)
        except BaseException:
            self._close_socket()
            raise
        LOGGER.info("AT session with %s open: %s", self.ip, self.module)
        return self.module

    async def close(self) -> None:
        """`AT+Q`, no reply waited for, then a short wait before the socket closes."""
        if self._transport is None:
            return
        with contextlib.suppress(OSError):
            self._send(QUIT)
            await asyncio.sleep(self.gap_s)
        self._close_socket()
        LOGGER.info("AT session with %s ended", self.ip)

    async def read_socket_b(self) -> SocketB:
        """`AT+SOCKB`, the query: where socket B dials. Raises AtError."""
        reply = await self._command("AT+SOCKB")
        if not reply.startswith("+ok="):
            raise AtError(f"{self.ip}: AT+SOCKB answered {reply!r}")
        try:
            return SocketB.parse(reply.removeprefix("+ok="))
        except ValueError as error:
            raise AtError(f"{self.ip}: AT+SOCKB answered {reply!r}: {error}") from error

    async def set_socket_b(self, value: SocketB) -> SocketB:
        """STATE-CHANGING: `AT+SOCKB=TCP,<port>,<host>`, socket B dials there at once. Reads socket B before and after and
        returns the value after; a value socket B already holds sends nothing. Raises ValueError for a value that may not
        be sent (nothing is sent then), AtError when the module does not take it."""
        value.check()
        before = await self.read_socket_b()
        if before == value:
            LOGGER.info("socket B of %s already dials %s; nothing set", self.ip, value)
            return before
        LOGGER.warning("setting socket B of %s from %s to %s", self.ip, before, value)
        reply = await self._command(f"AT+SOCKB={value}")
        if reply != "+ok":
            raise AtError(f"{self.ip}: AT+SOCKB={value} answered {reply!r}")
        after = await self.read_socket_b()
        LOGGER.warning("socket B of %s set: before %s, after %s", self.ip, before, after)
        return after

    async def _command(self, command: str) -> str:
        """One command and its reply up to its end, an error reply as AtError."""
        data = await self._ask(f"{command}\r".encode("ascii"), whole=True)
        reply = data.decode("ascii", "replace").split(REPLY_END, 1)[0].strip()
        if reply.startswith("+ERR"):
            raise AtError(f"{self.ip}: {command} answered {reply}")
        return reply

    async def _ask(self, data: bytes, *, whole: bool) -> bytes:
        """Sends the datagram and returns the reply: its first datagram, and with `whole` the ones after it up to the
        end of a command's reply or until the module is quiet. The reply's copies are dropped, before the send and
        after the reply. Raises AtError when nothing comes within the reply timeout."""
        assert self._datagrams is not None
        self._datagrams.drop_waiting()
        self._send(data)
        first = await self._datagrams.next(self.reply_timeout_s)
        if first is None:
            raise AtError(f"{self.ip}: no reply to {data.strip()!r} within {self.reply_timeout_s:g} s")
        reply = first[0]
        while whole and REPLY_END.encode("ascii") not in reply and (more := await self._datagrams.next(self.quiet_s)) is not None:
            reply += more[0]
        await self._datagrams.wait_quiet(self.quiet_s)
        return reply

    def _send(self, data: bytes) -> None:
        assert self._transport is not None
        LOGGER.debug("AT to %s: %r", self.ip, data)
        self._transport.sendto(data)

    def _close_socket(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._datagrams = None
