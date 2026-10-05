"""The AT session (PLAN §6.11) against the fake module's AT side on 127.0.0.1: the discovery, the handshake, reading and
setting socket B, the checks before a set, a set sent once, a module that does not answer; the fake answers as the
real module sometimes does, every reply several times and in pieces."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest

from renac_protocol.at_session import AtError, AtSession, FoundModule, SocketB, discover
from tests.mocks.mock_hf_lpt270 import (
    AT_REPLY_END,
    DEFAULT_NAME,
    AtServer,
    FakeModule,
    Identity,
    SimulatedInverter,
    TcpModule,
    initial_words,
    random_identity,
)

FAST = {"reply_timeout_s": 0.5, "quiet_s": 0.05, "gap_s": 0.01}
CLOUD = SocketB("127.0.0.1", 20146)


class UnsteadyAtServer(AtServer):
    """The fake's AT side answering every datagram three times, a command's reply in two datagrams, as the real module
    sometimes does; it keeps what it received. `set_reply`, when given, is what a set gets instead of the fake's own
    reply, b"" for nothing."""

    copies = 3
    set_reply: bytes | None = None

    def __init__(self, socket_b: TcpModule, identity: Identity) -> None:
        super().__init__(socket_b, identity)
        self.received: list[bytes] = []

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        self.received.append(data)
        reply = self.answer(data, address)
        if data.startswith(b"AT+SOCKB=") and self.set_reply is not None:
            reply = self.set_reply or None
        if reply is None or self.transport is None:
            return
        halves = [reply[: len(reply) // 2], reply[len(reply) // 2 :]] if reply.endswith(AT_REPLY_END.encode()) else [reply]
        for _ in range(self.copies):
            for half in halves:
                self.transport.sendto(half, address)

    @property
    def sets(self) -> list[bytes]:
        return [data for data in self.received if data.startswith(b"AT+SOCKB=")]


@pytest.fixture
async def module() -> AsyncIterator[tuple[UnsteadyAtServer, TcpModule, int]]:
    identity = random_identity()
    socket_b = TcpModule(FakeModule(SimulatedInverter(initial_words(identity)), identity=identity), CLOUD.host, CLOUD.port)
    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(lambda: UnsteadyAtServer(socket_b, identity), local_addr=("127.0.0.1", 0))
    yield server, socket_b, transport.get_extra_info("sockname")[1]
    transport.close()


def nobody_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_discovery_finds_each_module_once(module: tuple[UnsteadyAtServer, TcpModule, int]) -> None:
    server, _, port = module
    found = await discover("127.0.0.1", port, timeout_s=0.3)
    assert found == [FoundModule("127.0.0.1", server.identity.mac.replace(":", ""), DEFAULT_NAME)]
    assert server.received == [b"HF-A11ASSISTHREAD"]


async def test_a_module_that_does_not_answer() -> None:
    port = nobody_port()
    assert await discover("127.0.0.1", port, tries=2, timeout_s=0.1) == []
    with pytest.raises(AtError, match="no reply"):
        await AtSession("127.0.0.1", port, **FAST).open()


async def test_socket_b_is_read_and_set_and_the_session_ended(module: tuple[UnsteadyAtServer, TcpModule, int]) -> None:
    server, socket_b, port = module
    ha = SocketB("ha.example", 20145)
    async with AtSession("127.0.0.1", port, **FAST) as session:
        assert session.module == FoundModule("127.0.0.1", server.identity.mac.replace(":", ""), DEFAULT_NAME)
        assert await session.read_socket_b() == CLOUD
        assert await session.set_socket_b(ha) == ha
        assert (socket_b.host, socket_b.port) == (ha.host, ha.port)
        # What socket B already holds sends nothing.
        assert await session.set_socket_b(ha) == ha
    assert server.sets == [b"AT+SOCKB=TCP,20145,ha.example\r"]
    assert server.received[-1] == b"AT+Q\r" and not server.in_at_mode


async def test_a_value_that_may_not_be_sent_sends_nothing(module: tuple[UnsteadyAtServer, TcpModule, int]) -> None:
    server, socket_b, port = module
    refused = (SocketB("ha,example", 20145), SocketB("1.2.3", 20145), SocketB("h" * 101, 20145), SocketB("ha.example", 0), SocketB("ha.example", 1, "UDP"))
    async with AtSession("127.0.0.1", port, **FAST) as session:
        for value in refused:
            with pytest.raises(ValueError):
                await session.set_socket_b(value)
    assert server.sets == [] and (socket_b.host, socket_b.port) == (CLOUD.host, CLOUD.port)
    assert SocketB.from_address("192.168.1.20:20145") == SocketB("192.168.1.20", 20145)
    with pytest.raises(ValueError):
        SocketB.from_address("ha.example")


@pytest.mark.parametrize("reply", [b"", f"+ERR=-5{AT_REPLY_END}".encode()], ids=["no reply", "an error"])
async def test_a_set_the_module_does_not_take_is_sent_once(module: tuple[UnsteadyAtServer, TcpModule, int], reply: bytes) -> None:
    server, _, port = module
    server.set_reply = reply
    async with AtSession("127.0.0.1", port, **FAST) as session:
        with pytest.raises(AtError):
            await session.set_socket_b(SocketB("ha.example", 20145))
    assert len(server.sets) == 1
