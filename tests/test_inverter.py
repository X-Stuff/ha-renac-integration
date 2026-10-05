"""The inverter on its own and with groups read through a connection to the mock relay: the schedule (what is due when,
live mode, intervals, failures, the group of an address), groups read and their words stored, the readings decoded on
demand with the power32 group standing in and falling back, a refused block, a lost connection, failures recorded and
logged; the words for the cloud session and their ages, written words, forgetting and clearing; the table against the
groups."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncIterator, Mapping

import pytest

from renac_protocol.connection import Connection, dial
from renac_protocol.const import FAILED_RETRY_S
from renac_protocol.frames import ModbusFrame, build_read_request
from renac_protocol.group_reader import GroupResult, read_group
from renac_protocol.groups import ENERGY_GROUP, GROUPS, GROUPS_BY_KEY, IDENTITY_GROUP, LIVE_GROUP, POWER32_GROUP
from renac_protocol.inverter import Inverter
from renac_protocol.register_table import ALL_REGISTERS, POWER_32BIT
from renac_protocol.registers import Block, Reading, Register
from tests.mocks.mock_relay import ILLEGAL_ADDRESS, MockRelay


def reading_of(words: Mapping[int, int], key: str, registers: tuple[Register, ...] = ALL_REGISTERS) -> Reading:
    """What the register under the key decodes to from the words."""
    register = next(register for register in registers if register.key == key)
    return register.decode(tuple(words[address] for address in range(register.address, register.end)))


def words_of(words: Mapping[int, int], address: int, count: int) -> tuple[int, ...]:
    return tuple(words[register] for register in range(address, address + count))


def run_everything(inverter: Inverter, now: float) -> None:
    for group in inverter.due(now):
        inverter.store_result(GroupResult(group, finished_at=now))


async def read_due(inverter: Inverter, connection: Connection) -> list[GroupResult]:
    """The due groups read and stored, as the poller does it."""
    results = []
    for group in inverter.due(time.monotonic()):
        result = await read_group(connection, group)
        inverter.store_result(result)
        results.append(result)
    return results


@pytest.fixture
async def relay(inverter_words: dict[int, int]) -> AsyncIterator[MockRelay]:
    mock = MockRelay(inverter_words, latency_s=0.005)
    await mock.start()
    yield mock
    await mock.stop()


@pytest.fixture
async def client(relay: MockRelay) -> AsyncIterator[Connection]:
    connection = await dial("127.0.0.1", relay.port, reply_timeout_s=0.3, first_reply_timeout_s=0.6, request_gap_s=0.0, connect_timeout_s=2.0)
    yield connection
    await connection.close()


# The schedule


def test_everything_enabled_is_due_at_the_start() -> None:
    inverter = Inverter()
    assert inverter.due(100.0) == [group for group in inverter.groups if inverter.enabled[group.key]]
    assert inverter.seconds_until_due(100.0) == 0.0


def test_a_group_is_due_again_after_its_interval() -> None:
    inverter = Inverter()
    run_everything(inverter, 100.0)
    live = inverter.interval_of(LIVE_GROUP)
    assert inverter.due(100.0) == []
    assert inverter.seconds_until_due(100.0) == live
    assert LIVE_GROUP not in inverter.due(100.0 + live - 0.1)
    assert LIVE_GROUP in inverter.due(100.0 + live)


def test_live_mode_uses_the_live_intervals() -> None:
    inverter = Inverter()
    run_everything(inverter, 100.0)
    inverter.live_mode = True
    assert LIVE_GROUP.live_interval_s is not None
    assert inverter.due(100.0 + LIVE_GROUP.live_interval_s) == [LIVE_GROUP]
    inverter.live_mode = False
    assert inverter.due(100.0 + LIVE_GROUP.live_interval_s) == []
    # A configured live interval replaces the group's own.
    inverter = Inverter(live_intervals={"live": 2.0})
    run_everything(inverter, 100.0)
    inverter.live_mode = True
    assert inverter.due(102.0) == [LIVE_GROUP]


def test_configured_intervals_and_enabled_groups() -> None:
    inverter = Inverter(intervals={"energy": 10.0}, enabled={"live": False})
    assert LIVE_GROUP not in inverter.due(0.0)
    run_everything(inverter, 0.0)
    assert inverter.due(10.0) == [ENERGY_GROUP]


def test_a_failed_group_is_retried_after_the_retry_wait_or_its_interval_when_shorter() -> None:
    inverter = Inverter()
    inverter.store_result(GroupResult(IDENTITY_GROUP, error="no reply", finished_at=100.0))
    assert not inverter.is_due(IDENTITY_GROUP, 100.0 + FAILED_RETRY_S - 1)
    assert inverter.is_due(IDENTITY_GROUP, 100.0 + FAILED_RETRY_S)
    live = inverter.interval_of(LIVE_GROUP)
    inverter.store_result(GroupResult(LIVE_GROUP, error="no reply", finished_at=100.0))
    assert inverter.is_due(LIVE_GROUP, 100.0 + min(live, FAILED_RETRY_S))
    inverter.store_result(GroupResult(LIVE_GROUP, finished_at=100.0 + live))
    assert not inverter.is_due(LIVE_GROUP, 101.0 + live)


def test_no_enabled_group_means_nothing_is_ever_due() -> None:
    inverter = Inverter(enabled={key: False for key in GROUPS_BY_KEY})
    assert inverter.due(0.0) == []
    assert inverter.seconds_until_due(0.0) is None


def test_the_group_of_an_address() -> None:
    inverter = Inverter()
    live_range = LIVE_GROUP.ranges[0]
    assert inverter.group_of(live_range.address + live_range.count - 1) is LIVE_GROUP
    # Past the 16-bit block, before the 32-bit one: no group.
    assert inverter.group_of(live_range.address + live_range.count) is None
    assert inverter.group_of(POWER32_GROUP.ranges[0].address) is POWER32_GROUP


# Groups read through a connection


async def test_the_due_groups_read_and_their_readings_decode(relay: MockRelay, client: Connection) -> None:
    inverter = Inverter()
    due = inverter.due(time.monotonic())
    results = await read_due(inverter, client)

    assert [result.group for result in results] == due
    assert all(result.ok for result in results), [result.error for result in results]
    readings = inverter.readings()
    for key in ("serial", "pv1_power", "pv_energy", "work_mode"):
        assert readings[key] == reading_of(relay.words, key), key
    assert set(inverter.results) == {group.key for group in due}
    assert client.is_connected


async def test_nothing_is_read_until_a_group_is_due_and_live_mode_brings_it_sooner(relay: MockRelay, client: Connection) -> None:
    inverter = Inverter((dataclasses.replace(LIVE_GROUP, live_interval_s=0.05),))
    await read_due(inverter, client)
    assert await read_due(inverter, client) == []
    assert (inverter.seconds_until_due(time.monotonic()) or 0) > 1.0
    inverter.live_mode = True
    await asyncio.sleep(0.06)
    assert [result.group.key for result in await read_due(inverter, client)] == ["live"]


async def test_the_power32_group_stands_in_for_the_live_values_and_falls_back_when_refused(relay: MockRelay, client: Connection) -> None:
    inverter = Inverter(enabled={key: key in ("live", "power32") for key in GROUPS_BY_KEY})
    results = await read_due(inverter, client)
    assert [result.group.key for result in results] == ["live", "power32"] and all(result.ok for result in results)
    for register in POWER_32BIT:
        assert inverter.readings()[register.key] == reading_of(relay.words, register.key, POWER_32BIT), register.key
    assert inverter.readings()["pv1_power"] == reading_of(relay.words, "pv1_power")

    relay.replies[build_read_request(11124, 34)] = ILLEGAL_ADDRESS
    result = await read_group(client, POWER32_GROUP)
    inverter.store_result(result)
    assert not result.ok and inverter.readings_of(POWER32_GROUP) == {}
    for register in POWER_32BIT:
        assert inverter.readings()[register.key] == reading_of(relay.words, register.key), register.key


async def test_a_refused_block_fails_its_group_keeps_the_blocks_before_it_and_hides_its_readings(relay: MockRelay, client: Connection) -> None:
    undocumented = GROUPS_BY_KEY["undocumented"]
    last = undocumented.ranges[-1]
    relay.replies[build_read_request(last.address, last.count)] = ILLEGAL_ADDRESS
    inverter = Inverter(enabled={"undocumented": True})
    results = await read_due(inverter, client)
    by_key = {result.group.key: result for result in results}
    assert not by_key["undocumented"].ok
    first = undocumented.ranges[0]
    assert inverter.valid_words(first.address, first.count) == words_of(relay.words, first.address, first.count)
    assert inverter.valid_words(last.address, last.count) is None
    assert inverter.readings_of(undocumented) == {} and "register_20114" not in inverter.readings()
    assert all(result.ok for key, result in by_key.items() if key != "undocumented")
    assert not inverter.is_due(undocumented, by_key["undocumented"].finished_at + 1)


async def test_a_lost_connection_fails_the_group_being_read(relay: MockRelay, client: Connection) -> None:
    relay.drop.add(build_read_request(11000, 114))
    client.reply_timeout_s = 2.0

    def cut_the_link_at_the_live_read(request: ModbusFrame) -> None:
        if request.address == 11000:
            asyncio.get_running_loop().call_later(0.02, lambda: asyncio.ensure_future(relay.close_clients()))

    relay.on_request = cut_the_link_at_the_live_read
    result = await read_group(client, LIVE_GROUP)
    assert not result.ok and "11000 x114" in (result.error or "")
    assert not client.is_connected


def test_fail_records_every_group_failed_and_due_again_later() -> None:
    inverter = Inverter()
    due = inverter.due(time.monotonic())
    results = inverter.fail(due, "connect to 127.0.0.1:1: refused")
    assert [result.group for result in results] == due
    assert all(result.error == "connect to 127.0.0.1:1: refused" for result in inverter.results.values())
    assert inverter.readings() == {} and inverter.due(time.monotonic()) == []


async def test_a_failure_is_logged_once_while_it_repeats(relay: MockRelay, client: Connection, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="renac_protocol.inverter")
    relay.replies[build_read_request(14000, 27)] = ILLEGAL_ADDRESS
    inverter = Inverter()
    for _ in range(2):
        inverter.store_result(await read_group(client, ENERGY_GROUP))
    failed = [record.levelno for record in caplog.records if "group energy failed" in record.getMessage()]
    assert failed == [logging.WARNING, logging.DEBUG]


# The words, for the cloud session


async def test_the_words_read_are_valid_whole_blocks_only(relay: MockRelay, client: Connection) -> None:
    inverter = Inverter()
    await read_due(inverter, client)
    words = relay.words
    assert inverter.valid_words(11000, 114) == words_of(words, 11000, 114)
    # 11006-11015 belong to no register; the block's words are kept all the same.
    assert inverter.valid_words(11010, 5) == words_of(words, 11010, 5)
    # A BMS serial block: read by the identity group, decoded by nothing.
    assert inverter.valid_words(10048, 80) == words_of(words, 10048, 80)
    # 11114 lies past the live block.
    assert inverter.valid_words(11110, 20) is None


async def test_words_older_than_their_limit_are_not_valid(relay: MockRelay, client: Connection) -> None:
    # Valid for a thousandth of the live group's interval.
    inverter = Inverter(age_intervals=0.001)
    inverter.store_result(await read_group(client, LIVE_GROUP))
    await asyncio.sleep(inverter.interval_of(LIVE_GROUP) * 0.001 + 0.02)
    assert inverter.valid_words(11000, 114) is None


def test_written_words_go_in_as_if_read_and_forget_takes_them_out() -> None:
    inverter = Inverter()
    inverter.store_written(range(20001, 20008), (26, 1, 2, 3, 4, 5, 1))
    assert inverter.valid_words(20001, 7) == (26, 1, 2, 3, 4, 5, 1)
    # A register no group reads stays out.
    inverter.store_written([10500], [7])
    assert inverter.valid_words(10500, 1) is None
    inverter.forget(range(20001, 20007))
    assert inverter.valid_words(20001, 6) is None and inverter.valid_words(20007, 1) == (1,)


def test_a_stored_block_gives_its_words_and_readings_without_a_result(inverter_words: dict[int, int]) -> None:
    inverter = Inverter()
    block = Block(10000, words_of(inverter_words, 10000, 32))
    inverter.store_block(IDENTITY_GROUP, block)
    assert inverter.readings()["serial"] == reading_of(inverter_words, "serial")
    assert inverter.valid_words(10000, 32) == block.words
    assert IDENTITY_GROUP in inverter.due(time.monotonic())


async def test_clear_forgets_everything_and_makes_every_group_due(relay: MockRelay, client: Connection) -> None:
    inverter = Inverter()
    due = inverter.due(time.monotonic())
    await read_due(inverter, client)
    inverter.clear()
    assert inverter.readings() == {} and inverter.results == {} and inverter.valid_words(11000, 1) is None
    assert inverter.due(time.monotonic()) == due


# The table against the groups


def test_every_register_of_a_group_lies_inside_one_of_its_ranges() -> None:
    for group in GROUPS:
        for register in group.registers:
            assert any(block_range.address <= register.address and register.end <= block_range.address + block_range.count for block_range in group.ranges), (
                f"{group.key}: {register.key} outside the ranges"
            )


def test_every_register_belongs_to_exactly_one_group_and_power32_shares_the_live_keys() -> None:
    seen: dict[str, str] = {}
    for group in GROUPS:
        if group is POWER32_GROUP:
            continue
        for register in group.registers:
            assert register.key not in seen, f"{register.key} in {seen[register.key]} and {group.key}"
            seen[register.key] = group.key
    assert set(seen) == {register.key for register in ALL_REGISTERS}
    assert {register.key for register in POWER32_GROUP.registers} <= {register.key for register in LIVE_GROUP.registers}
