"""The fake HF-LPT270 as the other tests, the compose instance and the user rely on it: every group read answered, the
reads it refuses, the simulated values consistent; its AT side, its link in place of Bluetooth, and the one UART that
both paths share, on 127.0.0.1."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import pytest

from renac_protocol.frames import ModbusKind, build_read_request, parse_aa55, parse_modbus
from renac_protocol.groups import BMS_SERIAL_RANGES, GROUPS, LIVE_GROUP, POWER32_GROUP, UNDOCUMENTED_GROUP
from renac_protocol.handshake import MODULE_INFO, build_request
from renac_protocol.register_table import PHASES
from renac_protocol.registers import Block, decode_block
from tests.mocks.mock_hf_lpt270 import (
    AT_GREETING,
    DEFAULT_NAME,
    INVERTER_STATE_NORMAL,
    INVERTER_STATE_WAIT,
    AtServer,
    FakeModule,
    Identity,
    LinkServer,
    SimulatedInverter,
    TcpModule,
    initial_words,
    random_identity,
)


def make_module(identity: Identity | None = None, **options: float) -> FakeModule:
    identity = identity or random_identity()
    return FakeModule(SimulatedInverter(initial_words(identity), **options), identity=identity)


def read(module: FakeModule, address: int, count: int):  # noqa: ANN201
    answered = module.handle(build_read_request(address, count), time.monotonic())
    assert len(answered) == 1
    reply = answered[0][1]
    assert reply is not None
    return parse_modbus(reply, from_master=False)


def test_every_group_read_is_answered_and_decodes() -> None:
    module = make_module()
    for group in GROUPS:
        if group is UNDOCUMENTED_GROUP:
            continue
        for block in group.ranges:
            reply = read(module, block.address, block.count)
            assert reply.kind is ModbusKind.READ_REPLY and reply.count == block.count, (group.key, block)
            if block in BMS_SERIAL_RANGES:
                continue
            assert decode_block(Block(block.address, reply.registers()), group.registers), (group.key, block)


def test_undocumented_and_unknown_ranges_get_exception_02() -> None:
    module = make_module()
    for address, count in ((20114, 1), (50028, 1), (10500, 64), (11000, 125), (9999, 2)):
        reply = read(module, address, count)
        assert reply.is_exception and reply.exception_code == 2, (address, count)


def test_the_values_move_and_stay_consistent() -> None:
    inverter = SimulatedInverter(initial_words(random_identity()), day_s=100.0)
    seen_consumptions = set()
    for _ in range(20):
        inverter.advance(1.0)
        readings = decode_block(Block(11000, tuple(inverter.words[a] for a in range(11000, 11114))), LIVE_GROUP.registers)
        mirrors = decode_block(Block(11124, tuple(inverter.words[a] for a in range(11124, 11158))), POWER32_GROUP.registers)
        output = sum(readings[f"inverter_power_{p}"].value for p in PHASES)
        consumption = sum(readings[f"consumption_power_{p}"].value for p in PHASES)
        meter = sum(readings[f"meter1_power_{p}"].value for p in PHASES)
        assert meter == consumption - output
        assert readings["consumption_power"].value == consumption == mirrors["consumption_power"].value
        assert readings["meter1_power"].value == meter == mirrors["meter1_power"].value
        assert mirrors["battery_power"].value == readings["battery_power"].value
        assert readings["inverter_state"].value == (INVERTER_STATE_NORMAL if output > 0 else INVERTER_STATE_WAIT)
        seen_consumptions.add(consumption)
    assert len(seen_consumptions) > 5


def test_the_at_session() -> None:
    identity = random_identity()
    socket_b = TcpModule(make_module(identity), "127.0.0.1", 20146)
    at = AtServer(socket_b, identity)
    me, other = ("127.0.0.1", 50001), ("127.0.0.1", 50002)

    assert at.answer(b"AT+SOCKB\r", me) is None
    assert at.answer(AT_GREETING, me) == f"127.0.0.1,{identity.mac.replace(':', '')},{DEFAULT_NAME}".encode()
    assert at.answer(b"AT+SOCKB\r", me) is None
    assert at.answer(b"+ok", me) is None
    assert at.answer(b"AT+SOCKB\r", me) == b"+ok=TCP,20146,127.0.0.1\r\n\r\n"
    assert at.answer(b"AT+SOCKB", me) is None
    assert at.answer(b"AT+SOCKB\n", me) is None
    assert at.answer(b"AT+SOCKB\r", other) is None
    assert at.answer(b"AT+WSCAN\r", me) == b"+ERR=-2\r\n\r\n"
    assert at.answer(b"HELLO\r", me) == b"+ERR=-1\r\n\r\n"
    assert at.answer(b"AT+SOCKB=UDP,20145,127.0.0.1\r", me) == b"+ERR=-4\r\n\r\n"
    assert (socket_b.host, socket_b.port) == ("127.0.0.1", 20146)

    assert at.answer(b"AT+SOCKB=TCP,20145,ha.example\r", me) == b"+ok\r\n\r\n"
    assert (socket_b.host, socket_b.port) == ("ha.example", 20145)
    assert at.answer(b"AT+SOCKB\r", me) == b"+ok=TCP,20145,ha.example\r\n\r\n"
    assert at.answer(b"AT+Q\r", me) is None
    assert at.answer(b"AT+SOCKB\r", me) is None


class AtClient(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        self.replies.put_nowait(data)

    async def ask(self, data: bytes, timeout_s: float = 1.0) -> bytes | None:
        assert self.transport is not None
        self.transport.sendto(data)
        try:
            return await asyncio.wait_for(self.replies.get(), timeout_s)
        except TimeoutError:
            return None


class Listener:
    """A TCP server on 127.0.0.1 that keeps every connection and what it sends."""

    def __init__(self) -> None:
        self.connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._keep, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _keep(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.append((reader, writer))

    async def stop(self) -> None:
        for _, writer in self.connections:
            writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def wait_until(condition, timeout_s: float = 3.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def read_exactly(reader: asyncio.StreamReader, count: int, timeout_s: float = 2.0) -> bytes:
    return await asyncio.wait_for(reader.readexactly(count), timeout_s)


async def read_nothing(reader: asyncio.StreamReader, timeout_s: float = 0.3) -> bool:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(reader.read(1), timeout_s)
        return False
    return True


@pytest.fixture
async def listeners() -> AsyncIterator[tuple[Listener, Listener]]:
    first, second = Listener(), Listener()
    await first.start()
    await second.start()
    yield first, second
    await first.stop()
    await second.stop()


async def test_an_at_sockb_set_over_udp_moves_socket_b_at_once(listeners: tuple[Listener, Listener]) -> None:
    first, second = listeners
    assert first.server is not None and second.server is not None
    first_port = first.server.sockets[0].getsockname()[1]
    second_port = second.server.sockets[0].getsockname()[1]
    identity = random_identity()
    socket_b = TcpModule(make_module(identity), "127.0.0.1", first_port, reconnect_s=30)
    loop = asyncio.get_running_loop()
    at_transport, _ = await loop.create_datagram_endpoint(lambda: AtServer(socket_b, identity), local_addr=("127.0.0.1", 0))
    at_port = at_transport.get_extra_info("sockname")[1]
    client_transport, client = await loop.create_datagram_endpoint(AtClient, remote_addr=("127.0.0.1", at_port))
    running = asyncio.create_task(socket_b.run())
    try:
        await wait_until(lambda: len(first.connections) == 1 and socket_b.is_connected)
        assert (await client.ask(AT_GREETING) or b"").startswith(b"127.0.0.1,")
        assert await client.ask(b"+ok", timeout_s=0.3) is None
        assert await client.ask(f"AT+SOCKB=TCP,{second_port},127.0.0.1\r".encode()) == b"+ok\r\n\r\n"
        # At once: far sooner than the 30 s a drop would wait.
        await wait_until(lambda: len(second.connections) == 1, timeout_s=2.0)
        # The first connection was dropped.
        assert await asyncio.wait_for(first.connections[0][0].read(), 1.0) == b""
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        client_transport.close()
        at_transport.close()


async def test_the_link_answers_modbus_in_pieces_and_ignores_aa55() -> None:
    module = make_module()
    link = LinkServer(module, fragment=7)
    port = await link.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await wait_until(lambda: len(link.writers) == 1)
        expected = make_module().reply_to(parse_modbus(build_read_request(20007, 7), from_master=True))
        assert expected is not None
        writer.write(build_read_request(20007, 7))
        await writer.drain()
        assert await read_exactly(reader, len(expected)) == expected
        writer.write(build_request(*MODULE_INFO))
        await writer.drain()
        assert await read_nothing(reader)
    finally:
        writer.close()
        await link.stop()


async def test_one_uart_the_replies_go_out_on_both_paths(listeners: tuple[Listener, Listener]) -> None:
    server, _ = listeners
    assert server.server is not None
    module = FakeModule(SimulatedInverter(initial_words(random_identity())), answers_handshake=True)
    link = LinkServer(module)
    socket_b = TcpModule(module, "127.0.0.1", server.server.sockets[0].getsockname()[1])
    socket_b.link = link
    link.socket_b = socket_b
    port = await link.start()
    running = asyncio.create_task(socket_b.run())
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await wait_until(lambda: len(server.connections) == 1 and socket_b.is_connected and len(link.writers) == 1)
        cloud_reader, cloud_writer = server.connections[0]

        # The cloud's read: its reply on socket B and on the link.
        request = build_read_request(10176, 6)
        cloud_writer.write(request)
        await cloud_writer.drain()
        expected = module.reply_to(parse_modbus(request, from_master=True))
        assert expected is not None
        reply = await read_exactly(cloud_reader, len(expected))
        assert await read_exactly(reader, len(reply)) == reply

        # The handshake is the module's own: on socket B only.
        cloud_writer.write(build_request(*MODULE_INFO))
        await cloud_writer.drain()
        head = await read_exactly(cloud_reader, 9)
        parse_aa55(head + await read_exactly(cloud_reader, head[8] + 2))
        assert await read_nothing(reader)

        # The link's read: its reply on the link and on socket B.
        writer.write(build_read_request(20000, 1))
        await writer.drain()
        reply = await read_exactly(reader, 7)
        assert await read_exactly(cloud_reader, 7) == reply
    finally:
        writer.close()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await link.stop()
