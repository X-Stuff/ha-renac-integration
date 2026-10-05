"""The transport on 127.0.0.1. The connection, dialled to the mock relay: matching, retries, timeouts, the first reply's
timeout, the gap, the hold, the priorities, unsolicited frames, a lost connection. The listener, with the fake module
dialling in: a session, a replaced one, a lost one, stopping."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from renac_protocol.channel import ChannelClosed, ChannelError
from renac_protocol.connection import Connection, Priority, ReplyTimeout, TurnQueue, dial, read_reply_matcher
from renac_protocol.frames import ModbusFrame, ModbusKind, build_read_request
from renac_protocol.listener import Listener
from tests.helpers import Modules, wait_until
from tests.mocks.mock_relay import MockRelay, read_replies

IDENTITY_READ = build_read_request(10000, 32)
LIVE_READ = build_read_request(11000, 114)


@pytest.fixture
async def relay(inverter_words: dict[int, int]) -> AsyncIterator[MockRelay]:
    mock = MockRelay(inverter_words, latency_s=0.01, unsolicited_frames=read_replies(inverter_words))
    await mock.start()
    yield mock
    await mock.stop()


@pytest.fixture
async def listener() -> AsyncIterator[Listener]:
    value = Listener("127.0.0.1", 0, reply_timeout_s=0.3, request_gap_s=0.0)
    await value.start()
    yield value
    await value.stop()


@pytest.fixture
async def modules() -> AsyncIterator[Modules]:
    value = Modules()
    yield value
    await value.stop()


async def connect(relay: MockRelay, **options: float) -> Connection:
    settings = {"reply_timeout_s": 0.3, "first_reply_timeout_s": 0.6, "request_gap_s": 0.0, "connect_timeout_s": 2.0} | options
    return await dial("127.0.0.1", relay.port, **settings)


# The connection, dialled to the mock relay


async def test_read_identity_through_the_relay(relay: MockRelay) -> None:
    connection = await connect(relay)
    reply = await connection.read(10000, 32)
    await connection.close()

    assert reply.kind is ModbusKind.READ_REPLY and reply.count == 32
    assert relay.requests[0].raw == IDENTITY_READ
    assert connection.counters.requests == 1 and connection.counters.replies == 1
    assert connection.counters.last_round_trip_s > 0
    assert connection.channel.counters.frames_in == 1 and connection.channel.counters.bytes_out == len(IDENTITY_READ)
    assert not connection.is_connected and connection.closed.is_set() and connection.close_reason == "closed"


async def test_connect_failure_is_a_channel_error() -> None:
    with pytest.raises(ChannelError):
        await dial("127.0.0.1", 1, connect_timeout_s=1.0)


async def test_a_request_on_a_closed_connection_is_refused(relay: MockRelay) -> None:
    connection = await connect(relay)
    await connection.close()
    with pytest.raises(ChannelClosed):
        await connection.read(10000, 32)


async def test_a_refused_address_comes_back_as_an_exception_frame(relay: MockRelay) -> None:
    connection = await connect(relay)
    reply = await connection.read(10500, 64)
    await connection.close()
    assert reply.is_exception and reply.exception_code == 2
    assert connection.counters.exceptions == 1


async def test_timeout_retries_once_then_raises(relay: MockRelay) -> None:
    relay.drop.add(LIVE_READ)
    connection = await connect(relay)
    with pytest.raises(ReplyTimeout):
        await connection.read(11000, 114)
    await connection.close()
    assert connection.counters.timeouts == 2 and connection.counters.retries == 1
    assert len(relay.requests) == 2


async def test_the_first_reply_may_take_longer(relay: MockRelay) -> None:
    relay.latency_s = 0.4
    connection = await connect(relay)
    reply = await connection.read(10000, 32)
    assert reply.count == 32
    relay.latency_s = 0.01
    await connection.read(10176, 6)
    relay.latency_s = 0.4
    connection.retries = 0
    with pytest.raises(ReplyTimeout):
        await connection.read(14000, 27)
    await connection.close()


async def test_a_reply_of_another_shape_is_unsolicited_and_the_right_one_is_still_taken(relay: MockRelay) -> None:
    relay.latency_s = 0.1
    connection = await connect(relay)
    seen = []
    connection.on_unsolicited = seen.append
    task = asyncio.create_task(connection.read(14000, 27))
    await asyncio.sleep(0.03)
    await relay.send_unsolicited()
    reply = await task
    await connection.close()
    assert reply.count == 27
    assert connection.counters.unsolicited == 1 and len(seen) == 1
    assert isinstance(seen[0], ModbusFrame)


async def test_retries_can_be_left_out_for_one_request(relay: MockRelay) -> None:
    relay.drop.add(LIVE_READ)
    connection = await connect(relay)
    with pytest.raises(ReplyTimeout):
        await connection.send(LIVE_READ, read_reply_matcher(114), retries=0)
    await connection.close()
    assert connection.counters.timeouts == 1 and connection.counters.retries == 0
    assert len(relay.requests) == 1


async def test_a_high_priority_request_goes_next_after_the_one_in_flight(relay: MockRelay) -> None:
    relay.latency_s = 0.05
    connection = await connect(relay)
    polls = [asyncio.create_task(connection.read(address, count)) for address, count in ((10000, 32), (10176, 6), (14000, 27))]
    await asyncio.sleep(0.02)
    cloud = asyncio.create_task(connection.send(LIVE_READ, read_reply_matcher(114), priority=Priority.HIGH))
    replies = await asyncio.gather(*polls, cloud)
    await connection.close()
    assert [request.address for request in relay.requests] == [10000, 11000, 10176, 14000]
    assert [reply.count for reply in replies] == [32, 6, 27, 114]  # type: ignore[union-attr]


async def test_high_priority_requests_keep_their_own_order() -> None:
    turns = TurnQueue()
    order: list[str] = []

    async def take(name: str, high: bool) -> None:
        async with turns.turn(high):
            order.append(name)
            await asyncio.sleep(0.01)

    await turns.acquire(False)
    tasks = [asyncio.create_task(take(name, high)) for name, high in (("poll 1", False), ("cloud 1", True), ("poll 2", False), ("cloud 2", True))]
    await asyncio.sleep(0.01)
    turns.release()
    await asyncio.gather(*tasks)
    assert order == ["cloud 1", "cloud 2", "poll 1", "poll 2"]


async def test_a_cancelled_waiter_does_not_keep_the_turn() -> None:
    turns = TurnQueue()
    await turns.acquire(False)
    waiter = asyncio.create_task(turns.acquire(True))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    turns.release()
    await asyncio.wait_for(turns.acquire(False), 0.1)
    turns.release()


async def test_hold_keeps_the_next_request_back(relay: MockRelay) -> None:
    connection = await connect(relay)
    connection.hold(0.3)
    # Monotonic seconds are large floats: (now + 0.3) - now can come out a hair above 0.3.
    assert 0.2 < connection.held_for_s <= 0.3 + 1e-6
    loop = asyncio.get_running_loop()
    started = loop.time()
    await connection.read(10000, 32)
    await connection.close()
    assert loop.time() - started >= 0.25


async def test_requests_are_serialized_and_keep_the_gap(relay: MockRelay) -> None:
    connection = await connect(relay, request_gap_s=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    replies = await asyncio.gather(connection.read(10000, 32), connection.read(10176, 6), connection.read(14000, 27))
    await connection.close()
    assert [reply.count for reply in replies] == [32, 6, 27]
    assert loop.time() - started >= 0.1
    assert [request.address for request in relay.requests] == [10000, 10176, 14000]


async def test_peer_closing_fails_the_pending_request_and_ends_the_connection(relay: MockRelay) -> None:
    relay.drop.add(IDENTITY_READ)
    connection = await connect(relay, reply_timeout_s=2.0)
    task = asyncio.create_task(connection.read(10000, 32))
    await asyncio.sleep(0.05)
    await relay.close_clients()
    with pytest.raises(ChannelClosed):
        await task
    assert not connection.is_connected and connection.closed.is_set() and connection.close_reason is not None


# The listener, with the fake module dialling in


async def test_a_module_connects_and_answers_what_is_sent(listener: Listener, modules: Modules) -> None:
    sessions: list[Connection] = []
    listener.on_session = sessions.append
    module = modules.dial(listener.port)
    await wait_until(lambda: len(sessions) == 1)
    connection = sessions[0]
    reply = await connection.read(10000, 32)
    assert reply.count == 32 and connection.name == module.local_address
    assert listener.session is connection and listener.sessions == 1


async def test_a_second_connection_replaces_the_first(listener: Listener, modules: Modules) -> None:
    sessions: list[Connection] = []
    listener.on_session = sessions.append
    first = modules.dial(listener.port, reconnect_s=30.0)
    await wait_until(lambda: len(sessions) == 1)
    second = modules.dial(listener.port, reconnect_s=30.0)
    await wait_until(lambda: len(sessions) == 2)
    await wait_until(lambda: not first.is_connected)
    assert sessions[0].closed.is_set() and "replaced by a new connection from" in (sessions[0].close_reason or "")
    assert sessions[1].name == second.local_address and listener.sessions == 2
    assert (await sessions[1].read(10000, 32)).count == 32


async def test_a_lost_session_shows_on_its_connection(listener: Listener, modules: Modules) -> None:
    sessions: list[Connection] = []
    listener.on_session = sessions.append
    module = modules.dial(listener.port, reconnect_s=30.0)
    await wait_until(lambda: len(sessions) == 1)
    module.drop_connection()
    await asyncio.wait_for(sessions[0].closed.wait(), 3.0)
    assert not sessions[0].is_connected and sessions[0].close_reason is not None


async def test_stop_closes_the_session_and_the_port(listener: Listener, modules: Modules) -> None:
    module = modules.dial(listener.port, reconnect_s=30.0)
    await wait_until(lambda: listener.session is not None)
    await listener.stop()
    await wait_until(lambda: not module.is_connected)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", listener.port)
