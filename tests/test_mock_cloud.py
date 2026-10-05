"""The mock cloud as the compose instance runs it: its later polls and its clock, with the fake module answering it."""

from __future__ import annotations

import asyncio
import calendar
import time

from renac_protocol.frames import ModbusFrame, parse_modbus
from tests.mocks.mock_cloud import CLOCK_WRITE, MockCloud, later_frames
from tests.mocks.mock_hf_lpt270 import FakeModule, SimulatedInverter, TcpModule, initial_words, random_identity


async def wait_until(condition, timeout_s: float = 3.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_the_later_polls_follow_every_period_on_the_same_connection() -> None:
    cloud = MockCloud(period_s=0.3)
    port = await cloud.start()
    identity = random_identity()
    module = TcpModule(FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True), "127.0.0.1", port)
    running = asyncio.create_task(module.run())
    try:
        await wait_until(lambda: cloud.polls >= 3)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await cloud.stop()

    assert cloud.connections == 1
    requests = [exchange.request for exchange in cloud.exchanges]
    assert requests[: len(cloud.frames)] == [request for _, request in cloud.frames]
    later = [request for _, request in later_frames()]
    assert requests[len(cloud.frames) : len(cloud.frames) + len(later)] == later
    # Every read of a register the fake holds was answered, in every poll.
    identity_reads = [exchange for exchange in cloud.exchanges if exchange.request == later[0]]
    assert len(identity_reads) >= 2 and all(isinstance(exchange.reply, ModbusFrame) for exchange in identity_reads)


async def test_clock_now_writes_the_time_it_is_sent_at() -> None:
    cloud = MockCloud(clock_now=True)
    port = await cloud.start()
    identity = random_identity()
    module = TcpModule(FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True), "127.0.0.1", port)
    running = asyncio.create_task(module.run())
    try:
        await wait_until(cloud.session_done.is_set)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await cloud.stop()

    writes = [exchange for exchange in cloud.exchanges if exchange.request[1] == 0x10]
    assert len(writes) == 1 and writes[0].request != CLOCK_WRITE
    year, month, day, hour, minute, second = parse_modbus(writes[0].request, from_master=True).registers()
    written = calendar.timegm((2000 + year, month, day, hour, minute, second, 0, 0, 0))
    now = calendar.timegm(time.localtime())
    assert abs(written - now) <= 2
    assert writes[0].reply is not None
