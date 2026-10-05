"""A TCP server that behaves like the relay with the module behind it, for client mode tests on 127.0.0.1.

Every client's bytes are parsed as requests. A request in `replies` gets that reply; a fn 3 read of registers that are
all in the word table gets a reply built from it; any other fn 3 read with a valid CRC gets exception 02; anything
else, a bad CRC included, gets nothing. A reply goes out after a latency, broadcast to every client. Optionally the
replies carry leading padding, are split into two writes, or the accept is delayed as the relay's on-demand BLE
connect would. Unsolicited frames, the cloud's replies on the shared link, can go out every N seconds.

With `module` it answers nothing itself but bridges to the fake module's link (`mock_hf_lpt270.py --link`) as the
relay bridges to BLE: its clients' bytes go to the module whole, the module's bytes to every client as they come. It
keeps that connection, dialling again after a drop; a client's bytes while it is down are dropped.

Standalone it answers from random words for every register the poll groups read, the values meaning nothing, or with
--module it bridges:

    python -m tests.mocks.mock_relay --port 18899
    python -m tests.mocks.mock_relay --port 8899 --module 127.0.0.1:18898
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "renac_local"))

from renac_protocol.crc import append_crc  # noqa: E402
from renac_protocol.framer import Framer  # noqa: E402
from renac_protocol.frames import (  # noqa: E402
    EXCEPTION_BIT,
    READ,
    UNIT_ID,
    FrameError,
    ModbusFrame,
    ModbusKind,
    build_read_reply,
    describe_frame,
)
from renac_protocol.groups import GROUPS  # noqa: E402

LOGGER = logging.getLogger("mock_relay")

ILLEGAL_ADDRESS = append_crc(bytes([UNIT_ID, READ | EXCEPTION_BIT, 2]))
# Bridging: seconds before the module's link is dialled again after a drop or a failed dial.
LINK_RETRY_S = 2.0


def random_words() -> dict[int, int]:
    """A random word for every register any poll group reads, the 32-bit block included."""
    return {
        address: random.randrange(0x10000)
        for group in GROUPS
        for block_range in group.ranges
        for address in range(block_range.address, block_range.address + block_range.count)
    }


def read_replies(words: Mapping[int, int]) -> list[bytes]:
    """The replies to every poll group read the words hold, in the groups' order: traffic as the cloud's polls make it."""
    replies = []
    for group in GROUPS:
        for block_range in group.ranges:
            addresses = range(block_range.address, block_range.address + block_range.count)
            if all(address in words for address in addresses):
                replies.append(build_read_reply(tuple(words[address] for address in addresses)))
    return replies


class MockRelay:
    """The server. `words` is the inverter's registers by address; `replies` maps request bytes to reply bytes and wins
    over the words. `module`, the fake module's link as (host, port), makes it a bridge instead."""

    def __init__(
        self,
        words: Mapping[int, int] | None = None,
        *,
        replies: Mapping[bytes, bytes] | None = None,
        module: tuple[str, int] | None = None,
        latency_s: float = 0.02,
        accept_delay_s: float = 0.0,
        padding: bytes = b"",
        split_at: int | None = None,
        drop: Iterable[bytes] = (),
        unsolicited_frames: Iterable[bytes] = (),
        unsolicited_every_s: float | None = None,
    ) -> None:
        self.module = module
        self.words = dict(words or {})
        self.replies = dict(replies or {})
        self.latency_s = latency_s
        self.accept_delay_s = accept_delay_s
        self.padding = padding
        self.split_at = split_at
        self.drop = set(drop)
        self.unsolicited_frames = list(unsolicited_frames)
        self.unsolicited_every_s = unsolicited_every_s
        self.requests: list[ModbusFrame] = []
        self.clients: list[asyncio.StreamWriter] = []
        self.accepted = 0
        self.on_request: Callable[[ModbusFrame], None] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._unsolicited_task: asyncio.Task[None] | None = None
        self._unsolicited_index = 0
        self._link_task: asyncio.Task[None] | None = None
        self._link: asyncio.StreamWriter | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    @property
    def link_up(self) -> bool:
        """Bridging: the connection to the module's link is there."""
        return self._link is not None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Listens; returns the port."""
        self._server = await asyncio.start_server(self._serve, host, port)
        if self.unsolicited_every_s and self.unsolicited_frames:
            self._unsolicited_task = asyncio.create_task(self._send_unsolicited_every())
        if self.module is not None:
            self._link_task = asyncio.create_task(self._keep_link(*self.module))
        return self.port

    async def stop(self) -> None:
        for task in (self._unsolicited_task, self._link_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._unsolicited_task = None
        self._link_task = None
        await self.close_clients()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def close_clients(self) -> None:
        """Drops every client, as a lost BLE link with close_tcp_on_ble_loss would."""
        for writer in list(self.clients):
            writer.close()
        await asyncio.sleep(0)

    async def broadcast(self, data: bytes) -> None:
        """Sends the bytes to every client, with the configured padding and split."""
        data = self.padding + data
        for writer in list(self.clients):
            try:
                if self.split_at is not None and len(data) > self.split_at:
                    writer.write(data[: self.split_at])
                    await writer.drain()
                    await asyncio.sleep(0.005)
                    writer.write(data[self.split_at :])
                else:
                    writer.write(data)
                await writer.drain()
            except (OSError, ConnectionError):
                pass

    async def send_unsolicited(self) -> bytes:
        """Broadcasts the next unsolicited frame; returns it."""
        frame = self.unsolicited_frames[self._unsolicited_index % len(self.unsolicited_frames)]
        self._unsolicited_index += 1
        await self.broadcast(frame)
        return frame

    async def _send_unsolicited_every(self) -> None:
        assert self.unsolicited_every_s is not None
        while True:
            await asyncio.sleep(self.unsolicited_every_s)
            if self.clients:
                await self.send_unsolicited()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.accept_delay_s:
            await asyncio.sleep(self.accept_delay_s)
        self.accepted += 1
        self.clients.append(writer)
        framer = Framer(from_master=True)
        LOGGER.info("client %s connected", writer.get_extra_info("peername"))
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                if self.module is not None:
                    await self._to_module(data)
                for frame in framer.feed(data, time.monotonic()):
                    if isinstance(frame, ModbusFrame):
                        self.requests.append(frame)
                        LOGGER.info("request %s", describe_frame(frame))
                        if self.on_request is not None:
                            self.on_request(frame)
                        if self.module is None:
                            asyncio.create_task(self._answer(frame))
        except (OSError, ConnectionError):
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)
            writer.close()
            LOGGER.info("client gone")

    async def _keep_link(self, host: str, port: int) -> None:
        """Bridging: the connection to the module's link, kept as the relay keeps its BLE link, dialled again after a
        drop; the module's bytes go to every client as they come."""
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError as error:
                LOGGER.warning("module link %s:%d: %s; dialling again in %g s", host, port, error, LINK_RETRY_S)
            else:
                self._link = writer
                LOGGER.warning("module link %s:%d up", host, port)
                try:
                    while data := await reader.read(4096):
                        await self.broadcast(data)
                except (OSError, ConnectionError):
                    pass
                finally:
                    self._link = None
                    writer.close()
                LOGGER.warning("module link %s:%d lost; dialling again in %g s", host, port, LINK_RETRY_S)
            await asyncio.sleep(LINK_RETRY_S)

    async def _to_module(self, data: bytes) -> None:
        """Bridging: a client's bytes to the module, whole."""
        link = self._link
        if link is None:
            LOGGER.warning("no module link: %d B from a client dropped", len(data))
            return
        with contextlib.suppress(OSError, ConnectionError):
            link.write(data)
            await link.drain()

    def read_reply(self, address: int, count: int) -> bytes:
        """The reply to a fn 3 read from the words; exception 02 when any of the registers is not among them."""
        addresses = range(address, address + count)
        if not all(register in self.words for register in addresses):
            return ILLEGAL_ADDRESS
        return build_read_reply(tuple(self.words[register] for register in addresses))

    async def _answer(self, request: ModbusFrame) -> None:
        if request.raw in self.drop:
            return
        reply = self.replies.get(request.raw)
        if reply is None:
            if request.kind is not ModbusKind.READ_REQUEST:
                return
            reply = self.read_reply(request.address or 0, request.count or 0)
        await asyncio.sleep(self.latency_s)
        await self.broadcast(reply)


async def run_standalone(args: argparse.Namespace) -> None:
    padding = bytes.fromhex(args.padding) if args.padding else b""
    if args.module is not None:
        relay = MockRelay(module=args.module, accept_delay_s=args.accept_delay, padding=padding, split_at=args.split_at)
        described = "bridging to the module's link at {}:{}".format(*args.module)
    else:
        words = random_words()
        relay = MockRelay(
            words,
            latency_s=args.latency,
            accept_delay_s=args.accept_delay,
            padding=padding,
            split_at=args.split_at,
            unsolicited_frames=read_replies(words),
            unsolicited_every_s=args.unsolicited or None,
        )
        described = f"replies from random words, latency {args.latency:g} s"
    port = await relay.start(args.bind, args.port)
    print(f"mock relay on {args.bind}:{port}, {described}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await relay.stop()


def host_and_port(text: str) -> tuple[str, int]:
    """HOST:PORT as a pair, for argparse."""
    host, separator, port = text.rpartition(":")
    if not separator or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"{text!r} is not HOST:PORT")
    return host, int(port)


def main() -> None:
    parser = argparse.ArgumentParser(description="A TCP server behaving like the relay with the inverter's module behind it.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18899)
    parser.add_argument(
        "--module", type=host_and_port, metavar="HOST:PORT", help="bridge to the fake module's link at HOST:PORT instead of answering from random words"
    )
    parser.add_argument("--latency", type=float, default=0.06, help="seconds between a request and its reply; not with --module")
    parser.add_argument("--accept-delay", type=float, default=0.0, help="seconds before a new client is served, as an on-demand BLE connect")
    parser.add_argument("--padding", default="", help="hex bytes sent before every reply, e.g. ff00")
    parser.add_argument("--split-at", type=int, default=None, help="split every reply into two writes at this byte")
    parser.add_argument("--unsolicited", type=float, default=0.0, help="seconds between unsolicited replies, 0 for none; not with --module")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.module is not None and args.unsolicited:
        parser.error("--unsolicited needs the random words, not --module: the module's link carries the cloud's traffic itself")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_standalone(args))


if __name__ == "__main__":
    try:
        main()
    except FrameError as error:
        raise SystemExit(f"bad frame: {error}") from error
