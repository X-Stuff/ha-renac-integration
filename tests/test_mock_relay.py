"""The mock relay bridging to the fake module's link, as the compose test instance runs them."""

from __future__ import annotations

import asyncio
import time

from renac_protocol.frames import build_read_request, parse_modbus
from tests.mocks.mock_hf_lpt270 import FakeModule, LinkServer, SimulatedInverter, initial_words, random_identity
from tests.mocks.mock_relay import MockRelay


async def wait_until(condition, timeout_s: float = 3.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_the_bridge_carries_requests_to_the_module_and_replies_to_every_client() -> None:
    module = FakeModule(SimulatedInverter(initial_words(random_identity())))
    link = LinkServer(module, fragment=5)
    link_port = await link.start()
    relay = MockRelay(module=("127.0.0.1", link_port))
    port = await relay.start()
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await wait_until(lambda: relay.link_up and len(relay.clients) == 2)
        request = build_read_request(10176, 6)
        expected = module.reply_to(parse_modbus(request, from_master=True))
        assert expected is not None
        first_writer.write(request)
        await first_writer.drain()
        assert await asyncio.wait_for(first_reader.readexactly(len(expected)), 2) == expected
        assert await asyncio.wait_for(second_reader.readexactly(len(expected)), 2) == expected
        assert [request.address for request in relay.requests] == [10176]
    finally:
        first_writer.close()
        second_writer.close()
        await relay.stop()
        await link.stop()


async def test_without_the_module_a_clients_bytes_are_dropped_and_the_link_comes_later() -> None:
    module = FakeModule(SimulatedInverter(initial_words(random_identity())))
    link = LinkServer(module)
    link_port = await link.start()
    await link.stop()
    relay = MockRelay(module=("127.0.0.1", link_port))
    port = await relay.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await wait_until(lambda: len(relay.clients) == 1)
        writer.write(build_read_request(10176, 6))
        await writer.drain()
        await wait_until(lambda: len(relay.requests) == 1)
        assert not relay.link_up

        await link.start(port=link_port)
        await wait_until(lambda: relay.link_up, timeout_s=5.0)
        request = build_read_request(20000, 1)
        expected = module.reply_to(parse_modbus(request, from_master=True))
        assert expected is not None
        writer.write(request)
        await writer.drain()
        # The first bytes the client gets are this reply: the dropped request was never answered.
        assert await asyncio.wait_for(reader.readexactly(len(expected)), 2) == expected
    finally:
        writer.close()
        await relay.stop()
        await link.stop()
