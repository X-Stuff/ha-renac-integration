"""The cloud session with the mock cloud, under a poller in server mode with the fake module dialling in: its reads
answered from the inverter or the module, its priority against the polls, its clock write and the words, a blocked write
and its option, the bad-CRC frame and its option, a refused module's cloud, the cloud down, dropped and closed, the
caller's own way of connecting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from renac_protocol.cloud_session import CloudAddress, CloudSession, CloudStatus, CloudWrite
from renac_protocol.crc import append_crc
from renac_protocol.frames import AA55Frame, ModbusFrame, build_read_request, parse_modbus
from renac_protocol.handshake import HANDSHAKE, KEEPALIVE, build_request
from renac_protocol.inverter import Inverter
from renac_protocol.poller import Poller
from tests.helpers import PollerSetup, free_port, modbus_reads, wait_until
from tests.mocks.mock_cloud import BAD_CRC_READ, CLOCK_WRITE, OUTSIDE_CLOCK_WRITE, Exchange, MockCloud, reply_matcher
from tests.mocks.mock_hf_lpt270 import TcpModule
from tests.mocks.mock_relay import read_replies


@pytest.fixture
async def setup() -> AsyncIterator[PollerSetup]:
    value = PollerSetup()
    yield value
    await value.stop()


def default_groups_done(poller: Poller) -> bool:
    return all(key in poller.inverter.results for key in ("identity", "settings", "live", "energy"))


def cloud_of(poller: Poller) -> CloudSession:
    assert poller.cloud is not None
    return poller.cloud


def exchange_of(cloud: MockCloud, request: bytes) -> Exchange:
    return next(exchange for exchange in cloud.exchanges if exchange.request == request)


def module_raw(module: TcpModule) -> list[bytes]:
    return [frame.raw for frame in module.requests]


def read_reply(cloud: MockCloud, request: bytes) -> ModbusFrame:
    reply = exchange_of(cloud, request).reply
    assert isinstance(reply, ModbusFrame), request.hex()
    return reply


async def test_the_clouds_first_poll_gets_its_answers_and_the_polling_goes_on(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port), settle_s=0.3)
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    await wait_until(lambda: default_groups_done(poller) and build_request(*KEEPALIVE) in module_raw(module))
    session = cloud_of(poller)

    assert session.status == CloudStatus(address=f"127.0.0.1:{cloud.port}", connected=True)
    # The cloud was there before the settle time was up: the module saw the cloud's handshake only, no second one.
    assert [(frame.command, frame.sub) for frame in module.requests if isinstance(frame, AA55Frame)] == [*HANDSHAKE, KEEPALIVE]

    # Every request that gets an answer got one; the blocked write did not.
    for exchange in cloud.exchanges:
        expected = reply_matcher(exchange.request) is not None and exchange.request != OUTSIDE_CLOCK_WRITE
        assert (exchange.reply is not None) == expected, exchange.request.hex()
    # The module's answers to the cloud's handshake give the poller the module's identity.
    assert poller.module.module_serial == module.module.identity.module_serial


async def test_the_clouds_reads_of_polled_registers_are_answered_from_the_inverter(setup: PollerSetup) -> None:
    reads = [
        build_read_request(11000, 114),
        build_read_request(14000, 27),
        build_read_request(10000, 32),
        build_read_request(11010, 5),
        build_read_request(10048, 80),
    ]
    cloud = await setup.start_cloud(frames=[(0.5, request) for request in reads], time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    session = cloud_of(poller)
    assert session.counters.answered_from_state == len(reads) and session.counters.forwarded == 0
    assert modbus_reads(module).count((11000, 114)) == 1
    for request in reads:
        frame = parse_modbus(request, from_master=True)
        assert read_reply(cloud, request).registers() == poller.inverter.valid_words(frame.address or 0, frame.count or 0)


async def test_the_clouds_other_reads_go_to_the_module_and_exceptions_are_not_kept(setup: PollerSetup) -> None:
    # The 32-bit block, not polled without the power32 group.
    unread = build_read_request(11124, 34)
    refused = build_read_request(10500, 64)
    frames = [(0.5, unread), (0.5, refused), (0.5, refused), (0.5, build_read_request(11110, 20))]
    cloud = await setup.start_cloud(frames=frames, time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert read_reply(cloud, unread).count == 34
    assert all(exchange.reply is not None and exchange.reply.is_exception for exchange in cloud.exchanges if exchange.request == refused)  # type: ignore[union-attr]
    reads = modbus_reads(module)
    assert reads.count((11124, 34)) == 1 and reads.count((10500, 64)) == 2
    # 11114-11123 lie past the live block: not all held, so the module is asked.
    assert reads.count((11110, 20)) == 1
    session = cloud_of(poller)
    assert session.counters.forwarded == 4 and session.counters.answered_from_state == 0


async def test_a_cloud_request_goes_before_the_polls_still_waiting(setup: PollerSetup) -> None:
    unread = build_read_request(11124, 34)
    cloud = await setup.start_cloud(frames=[(0.3, unread)], time_scale=1.0)
    poller = await setup.start_poller(Inverter(enabled={"protection": True}), cloud=CloudAddress("127.0.0.1", cloud.port), request_gap_s=0.05)
    module = setup.start_module()
    await wait_until(lambda: cloud.session_done.is_set() and "protection" in poller.inverter.results, timeout_s=10.0)
    reads = modbus_reads(module)
    # 19 reads by the poller, 50 ms apart: the cloud's comes in the middle of the update, not after it.
    position = reads.index((11124, 34))
    assert 0 < position < len(reads) - 3
    assert read_reply(cloud, unread).count == 34


async def test_the_clouds_clock_write_reaches_the_inverter_and_the_words(setup: PollerSetup) -> None:
    clock_read = build_read_request(20001, 6)
    cloud = await setup.start_cloud(frames=[(0.5, CLOCK_WRITE), (0.5, clock_read)], time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    reported: list[CloudWrite] = []
    poller.on_cloud_write = reported.append
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert read_reply(cloud, CLOCK_WRITE).raw == append_crc(CLOCK_WRITE[:6])
    assert module_raw(module).count(CLOCK_WRITE) == 1
    # Answered from the words, which hold the written clock: 2026-01-02 03:04:05.
    assert read_reply(cloud, clock_read).registers() == (26, 1, 2, 3, 4, 5)
    assert modbus_reads(module).count((20001, 6)) == 1
    write = cloud_of(poller).last_write
    assert write is not None and write.is_clock and write.forwarded and write.values == (26, 1, 2, 3, 4, 5)
    assert reported == []


async def test_a_clock_write_without_a_reply_takes_the_clock_out_of_the_words(setup: PollerSetup) -> None:
    clock_read = build_read_request(20001, 6)
    cloud = await setup.start_cloud(frames=[(0.5, CLOCK_WRITE), (0.5, clock_read)], time_scale=1.0, reply_timeout_s=0.6)
    await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    module.drop.add(CLOCK_WRITE)
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert exchange_of(cloud, CLOCK_WRITE).reply is None
    # Sent once, never repeated.
    assert module_raw(module).count(CLOCK_WRITE) == 1
    assert modbus_reads(module).count((20001, 6)) == 2
    assert read_reply(cloud, clock_read).count == 6


async def test_a_clock_write_the_inverter_refuses_returns_the_exception_and_the_words_stay(setup: PollerSetup) -> None:
    # Two registers announced, one sent: the inverter answers illegal data value.
    malformed = append_crc(bytes.fromhex("01 10 4e 21 00 02 02 00 1a"))
    clock_read = build_read_request(20001, 6)
    cloud = await setup.start_cloud(frames=[(0.5, malformed), (0.5, clock_read)], time_scale=1.0)
    await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    refused = read_reply(cloud, malformed)
    assert refused.is_exception
    assert malformed in module_raw(module)
    # The clock read answered from the words, which kept what the poll read.
    assert read_reply(cloud, clock_read).count == 6
    assert modbus_reads(module).count((20001, 6)) == 1


async def test_a_write_outside_the_clock_is_blocked_logged_and_reported(setup: PollerSetup, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="renac_protocol.cloud_session")
    language_read = build_read_request(20007, 7)
    cloud = await setup.start_cloud(frames=[(0.5, OUTSIDE_CLOCK_WRITE), (0.5, language_read)], time_scale=1.0, reply_timeout_s=0.3)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    reported: list[CloudWrite] = []
    poller.on_cloud_write = reported.append
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert exchange_of(cloud, OUTSIDE_CLOCK_WRITE).reply is None
    assert OUTSIDE_CLOCK_WRITE not in module_raw(module)
    assert module.module.inverter.words[20007] == 0
    assert read_reply(cloud, language_read).registers()[0] == 0
    session = cloud_of(poller)
    assert session.counters.writes_blocked == 1
    assert [(write.address, write.count, write.values, write.forwarded) for write in reported] == [(20007, 1, (1,), False)]
    assert session.last_write == reported[0]
    assert any(record.levelno == logging.WARNING and "20007 x1 = 1" in record.getMessage() for record in caplog.records)


async def test_a_write_outside_the_clock_with_its_option_reaches_the_inverter_and_the_words(setup: PollerSetup, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="renac_protocol.cloud_session")
    language_read = build_read_request(20007, 7)
    cloud = await setup.start_cloud(frames=[(0.5, OUTSIDE_CLOCK_WRITE), (0.5, language_read)], time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port), cloud_options={"forward_unknown_writes": True})
    reported: list[CloudWrite] = []
    poller.on_cloud_write = reported.append
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert read_reply(cloud, OUTSIDE_CLOCK_WRITE).raw == OUTSIDE_CLOCK_WRITE
    assert module.module.inverter.words[20007] == 1
    # From the words, which took the written word.
    assert read_reply(cloud, language_read).registers()[0] == 1
    assert modbus_reads(module).count((20007, 7)) == 1
    assert cloud_of(poller).counters.writes_blocked == 0
    assert [(write.address, write.forwarded) for write in reported] == [(20007, True)]
    # The user allowed it: info level, not a warning.
    forwarded = [record for record in caplog.records if "20007 x1 = 1 is not the clock's" in record.getMessage()]
    assert [record.levelno for record in forwarded] == [logging.INFO]


async def test_a_bad_crc_frame_is_dropped_and_logged_once_per_cloud_connection(setup: PollerSetup, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="renac_protocol.cloud_session")
    frames = [(0.3, BAD_CRC_READ), (0.3, BAD_CRC_READ), (0.3, build_read_request(11000, 10))]
    cloud = await setup.start_cloud(frames=frames, time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    assert cloud_of(poller).counters.bad_crc_dropped == 2
    assert module.module.framer.counters.crc_errors == 0
    dropped = [record for record in caplog.records if "cloud's frame with a bad CRC dropped" in record.getMessage()]
    assert [record.levelno for record in dropped] == [logging.INFO, logging.DEBUG]


async def test_a_bad_crc_frame_with_its_option_reaches_the_module_as_received(setup: PollerSetup) -> None:
    frames = [(0.3, BAD_CRC_READ), (0.3, build_read_request(11000, 10))]
    cloud = await setup.start_cloud(frames=frames, time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port), cloud_options={"forward_bad_crc": True})
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)

    # The fake's framer drops it, as the inverter ignores it.
    assert module.module.framer.counters.crc_errors == 1
    assert cloud_of(poller).counters.bad_crc_dropped == 0
    assert exchange_of(cloud, BAD_CRC_READ).reply is None


async def test_module_frames_nobody_asked_for_go_to_the_cloud(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    frame = read_replies(module.module.inverter.words)[0]
    await module.send(frame)
    await wait_until(lambda: any(unsolicited.raw == frame for unsolicited in cloud.unsolicited))


async def test_a_refused_modules_cloud_gets_nothing_from_the_inverter(setup: PollerSetup) -> None:
    read = build_read_request(11000, 114)
    cloud = await setup.start_cloud(frames=[(0.5, read)], time_scale=1.0)
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port), serial="1234567890123456")
    module = setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    assert poller.session is not None and poller.session.refused is not None
    # The other inverter's own words, from it.
    assert read_reply(cloud, read).count == 114 and modbus_reads(module) == [(10000, 32), (11000, 114)]
    assert cloud_of(poller).counters.answered_from_state == 0


async def test_a_cloud_that_is_down_is_tried_again(setup: PollerSetup) -> None:
    port = free_port()
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", port), cloud_options={"retry_s": (0.2,)})
    setup.start_module()
    await wait_until(lambda: poller.cloud is not None and poller.cloud.status.error is not None)
    session = cloud_of(poller)
    assert not session.status.connected and "connect to 127.0.0.1" in (session.status.error or "")
    cloud = await setup.start_cloud(port)
    await wait_until(lambda: session.status.connected)
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)


async def test_a_dropped_cloud_is_connected_again(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port), cloud_options={"retry_s": (0.2,)})
    setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    session = cloud_of(poller)
    cloud.close_connections()
    await wait_until(lambda: not session.status.connected)
    await wait_until(lambda: cloud.connections == 2 and session.status.connected)
    assert session.counters.connects == 2


async def test_the_end_of_the_modules_session_closes_the_cloud(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    poller = await setup.start_poller(cloud=CloudAddress("127.0.0.1", cloud.port))
    module = setup.start_module(reconnect_s=30.0)
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    assert poller.session is not None and poller.session.checked
    session = cloud_of(poller)
    module.drop_connection()
    await wait_until(lambda: cloud.writers == [])
    await wait_until(lambda: poller.session is None)
    assert not session.connected and poller.cloud is None


async def test_a_cloud_name_is_resolved_before_connecting(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    poller = await setup.start_poller(cloud=CloudAddress("localhost", cloud.port, None))
    setup.start_module()
    await wait_until(lambda: poller.cloud is not None and poller.cloud.status.connected)
    assert cloud_of(poller).status.address == f"127.0.0.1:{cloud.port}"


async def test_open_cloud_gets_the_name_unresolved(setup: PollerSetup) -> None:
    cloud = await setup.start_cloud()
    asked: list[tuple[str, int]] = []

    async def open_cloud(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        asked.append((host, port))
        return await asyncio.open_connection("127.0.0.1", cloud.port)

    poller = await setup.start_poller(cloud=CloudAddress("cloud.test", 20145), cloud_options={"open_cloud": open_cloud, "cloud_via": "the test's opener"})
    setup.start_module()
    await wait_until(cloud.session_done.is_set, timeout_s=10.0)
    assert asked == [("cloud.test", 20145)]
    assert cloud_of(poller).status.address == "cloud.test:20145"
