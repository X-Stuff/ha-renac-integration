"""A cloud on 127.0.0.1: it plays the cloud's polls to whoever connects and records the replies.

The server module, or the fake module's socket B, connects to it as to the real cloud. At every connection it sends
the cloud's requests in order, its own copy of them (`FIRST_POLL`, the protocol as docs/PROTOCOL.md section 7 describes
it, no unit's data): the handshake, the reads, the clock write, the bad-CRC frame, the keepalive; and right after the
clock write one write outside the clock, `OUTSIDE_CLOCK_WRITE`, which the server blocks by default. With `period_s` the
later polls (`LATER_POLL`) follow on the same connection. After each request it waits for its reply, up to
`reply_timeout_s`; the keepalive and the bad-CRC frame get none, so it does not wait for them. With `time_scale` 1 the
requests keep the cloud's timing, with 0 each goes once the one before is done. Frames that come without a request
waiting for them are kept in `unsolicited`. It connects nowhere itself.

Standalone it is the cloud of the compose test instance: the cloud's timing, a poll every --period seconds, the clock
write with the current time, every request and its reply logged. From the repo's root:

    python -m tests.mocks.mock_cloud --port 20146
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "renac_local"))

from renac_protocol.connection import Matcher, aa55_reply_matcher, modbus_reply_matcher  # noqa: E402
from renac_protocol.crc import append_crc  # noqa: E402
from renac_protocol.framer import Framer  # noqa: E402
from renac_protocol.frames import AA55Frame, Frame, FrameError, ModbusFrame, build_read_request, describe_frame, parse_frame  # noqa: E402
from renac_protocol.handshake import REPLIED, build_request  # noqa: E402

LOGGER = logging.getLogger("mock_cloud")


def clock_write_at(moment: time.struct_time) -> bytes:
    """The cloud's clock write, fn 16 to 20001-20006: year since 2000, month, day, hour, minute, second."""
    words = (moment.tm_year - 2000, moment.tm_mon, moment.tm_mday, moment.tm_hour, moment.tm_min, moment.tm_sec)
    return append_crc(bytes.fromhex("01 10 4e 21 00 06 0c") + b"".join(word.to_bytes(2, "big") for word in words))


# The clock write, at a made-up time: 2026-01-02 03:04:05.
CLOCK_WRITE = clock_write_at(time.struct_time((2026, 1, 2, 3, 4, 5, 4, 2, 0)))
# fn 6 to 20007, the language, value 1: not the clock. The fake module starts it at 0.
OUTSIDE_CLOCK_WRITE = append_crc(bytes.fromhex("01 06 4e 27 00 01"))
# The cloud's read of 11121 x3, always with a wrong CRC, 9d f4 for 5c 34.
BAD_CRC_READ = bytes.fromhex("01 03 2b 71 00 03 9d f4")

# The cloud's first poll after it connects: each request's time in seconds from the first, its kind and two numbers.
# "aa55": command and sub; "read": address and count; "clock": the clock write; "bad crc": the read with the wrong CRC.
FIRST_POLL: tuple[tuple[float, str, int, int], ...] = (
    (0.0, "aa55", 0x15, 0x00),
    (1.85, "aa55", 0x15, 0x0B),
    (1.93, "aa55", 0x15, 0x0B),
    (2.03, "aa55", 0x0E, 0x00),
    (4.16, "read", 10000, 32),
    (4.35, "read", 10176, 6),
    (4.5, "read", 10048, 80),
    (4.79, "read", 10032, 16),
    (4.96, "read", 10183, 3),
    (5.13, "read", 10128, 48),
    (5.37, "read", 10500, 64),
    (5.59, "read", 10600, 18),
    (15.71, "clock", 0, 0),
    (16.86, "read", 20000, 1),
    (17.0, "read", 20001, 6),
    (17.14, "read", 20007, 7),
    (17.32, "read", 20100, 14),
    (17.49, "read", 50028, 1),
    (17.69, "read", 20115, 1),
    (17.82, "read", 20116, 1),
    (17.95, "read", 50029, 1),
    (18.06, "read", 20122, 1),
    (18.24, "read", 20130, 1),
    (18.4, "read", 21022, 1),
    (18.52, "read", 50030, 1),
    (18.68, "read", 21000, 1),
    (18.82, "read", 21016, 6),
    (18.97, "read", 22000, 9),
    (19.11, "read", 22100, 11),
    (19.25, "read", 22200, 10),
    (19.41, "read", 23000, 11),
    (19.59, "read", 23100, 10),
    (19.79, "read", 23200, 26),
    (20.03, "read", 23300, 17),
    (20.21, "read", 23400, 2),
    (20.35, "read", 10200, 16),
    (20.51, "read", 20125, 5),
    (20.68, "read", 23620, 7),
    (20.93, "read", 23640, 7),
    (21.03, "read", 23660, 7),
    (21.22, "read", 20131, 21),
    (21.41, "read", 50031, 1),
    (21.56, "read", 50032, 1),
    (21.72, "read", 50035, 1),
    (23.04, "read", 11000, 114),
    (23.41, "read", 14000, 27),
    (23.64, "read", 21016, 4),
    (23.84, "read", 11124, 34),
    (25.2, "read", 20114, 1),
    (26.34, "read", 11114, 7),
    (27.46, "bad crc", 0, 0),
    (37.47, "read", 11011, 1),
    (38.1, "read", 10618, 2),
    (138.52, "aa55", 0x15, 0x02),
)

# The later polls on the same connection, every 5 minutes, the same numbers: no handshake but one 0x15/0x0B after the
# identity reads, no 10000 x32, no clock write; 47 reads; the keepalive 100 s after the last request.
LATER_POLL: tuple[tuple[float, str, int, int], ...] = (
    (0.0, "read", 10176, 6),
    (0.16, "read", 10048, 80),
    (0.47, "read", 10032, 16),
    (0.69, "read", 10183, 3),
    (0.88, "read", 10128, 48),
    (1.13, "read", 10500, 64),
    (1.29, "read", 10600, 18),
    (1.44, "aa55", 0x15, 0x0B),
    (1.57, "read", 20000, 1),
    (1.64, "read", 20001, 6),
    (1.79, "read", 20007, 7),
    (1.87, "read", 20100, 14),
    (1.94, "read", 50028, 1),
    (2.09, "read", 20115, 1),
    (2.22, "read", 20116, 1),
    (2.37, "read", 50029, 1),
    (2.55, "read", 20122, 1),
    (2.73, "read", 20130, 1),
    (2.88, "read", 21022, 1),
    (3.01, "read", 50030, 1),
    (3.57, "read", 21000, 1),
    (3.64, "read", 21016, 6),
    (3.72, "read", 22000, 9),
    (3.88, "read", 22100, 11),
    (4.08, "read", 22200, 10),
    (4.27, "read", 23000, 11),
    (4.43, "read", 23100, 10),
    (4.61, "read", 23200, 26),
    (4.82, "read", 23300, 17),
    (5.03, "read", 23400, 2),
    (5.18, "read", 10200, 16),
    (5.38, "read", 20125, 5),
    (5.54, "read", 23620, 7),
    (5.72, "read", 23640, 7),
    (5.88, "read", 23660, 7),
    (6.04, "read", 20131, 21),
    (6.26, "read", 50031, 1),
    (6.42, "read", 50032, 1),
    (6.59, "read", 50035, 1),
    (6.74, "read", 11000, 114),
    (6.85, "read", 14000, 27),
    (6.98, "read", 21016, 4),
    (7.07, "read", 11124, 34),
    (8.37, "read", 20114, 1),
    (9.91, "read", 11114, 7),
    (11.09, "bad crc", 0, 0),
    (21.09, "read", 11011, 1),
    (21.31, "read", 10618, 2),
    (121.57, "aa55", 0x15, 0x02),
)
# The cloud's time between the starts of two polls.
POLL_PERIOD_S = 300.0


@dataclass
class Exchange:
    """One frame the cloud sent and the reply it got; None when none came or none was waited for."""

    request: bytes
    reply: Frame | None


def request_bytes(kind: str, first: int, second: int) -> bytes:
    """The bytes of one request of `FIRST_POLL` or `LATER_POLL`."""
    if kind == "aa55":
        return build_request(first, second)
    if kind == "read":
        return build_read_request(first, second)
    if kind == "clock":
        return CLOCK_WRITE
    if kind == "bad crc":
        return BAD_CRC_READ
    raise ValueError(f"unknown request kind {kind!r}")


def cloud_frames() -> list[tuple[float, bytes]]:
    """The cloud's first poll, each request with its time from the first one, and the write outside the clock right
    after the clock write."""
    frames: list[tuple[float, bytes]] = []
    for at, kind, first, second in FIRST_POLL:
        frames.append((at, request_bytes(kind, first, second)))
        if kind == "clock":
            frames.append((at, OUTSIDE_CLOCK_WRITE))
    return frames


def later_frames() -> list[tuple[float, bytes]]:
    """A later poll, each request with its time from the first one."""
    return [(at, request_bytes(kind, first, second)) for at, kind, first, second in LATER_POLL]


def describe_request(request: bytes) -> str:
    try:
        return describe_frame(parse_frame(request, from_master=True))
    except FrameError:
        return f"{request.hex(' ')}, bad CRC"


def reply_matcher(request: bytes) -> Matcher | None:
    """What the reply to the request looks like; None for a request that gets no reply."""
    try:
        frame = parse_frame(request, from_master=True)
    except FrameError:
        return None
    if isinstance(frame, AA55Frame):
        return aa55_reply_matcher(frame) if (frame.command, frame.sub) in REPLIED else None
    if isinstance(frame, ModbusFrame) and frame.is_request:
        return modbus_reply_matcher(frame)
    return None


class MockCloud:
    """The server; `exchanges` holds what every played request got. `frames` is the first poll; `period_s` adds the
    later polls, each starting `period_s` after the one before or right after it when that one ran longer; None plays
    the first poll only. With `clock_now` the clock write carries the time it is sent at, the computer's local time."""

    def __init__(
        self,
        frames: Iterable[tuple[float, bytes]] | None = None,
        *,
        reply_timeout_s: float = 1.0,
        time_scale: float = 0.0,
        period_s: float | None = None,
        clock_now: bool = False,
    ) -> None:
        self.frames = list(frames) if frames is not None else cloud_frames()
        self.reply_timeout_s = reply_timeout_s
        self.time_scale = time_scale
        self.period_s = period_s
        self.clock_now = clock_now
        self.exchanges: list[Exchange] = []
        self.unsolicited: list[Frame] = []
        self.connections = 0
        self.polls = 0
        self.session_done = asyncio.Event()
        self.writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self._pending: dict[asyncio.StreamWriter, tuple[Matcher, asyncio.Future[Frame]]] = {}

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        """Listens; returns the port."""
        self._server = await asyncio.start_server(self._serve, host, port)
        return self.port

    async def stop(self) -> None:
        self.close_connections()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def close_connections(self) -> None:
        """Drops every connection, as the cloud going away would."""
        for writer in list(self.writers):
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        address = writer.get_extra_info("peername")
        peer = f"{address[0]}:{address[1]}"
        LOGGER.info("%s connected", peer)
        playing = asyncio.create_task(self._play_polls(writer))
        try:
            await self._read(reader, writer)
        except (OSError, ConnectionError):
            pass
        finally:
            playing.cancel()
            await asyncio.gather(playing, return_exceptions=True)
            self._pending.pop(writer, None)
            if writer in self.writers:
                self.writers.remove(writer)
            writer.close()
            LOGGER.info("%s gone", peer)

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        framer = Framer(from_master=False)
        while True:
            data = await reader.read(4096)
            if not data:
                return
            for frame in framer.feed(data, time.monotonic()):
                pending = self._pending.get(writer)
                if pending is not None and not pending[1].done() and pending[0](frame):
                    pending[1].set_result(frame)
                else:
                    self.unsolicited.append(frame)
                    LOGGER.debug("unsolicited: %s", describe_frame(frame))

    async def _play_polls(self, writer: asyncio.StreamWriter) -> None:
        """The first poll, then the later ones every `period_s`, until the connection ends."""
        start = time.monotonic()
        await self._play(writer, self.frames)
        self.session_done.set()
        if self.period_s is None:
            return
        later = later_frames()
        while True:
            start += self.period_s
            wait = start - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                start = time.monotonic()
            await self._play(writer, later)

    async def _play(self, writer: asyncio.StreamWriter, frames: list[tuple[float, bytes]]) -> None:
        """One poll: each request at its time, then its reply waited for."""
        self.polls += 1
        LOGGER.info("poll %d", self.polls)
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        for offset, request in frames:
            wait = start + offset * self.time_scale - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if self.clock_now and request == CLOCK_WRITE:
                request = clock_write_at(time.localtime())
            matches = reply_matcher(request)
            future: asyncio.Future[Frame] = loop.create_future()
            if matches is not None:
                self._pending[writer] = (matches, future)
            writer.write(request)
            await writer.drain()
            reply = None
            if matches is not None:
                with contextlib.suppress(TimeoutError):
                    reply = await asyncio.wait_for(future, self.reply_timeout_s)
            self._pending.pop(writer, None)
            self.exchanges.append(Exchange(request, reply))
            answer = describe_frame(reply) if reply is not None else "no reply" if matches is not None else "none expected"
            LOGGER.info("%s -> %s", describe_request(request), answer)


async def run_standalone(args: argparse.Namespace) -> None:
    cloud = MockCloud(time_scale=1.0, period_s=args.period, clock_now=True)
    port = await cloud.start(args.port, args.bind)
    LOGGER.info("mock cloud on %s:%d, a poll every %g s", args.bind, port, args.period)
    try:
        await asyncio.Event().wait()
    finally:
        await cloud.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="A cloud on this computer: the cloud's polls to whoever connects, every request and its reply logged.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=20146)
    parser.add_argument("--period", type=float, default=POLL_PERIOD_S, help=f"seconds between the starts of two polls (default {POLL_PERIOD_S:g}, the cloud's)")
    parser.add_argument("-v", "--verbose", action="store_true", help="also log the frames no request waits for")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_standalone(args))


if __name__ == "__main__":
    main()
