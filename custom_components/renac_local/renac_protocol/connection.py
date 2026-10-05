"""One request at a time on a channel, its reply matched by shape.

`Connection` owns the task that pulls its channel's frames, for the life of that channel: a module session in server
mode, one dial in client mode. A request is sent, its reply matched, a timeout tried once more. A frame that matches
no pending request is unsolicited: counted and handed to `on_unsolicited`, the owner's. `hold(seconds)` keeps the next
request back; client mode uses it as its quiet time after the cloud's traffic on the shared UART.

Two priorities: a HIGH request, the cloud's in server mode, is the next one sent, ahead of every waiting NORMAL one,
the polls; the request in flight always finishes first. Requests of one priority go in the order they came. Every
frame to and from the module is logged at debug level with its hex and its decoded form.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum

from .channel import Channel, ChannelClosed, open_stream
from .const import CONNECT_TIMEOUT_S, FIRST_REPLY_TIMEOUT_S, IDLE_TIMEOUT_S, REPLY_TIMEOUT_S, REQUEST_GAP_S, RETRIES
from .frames import READ, AA55Frame, Frame, ModbusFrame, ModbusKind, build_read_request, describe_bytes, describe_frame

LOGGER = logging.getLogger(__name__)

Matcher = Callable[[Frame], bool]


class Priority(Enum):
    """A request's place in the queue: HIGH goes ahead of every waiting NORMAL one."""

    NORMAL = "normal"
    HIGH = "high"


class ReplyTimeout(Exception):
    """No matching reply came within the timeout, after the retries."""


@dataclass
class ConnectionCounters:
    """Requests and replies on the connection."""

    requests: int = 0
    replies: int = 0
    retries: int = 0
    timeouts: int = 0
    exceptions: int = 0
    unsolicited: int = 0
    last_round_trip_s: float = 0.0
    max_round_trip_s: float = 0.0
    total_round_trip_s: float = 0.0

    @property
    def mean_round_trip_s(self) -> float:
        return self.total_round_trip_s / self.replies if self.replies else 0.0


class TurnQueue:
    """One holder at a time. On release the turn goes to the first waiting high-priority holder, else to the first
    waiting normal one."""

    def __init__(self) -> None:
        self._held = False
        self._high: deque[asyncio.Future[None]] = deque()
        self._normal: deque[asyncio.Future[None]] = deque()

    @contextlib.asynccontextmanager
    async def turn(self, high: bool) -> AsyncIterator[None]:
        await self.acquire(high)
        try:
            yield
        finally:
            self.release()

    async def acquire(self, high: bool) -> None:
        if not self._held:
            self._held = True
            return
        queue = self._high if high else self._normal
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queue.append(future)
        try:
            await future
        except asyncio.CancelledError:
            if future.cancelled():
                with contextlib.suppress(ValueError):
                    queue.remove(future)
            else:
                # The turn came just as the wait was cancelled: pass it on.
                self.release()
            raise

    def release(self) -> None:
        """Hands the turn to the next waiting holder; the queue stays held for it. Free when nobody waits."""
        for queue in (self._high, self._normal):
            while queue:
                future = queue.popleft()
                if not future.done():
                    future.set_result(None)
                    return
        self._held = False


def read_reply_matcher(count: int) -> Matcher:
    """Matches the reply to a fn 3 request for `count` registers: a reply of that size or a fn 3 exception."""

    def matches(frame: Frame) -> bool:
        if not isinstance(frame, ModbusFrame):
            return False
        if frame.kind is ModbusKind.READ_REPLY:
            return frame.count == count
        return frame.is_exception and frame.function == READ

    return matches


def modbus_reply_matcher(request: ModbusFrame) -> Matcher:
    """Matches the reply to any Modbus request by its shape."""
    if request.kind is ModbusKind.READ_REQUEST:
        return read_reply_matcher(request.count or 0)

    def matches(frame: Frame) -> bool:
        if not isinstance(frame, ModbusFrame):
            return False
        if frame.is_exception:
            return frame.function == request.function
        if request.kind is ModbusKind.WRITE_SINGLE:
            return frame.kind is ModbusKind.WRITE_SINGLE and frame.address == request.address
        return frame.kind is ModbusKind.WRITE_MULTIPLE_REPLY and frame.address == request.address and frame.count == request.count

    return matches


def aa55_reply_matcher(request: AA55Frame) -> Matcher:
    """Matches the reply to an AA55 request: same command and sub, reply bit set."""

    def matches(frame: Frame) -> bool:
        return isinstance(frame, AA55Frame) and frame.is_reply and frame.command == request.command and frame.sub == request.sub

    return matches


class Connection:
    """The requests and replies on one channel, for its life."""

    def __init__(
        self,
        channel: Channel,
        *,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        first_reply_timeout_s: float | None = None,
        request_gap_s: float = REQUEST_GAP_S,
        retries: int = RETRIES,
    ) -> None:
        """`first_reply_timeout_s` is the first reply's own timeout, when it has one: after a dial, while the relay
        brings its BLE link up."""
        self.channel = channel
        self.reply_timeout_s = reply_timeout_s
        self.request_gap_s = request_gap_s
        self.retries = retries
        self.counters = ConnectionCounters()
        # Called with every frame that answers no request; the owner's.
        self.on_unsolicited: Callable[[Frame], None] | None = None
        self._turns = TurnQueue()
        self._pending: tuple[Matcher, asyncio.Future[Frame]] | None = None
        self._last_exchange_at: float | None = None
        self._hold_until = 0.0
        # The next reply's own timeout while one is set; None for the usual one.
        self._next_timeout_s = first_reply_timeout_s
        self._reader_task: asyncio.Task[None] | None = asyncio.create_task(self._read(), name=f"connection {channel.name}")

    @property
    def name(self) -> str:
        """The peer's address, for logs and errors."""
        return self.channel.name

    @property
    def is_connected(self) -> bool:
        return self.channel.is_connected

    @property
    def closed(self) -> asyncio.Event:
        """Set when the connection has ended."""
        return self.channel.closed

    @property
    def close_reason(self) -> str | None:
        return self.channel.close_reason

    @property
    def connected_at(self) -> float:
        """When the connection was made (monotonic seconds)."""
        return self.channel.connected_at

    @property
    def held_for_s(self) -> float:
        """Seconds the next request still waits because of `hold()`."""
        return max(0.0, self._hold_until - time.monotonic())

    def hold(self, seconds: float) -> None:
        """Keeps the next request back for `seconds`; a longer hold already on stands."""
        self._hold_until = max(self._hold_until, time.monotonic() + seconds)

    async def read(self, address: int, count: int, *, priority: Priority = Priority.NORMAL) -> ModbusFrame:
        """Reads `count` registers from `address`: the reply, which may be an exception frame. Raises ReplyTimeout or
        ChannelError."""
        reply = await self.send(build_read_request(address, count), read_reply_matcher(count), priority=priority)
        assert isinstance(reply, ModbusFrame)
        return reply

    async def send(self, request: bytes, matches: Matcher, *, priority: Priority = Priority.NORMAL, retries: int | None = None) -> Frame:
        """Sends the request and returns the first frame `matches` accepts. Raises ReplyTimeout or ChannelError.
        `retries` replaces the connection's own count for this request."""
        attempts = (self.retries if retries is None else retries) + 1
        async with self._turns.turn(priority is Priority.HIGH):
            timeout = self._next_timeout_s if self._next_timeout_s is not None else self.reply_timeout_s
            for attempt in range(attempts):
                await self._wait_before_sending()
                future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
                self._pending = (matches, future)
                # perf_counter for durations: time.monotonic ticks in 15.6 ms steps on Windows.
                sent_at = time.perf_counter()
                try:
                    self._write(request)
                    frame = await asyncio.wait_for(future, timeout)
                except TimeoutError:
                    self.counters.timeouts += 1
                    if attempt < attempts - 1:
                        self.counters.retries += 1
                        continue
                    raise ReplyTimeout(f"no reply within {timeout:g} s, {attempt + 1} attempts") from None
                finally:
                    self._pending = None
                    self._last_exchange_at = time.monotonic()

                self._next_timeout_s = None
                self._record_round_trip(time.perf_counter() - sent_at)
                if isinstance(frame, ModbusFrame) and frame.is_exception:
                    self.counters.exceptions += 1
                return frame
            raise ReplyTimeout("no attempts")

    async def send_without_reply(self, request: bytes, *, priority: Priority = Priority.NORMAL) -> None:
        """Sends a request that gets no reply, the cloud's keepalive for one, in turn with the others. A reply that
        comes anyway is unsolicited. Raises ChannelError."""
        async with self._turns.turn(priority is Priority.HIGH):
            await self._wait_before_sending()
            try:
                self._write(request)
            finally:
                self._last_exchange_at = time.monotonic()

    async def close(self, reason: str = "closed") -> None:
        """Ends the connection; a pending request fails with ChannelClosed."""
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.channel.close(reason)
        self._fail_pending(reason)

    def _write(self, request: bytes) -> None:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("to module   %3d B  %s  %s", len(request), request.hex(" "), describe_bytes(request, from_master=True))
        self.channel.write(request)
        self.counters.requests += 1

    async def _wait_before_sending(self) -> None:
        while True:
            now = time.monotonic()
            wait = self._hold_until - now
            if self._last_exchange_at is not None:
                wait = max(wait, self._last_exchange_at + self.request_gap_s - now)
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    def _record_round_trip(self, seconds: float) -> None:
        counters = self.counters
        counters.replies += 1
        counters.last_round_trip_s = seconds
        counters.max_round_trip_s = max(counters.max_round_trip_s, seconds)
        counters.total_round_trip_s += seconds

    async def _read(self) -> None:
        async for frame in self.channel.frames():
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug("from module %3d B  %s  %s", len(frame.raw), frame.raw.hex(" "), describe_frame(frame))
            self._take(frame)
        self._fail_pending(self.channel.close_reason or "closed")

    def _take(self, frame: Frame) -> None:
        pending = self._pending
        if pending is not None:
            matches, future = pending
            if not future.done() and matches(frame):
                future.set_result(frame)
                return
        self.counters.unsolicited += 1
        if self.on_unsolicited is not None:
            self.on_unsolicited(frame)

    def _fail_pending(self, reason: str) -> None:
        pending = self._pending
        if pending is not None and not pending[1].done():
            pending[1].set_exception(ChannelClosed(f"{self.name}: {reason}"))


async def dial(
    host: str,
    port: int,
    *,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    first_reply_timeout_s: float = FIRST_REPLY_TIMEOUT_S,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    reply_timeout_s: float = REPLY_TIMEOUT_S,
    request_gap_s: float = REQUEST_GAP_S,
    retries: int = RETRIES,
) -> Connection:
    """Client mode's way in: connects to host:port and returns the connection, whose first reply may take
    `first_reply_timeout_s`. Raises ChannelError when it cannot connect."""
    reader, writer = await open_stream(host, port, timeout_s=connect_timeout_s)
    channel = Channel(reader, writer, name=f"{host}:{port}", idle_timeout_s=idle_timeout_s)
    return Connection(channel, reply_timeout_s=reply_timeout_s, first_reply_timeout_s=first_reply_timeout_s, request_gap_s=request_gap_s, retries=retries)
