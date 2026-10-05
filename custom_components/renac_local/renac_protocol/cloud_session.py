"""The cloud session: Renac's cloud served for one module session, as the module would serve it, through HA.

`CloudSession` is made by the poller when a module session starts and stopped when it ends. It connects to the cloud,
its name resolved through the DNS client or the system's resolver, or through the caller's own way of connecting
(`open_cloud`, a proxy for example, which gets the name unresolved); after a failure or a drop it tries again. The
cloud's frames are handled one after another, in one task, in the cloud's order:

- A read of registers the inverter holds, every one valid, is answered from it; the module is not asked. Without an
  inverter, a refused session's, every read goes to the module.
- Any other request goes to the module with the high priority, the next one after the request in flight, once, never
  repeated, and its reply goes back.
- A write to the clock, 20001-20006, goes to the module; its echo goes back and the inverter takes the written words;
  an exception goes back and the words stay; on a timeout the cloud gets nothing and those registers leave the words.
  Any other write is blocked, the cloud gets no answer, unless `forward_unknown_writes`; it is reported to
  `on_cloud_write` either way, and logged at warning level when blocked, at info level when forwarded: the user allowed
  it.
- A Modbus frame with a bad CRC is dropped, unless `forward_bad_crc`; logged once per cloud connection.
- The module's frames nobody asked for go to the cloud (`take_unsolicited()`), in the order they came.

The module's replies to the cloud's handshake go into the session's `ModuleInfo`. Everything goes to `logging`, at
debug level every frame to and from the cloud with its hex and its decoded form. The session never writes to the
inverter on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .channel import Channel, ChannelClosed, ChannelError, Opener, open_stream
from .connection import Connection, Matcher, Priority, ReplyTimeout, aa55_reply_matcher, modbus_reply_matcher
from .const import CLOUD_PORT, CLOUD_RETRY_S, DEFAULT_DNS_SERVER, IDLE_TIMEOUT_S
from .dns import DnsError, is_ip_literal, resolve_host
from .frames import MAX_READ_COUNT, AA55Frame, Frame, ModbusFrame, ModbusKind, build_read_reply, describe_bytes, describe_frame
from .handshake import REPLIED, ModuleInfo
from .inverter import Inverter

LOGGER = logging.getLogger(__name__)

# The inverter's clock, the only registers the cloud's writes reach by default.
CLOCK_ADDRESSES = range(20001, 20007)


def is_clock_write(address: int, count: int) -> bool:
    """True when every register written is one of the clock's."""
    return count >= 1 and CLOCK_ADDRESSES.start <= address and address + count <= CLOCK_ADDRESSES.stop


@dataclass(frozen=True)
class CloudAddress:
    """Where the cloud is: a host name or an IP literal, its port, and the DNS server for a name (None: the system's
    resolver)."""

    host: str
    port: int = CLOUD_PORT
    dns_server: str | None = DEFAULT_DNS_SERVER

    @property
    def name(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def resolver(self) -> str:
        """How the host becomes an address, for logs and the console."""
        if is_ip_literal(self.host):
            return "an IP address"
        return "the system's resolver" if self.dns_server is None else f"DNS server {self.dns_server}"


@dataclass(frozen=True)
class CloudSettings:
    """The cloud and the policies of D18. `open_cloud` opens the cloud connection in place of a direct one, given the
    cloud's host unresolved and its port; `cloud_via` names it for the logs."""

    address: CloudAddress
    forward_unknown_writes: bool = False
    forward_bad_crc: bool = False
    open_cloud: Opener | None = None
    cloud_via: str = ""
    retry_s: tuple[float, ...] = CLOUD_RETRY_S
    idle_timeout_s: float = IDLE_TIMEOUT_S

    @property
    def route(self) -> str:
        """The cloud and the way to it, for logs and the console."""
        if self.open_cloud is not None:
            return f"{self.address.name} via {self.cloud_via or 'the given connection'}"
        return f"{self.address.name} through {self.address.resolver}"

    def describe(self) -> str:
        """The cloud and its policies, for the logs."""
        return (
            f"cloud {self.route}, its writes outside the clock {'forwarded' if self.forward_unknown_writes else 'blocked'},"
            f" its frames with a bad CRC {'forwarded' if self.forward_bad_crc else 'dropped'}"
        )


@dataclass
class CloudStatus:
    """The cloud's side: the address connected to, whether it is connected, and why not."""

    address: str | None = None
    connected: bool = False
    error: str | None = None


@dataclass
class CloudCounters:
    """What the session did with the cloud's frames."""

    connects: int = 0
    answered_from_state: int = 0
    forwarded: int = 0
    writes_blocked: int = 0
    bad_crc_dropped: int = 0


@dataclass(frozen=True)
class CloudWrite:
    """A write the cloud sent: the first register, how many, their values, whether it went to the inverter, and when
    (seconds since the epoch)."""

    address: int
    count: int
    values: tuple[int, ...]
    forwarded: bool
    at: float

    @property
    def is_clock(self) -> bool:
        return is_clock_write(self.address, self.count)

    def describe(self) -> str:
        return f"{self.address} x{self.count} = {', '.join(str(value) for value in self.values)}"


class CloudSession:
    """The cloud, served on the module's connection for one session; its reads answered from `inverter` when one is
    set and holds them."""

    def __init__(self, module: Connection, settings: CloudSettings, module_info: ModuleInfo, *, inverter: Inverter | None = None) -> None:
        self.module = module
        self.settings = settings
        self.module_info = module_info
        # The inverter the cloud's reads are answered from and its writes go into; None: every read to the module.
        self.inverter = inverter
        self.status = CloudStatus()
        self.counters = CloudCounters()
        self.last_write: CloudWrite | None = None
        # Called after every change worth showing: the connection, a cloud write, a dropped bad-CRC frame.
        self.on_update: Callable[[], None] | None = None
        # Called for every cloud write outside the clock, blocked or forwarded: HA reports it.
        self.on_cloud_write: Callable[[CloudWrite], None] | None = None
        # The connection to the cloud while there is one.
        self.channel: Channel | None = None
        self._task: asyncio.Task[None] | None = None
        self._bad_crc_logged = False

    @property
    def connected(self) -> bool:
        return self.channel is not None and self.channel.is_connected

    def start(self) -> None:
        """Connects to the cloud and serves it until `stop()`."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="cloud session")

    async def stop(self, reason: str = "stopped") -> None:
        """Stops serving and closes the cloud's connection."""
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.channel is not None:
            await self.channel.close(reason)
            self.channel = None

    def take_unsolicited(self, frame: Frame) -> None:
        """The module sent a frame nobody asked for: it goes to the cloud."""
        if not self.connected:
            LOGGER.debug("frame nobody asked for, dropped, no cloud connected: %s", describe_frame(frame))
            return
        LOGGER.debug("frame nobody asked for, passed to the cloud: %s", describe_frame(frame))
        self._send_to_cloud(frame.raw)

    async def _run(self) -> None:
        """Keeps the cloud connected: after a failed attempt or a drop it waits and connects again."""
        failures = 0
        while True:
            try:
                channel = await self._connect()
            except (DnsError, ChannelError) as error:
                wait = self.settings.retry_s[min(failures, len(self.settings.retry_s) - 1)]
                failures += 1
                self.status = CloudStatus(error=str(error))
                LOGGER.warning("cloud %s: %s; trying again in %g s", self.settings.address.name, error, wait)
                self._notify()
                await asyncio.sleep(wait)
                continue
            failures = 0
            async for frame in channel.frames():
                if LOGGER.isEnabledFor(logging.DEBUG):
                    LOGGER.debug("from cloud  %3d B  %s  %s", len(frame.raw), frame.raw.hex(" "), describe_frame(frame))
                await self._handle(frame)
            reason = channel.close_reason or "closed"
            LOGGER.info("cloud connection ended: %s", reason)
            self.channel = None
            self.status = CloudStatus(address=self.status.address, error=reason)
            self._notify()
            LOGGER.info("connecting to the cloud again in %g s", self.settings.retry_s[0])
            await asyncio.sleep(self.settings.retry_s[0])

    async def _connect(self) -> Channel:
        """One attempt: the name resolved, unless `open_cloud` takes it as it is, then the connection. Raises DnsError
        or ChannelError."""
        settings = self.settings
        address = settings.address
        host = address.host
        if settings.open_cloud is None:
            try:
                addresses = await resolve_host(address.host, address.dns_server)
            except DnsError as error:
                raise DnsError(f"cannot resolve {address.host} through {address.resolver}: {error}") from error
            if not is_ip_literal(address.host):
                LOGGER.info("cloud %s resolved through %s: %s", address.host, address.resolver, ", ".join(addresses))
            host = addresses[0]
        reader, writer = await open_stream(host, address.port, opener=settings.open_cloud)
        channel = Channel(reader, writer, name=f"{host}:{address.port}", from_master=True, idle_timeout_s=settings.idle_timeout_s, pass_bad_crc=True)
        self.channel = channel
        self.counters.connects += 1
        self._bad_crc_logged = False
        self.status = CloudStatus(address=channel.name, connected=True)
        via = f" via {settings.cloud_via or 'the given connection'}" if settings.open_cloud is not None else ""
        LOGGER.info("cloud connected at %s%s", channel.name, via)
        self._notify()
        return channel

    async def _handle(self, frame: Frame) -> None:
        if isinstance(frame, AA55Frame):
            await self._take_aa55(frame)
        elif not frame.crc_ok:
            await self._take_bad_crc(frame)
        elif frame.kind is ModbusKind.READ_REQUEST:
            await self._take_read(frame)
        elif frame.kind in (ModbusKind.WRITE_SINGLE, ModbusKind.WRITE_MULTIPLE_REQUEST):
            await self._take_write(frame)
        else:
            LOGGER.debug("not a request from the cloud, dropped: %s", describe_frame(frame))

    async def _take_aa55(self, frame: AA55Frame) -> None:
        """The module answers the cloud's AA55 frames itself; the keepalive gets no answer."""
        if frame.is_reply:
            LOGGER.debug("AA55 reply from the cloud, dropped: %s", describe_frame(frame))
            return
        if (frame.command, frame.sub) not in REPLIED:
            self.counters.forwarded += 1
            await self._send_without_reply(frame)
            return
        reply = await self._forward(frame, aa55_reply_matcher(frame))
        if reply is None:
            return
        if isinstance(reply, AA55Frame) and self.module_info.update_from(reply):
            self._notify()
        self._send_to_cloud(reply.raw)

    async def _take_read(self, frame: ModbusFrame) -> None:
        """Answered from the inverter when it holds every register asked for, valid; from the module otherwise."""
        address, count = frame.address or 0, frame.count or 0
        inverter = self.inverter
        words = inverter.valid_words(address, count) if inverter is not None and count <= MAX_READ_COUNT else None
        if words is not None:
            self.counters.answered_from_state += 1
            LOGGER.debug("cloud's %s answered from the state", describe_frame(frame))
            self._send_to_cloud(build_read_reply(words, frame.unit))
            return
        reply = await self._forward(frame, modbus_reply_matcher(frame))
        if reply is not None:
            self._send_to_cloud(reply.raw)

    async def _take_write(self, frame: ModbusFrame) -> None:
        """D18: the clock goes to the inverter; any other write only with `forward_unknown_writes`."""
        address, count = frame.address or 0, frame.count or 0
        clock = is_clock_write(address, count)
        write = CloudWrite(address, count, frame.registers(), forwarded=clock or self.settings.forward_unknown_writes, at=time.time())
        self.last_write = write
        if clock:
            LOGGER.info("cloud's clock write %s: to the inverter", write.describe())
        else:
            if write.forwarded:
                LOGGER.info("cloud's write %s is not the clock's: to the inverter, forwarding the cloud's other writes is on", write.describe())
            else:
                self.counters.writes_blocked += 1
                LOGGER.warning("cloud's write %s is not the clock's: blocked, the cloud gets no answer", write.describe())
            if self.on_cloud_write is not None:
                self.on_cloud_write(write)
        self._notify()
        if not write.forwarded:
            return
        addresses = range(address, address + count)
        reply = await self._forward(frame, modbus_reply_matcher(frame))
        inverter = self.inverter
        if reply is None:
            if inverter is not None:
                inverter.forget(addresses)
            return
        if isinstance(reply, ModbusFrame) and not reply.is_exception and inverter is not None:
            inverter.store_written(addresses, write.values)
        self._send_to_cloud(reply.raw)

    async def _take_bad_crc(self, frame: ModbusFrame) -> None:
        """Dropped, or with `forward_bad_crc` sent as received; any reply comes back as a frame nobody asked for."""
        if self.settings.forward_bad_crc:
            self._log_bad_crc(frame, "forwarded as received")
            self.counters.forwarded += 1
            await self._send_without_reply(frame)
            return
        self.counters.bad_crc_dropped += 1
        self._log_bad_crc(frame, "dropped")
        self._notify()

    def _log_bad_crc(self, frame: ModbusFrame, action: str) -> None:
        """The first one of a cloud connection at info level, the rest at debug level."""
        if self._bad_crc_logged:
            LOGGER.debug("cloud's frame with a bad CRC %s: %s  %s", action, frame.raw.hex(" "), describe_frame(frame))
            return
        self._bad_crc_logged = True
        LOGGER.info("cloud's frame with a bad CRC %s: %s  %s; more on this cloud connection at debug level", action, frame.raw.hex(" "), describe_frame(frame))

    async def _forward(self, frame: Frame, matches: Matcher) -> Frame | None:
        """Sends the cloud's request to the module as the next request, once, never repeated: a repeated write would
        reach the inverter twice. Its reply; None when none came."""
        self.counters.forwarded += 1
        try:
            return await self.module.send(frame.raw, matches, priority=Priority.HIGH, retries=0)
        except ReplyTimeout as error:
            LOGGER.warning("cloud's %s: %s; the cloud gets nothing", describe_frame(frame), error)
        except ChannelError as error:
            LOGGER.warning("cloud's %s: %s", describe_frame(frame), error)
        return None

    async def _send_without_reply(self, frame: Frame) -> None:
        try:
            await self.module.send_without_reply(frame.raw, priority=Priority.HIGH)
        except ChannelError as error:
            LOGGER.warning("cloud's %s: %s", describe_frame(frame), error)

    def _send_to_cloud(self, data: bytes) -> None:
        channel = self.channel
        if channel is None:
            return
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("to cloud    %3d B  %s  %s", len(data), data.hex(" "), describe_bytes(data, from_master=False))
        with contextlib.suppress(ChannelClosed):
            channel.write(data)

    def _notify(self) -> None:
        if self.on_update is not None:
            self.on_update()
