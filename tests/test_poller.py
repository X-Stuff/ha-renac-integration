"""The poller on 127.0.0.1. Server mode with the fake module dialling in: a session's settle time, identity read and
handshake, the groups on their timers and live mode, a replaced and a lost session, a check tried again, the readings
kept until a read fails, the tool's clear at a session's end. Client mode through the mock relay, both ways in one
poller, the serial learned and checked, refusals."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator

import pytest

from renac_protocol.frames import AA55Frame, ModbusFrame, build_read_request
from renac_protocol.groups import ENERGY_GROUP, IDENTITY_GROUP, LIVE_GROUP
from renac_protocol.handshake import HANDSHAKE
from renac_protocol.inverter import Inverter
from renac_protocol.poller import Poller, Source
from tests.helpers import PollerSetup, free_port, modbus_reads, relay_reads, wait_until
from tests.mocks.mock_hf_lpt270 import random_identity

IDENTITY_READ = build_read_request(10000, 32)


@pytest.fixture
async def setup() -> AsyncIterator[PollerSetup]:
    value = PollerSetup()
    yield value
    await value.stop()


def default_groups_done(poller: Poller) -> bool:
    return all(key in poller.inverter.results for key in ("identity", "settings", "live", "energy"))


def checked(poller: Poller) -> bool:
    return poller.session is not None and poller.session.checked


def serial_reading(poller: Poller) -> str | None:
    reading = poller.inverter.readings().get("serial")
    return str(reading.value) if reading is not None else None


# Server mode


async def test_a_session_starts_after_the_settle_time_with_the_identity_read_then_the_handshake(setup: PollerSetup) -> None:
    poller = await setup.start_poller(settle_s=0.3)
    learned: list[str] = []
    poller.on_serial_learned = learned.append
    module = setup.start_module()
    await wait_until(lambda: default_groups_done(poller))

    assert module.connected_at is not None and module.first_request_at is not None
    assert module.first_request_at - module.connected_at >= 0.25
    first = module.requests[0]
    assert isinstance(first, ModbusFrame) and (first.address, first.count) == (10000, 32)
    handshake = [(frame.command, frame.sub) for frame in module.requests[1:5] if isinstance(frame, AA55Frame)]
    assert handshake == list(HANDSHAKE)
    # The identity read, then the identity group: every group is due at the first session.
    assert modbus_reads(module)[:3] == [(10000, 32), (10000, 32), (10176, 6)]
    identity = module.module.identity
    assert learned == [identity.inverter_serial] and poller.serial == identity.inverter_serial
    assert checked(poller) and poller.source is Source.SERVER
    assert poller.module.module_serial == identity.module_serial
    assert all(result.ok for result in poller.inverter.results.values())


async def test_without_the_handshake_identity_comes_first(setup: PollerSetup) -> None:
    poller = await setup.start_poller(send_handshake=False)
    module = setup.start_module()
    await wait_until(lambda: default_groups_done(poller))
    assert not any(isinstance(frame, AA55Frame) for frame in module.requests)
    assert modbus_reads(module)[0] == (10000, 32)
    assert poller.module.is_empty


async def test_groups_run_on_their_timers_and_live_mode_shortens_them(setup: PollerSetup) -> None:
    live = dataclasses.replace(LIVE_GROUP, interval_s=10.0, live_interval_s=0.1)
    energy = dataclasses.replace(ENERGY_GROUP, interval_s=0.15)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live, energy)), send_handshake=False)
    module = setup.start_module()
    await wait_until(lambda: "energy" in poller.inverter.results)
    await asyncio.sleep(0.5)
    reads = modbus_reads(module)
    assert reads.count((11000, 114)) == 1
    assert reads.count((14000, 27)) >= 3
    # The identity read and the identity group.
    assert reads.count((10000, 32)) == 2

    poller.live_mode = True
    await asyncio.sleep(0.5)
    assert modbus_reads(module).count((11000, 114)) >= 4
    poller.live_mode = False


async def test_a_second_connection_replaces_the_first(setup: PollerSetup) -> None:
    poller = await setup.start_poller()
    identity = random_identity()
    first = setup.start_module(identity, reconnect_s=30.0)
    await wait_until(lambda: default_groups_done(poller))
    second = setup.start_module(identity, reconnect_s=30.0)
    await wait_until(lambda: second.is_connected and poller.session is not None and poller.session.connection.name == second.local_address)
    await wait_until(lambda: not first.is_connected)
    await wait_until(lambda: checked(poller) and not poller.module.is_empty)
    # The new session's own identity read and handshake.
    assert modbus_reads(second)[0] == (10000, 32)
    assert any(isinstance(frame, AA55Frame) for frame in second.requests)
    assert poller.listener is not None and poller.listener.sessions == 2


async def test_a_lost_session_keeps_the_readings_and_the_module_comes_back(setup: PollerSetup) -> None:
    poller = await setup.start_poller()
    module = setup.start_module(reconnect_s=0.3)
    await wait_until(lambda: default_groups_done(poller))
    module.drop_connection()
    await wait_until(lambda: poller.session is None)
    assert poller.module.is_empty and poller.last_session_end is not None
    assert serial_reading(poller) == module.module.identity.inverter_serial
    await wait_until(lambda: module.connects == 2 and checked(poller) and not poller.module.is_empty)


async def test_an_identity_read_without_a_reply_is_tried_again_and_fails_the_due_groups(setup: PollerSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("renac_protocol.poller.CHECK_RETRY_S", 0.3)
    poller = await setup.start_poller(send_handshake=False)
    module = setup.start_module()
    module.drop.add(IDENTITY_READ)
    # The read and its retry time out: the due groups fail with it.
    await wait_until(lambda: len(poller.inverter.results) == 4)
    assert not checked(poller) and poller.inverter.readings() == {}
    assert all("the identity read failed" in (result.error or "") for result in poller.inverter.results.values())
    await wait_until(lambda: modbus_reads(module).count((10000, 32)) >= 2)
    module.drop.clear()
    await wait_until(lambda: default_groups_done(poller) and all(result.ok for result in poller.inverter.results.values()), timeout_s=5.0)
    assert checked(poller)


async def test_a_reading_keeps_its_value_until_a_read_of_its_group_fails(setup: PollerSetup) -> None:
    live = dataclasses.replace(LIVE_GROUP, interval_s=0.2)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live)), send_handshake=False)
    module = setup.start_module(reconnect_s=30.0)
    await wait_until(lambda: "live" in poller.inverter.results and poller.inverter.results["live"].ok)
    module.drop_connection()
    await wait_until(lambda: poller.session is None)
    # Nothing changes with the session's end; the live group fails when it is due with no way in.
    assert "pv1_power" in poller.inverter.readings()
    await wait_until(lambda: not poller.inverter.results["live"].ok)
    assert poller.inverter.results["live"].error == "no connection"
    assert "pv1_power" not in poller.inverter.readings() and serial_reading(poller) is not None


async def test_before_any_way_in_is_tried_nothing_fails(setup: PollerSetup) -> None:
    poller = await setup.start_poller()
    await asyncio.sleep(0.3)
    assert poller.inverter.results == {} and poller.inverter.readings() == {} and poller.source is None


async def test_clear_on_session_end_forgets_everything_the_learned_serial_included(setup: PollerSetup) -> None:
    poller = await setup.start_poller(clear_on_session_end=True)
    module = setup.start_module(reconnect_s=30.0)
    await wait_until(lambda: default_groups_done(poller))
    assert poller.serial is not None
    module.drop_connection()
    await wait_until(lambda: poller.session is None)
    assert poller.inverter.results == {} and poller.inverter.readings() == {} and poller.serial is None


async def test_stop_closes_the_session_and_the_port(setup: PollerSetup) -> None:
    poller = await setup.start_poller()
    module = setup.start_module(reconnect_s=30.0)
    await wait_until(lambda: poller.session is not None)
    assert poller.listener is not None
    port = poller.listener.port
    await poller.stop()
    await wait_until(lambda: not module.is_connected)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


# Client mode, both ways, the serial


async def test_client_mode_dials_per_update_reads_the_identity_and_learns_the_serial(setup: PollerSetup) -> None:
    identity = random_identity()
    relay = await setup.start_relay(identity)
    poller = await setup.start_poller(server=False, client=relay.port)
    learned: list[str] = []
    poller.on_serial_learned = learned.append
    await wait_until(lambda: default_groups_done(poller))

    assert learned == [identity.inverter_serial] and poller.serial == identity.inverter_serial
    assert poller.source is Source.CLIENT and poller.session is None and poller.client_refused is None and poller.client_error is None
    # The identity read first, then the groups.
    assert relay_reads(relay)[:3] == [(10000, 32), (10000, 32), (10176, 6)]
    assert all(result.ok for result in poller.inverter.results.values())
    assert poller.inverter.readings()["model"].value == identity.model
    await wait_until(lambda: poller.client_connection is None)


async def test_a_group_due_during_a_client_update_is_read_on_the_same_connection(setup: PollerSetup) -> None:
    identity = random_identity()
    relay = await setup.start_relay(identity, latency_s=0.1)
    live = dataclasses.replace(LIVE_GROUP, interval_s=0.25)
    energy = dataclasses.replace(ENERGY_GROUP, interval_s=0.25)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live, energy)), server=False, client=relay.port)
    # The first update reads everything, the energy group last. The second, due by the live group, lasts longer than
    # what is left of the energy group's interval: the energy group is read on that connection, not on a dial of its own.
    await wait_until(lambda: relay_reads(relay).count((14000, 27)) >= 2)
    assert relay.accepted == 2 and relay_reads(relay)[-3:] == [(10000, 32), (11000, 114), (14000, 27)]
    assert poller.inverter.results["energy"].ok


async def test_live_mode_holds_the_clients_connection(setup: PollerSetup) -> None:
    identity = random_identity()
    relay = await setup.start_relay(identity)
    live = dataclasses.replace(LIVE_GROUP, live_interval_s=0.1)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live)), server=False, client=relay.port)
    poller.live_mode = True
    await wait_until(lambda: relay_reads(relay).count((11000, 114)) >= 2)
    accepted, checks, reads = relay.accepted, relay_reads(relay).count((10000, 32)), relay_reads(relay).count((11000, 114))
    await wait_until(lambda: relay_reads(relay).count((11000, 114)) >= reads + 3)
    # The same connection, checked once.
    assert relay.accepted == accepted and relay_reads(relay).count((10000, 32)) == checks
    assert poller.client_connection is not None and poller.client_connection.is_connected
    # Off: the held connection is closed without waiting for the next update.
    poller.live_mode = False
    await wait_until(lambda: poller.client_connection is None)


async def test_a_client_without_its_endpoint_fails_every_due_group(setup: PollerSetup) -> None:
    poller = await setup.start_poller(server=False, client=free_port())
    await wait_until(lambda: len(poller.inverter.results) == 4)
    assert all("connect to 127.0.0.1" in (result.error or "") for result in poller.inverter.results.values())
    assert poller.inverter.readings() == {} and poller.serial is None and "connect to 127.0.0.1" in (poller.client_error or "")


async def test_a_client_meeting_another_inverter_is_refused_and_reads_nothing(setup: PollerSetup) -> None:
    relay = await setup.start_relay(random_identity())
    poller = await setup.start_poller(server=False, client=relay.port, serial="1234567890123456")
    learned: list[str] = []
    poller.on_serial_learned = learned.append
    await wait_until(lambda: poller.client_refused is not None)

    assert poller.client_refused is not None and "answers, not 1234567890123456" in poller.client_refused
    assert poller.client_error == poller.client_refused
    assert relay_reads(relay) == [(10000, 32)]
    assert poller.inverter.readings() == {} and learned == [] and poller.serial == "1234567890123456"
    assert all(result.error == poller.client_refused for result in poller.inverter.results.values())
    assert poller.client_connection is None


async def test_server_first_the_client_while_the_module_is_away(setup: PollerSetup) -> None:
    identity = random_identity()
    relay = await setup.start_relay(identity)
    live = dataclasses.replace(LIVE_GROUP, interval_s=0.2)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live)), client=relay.port, send_handshake=False)
    await wait_until(lambda: "live" in poller.inverter.results and poller.source is Source.CLIENT)

    # The module connects: the updates move to it, the client stops.
    module = setup.start_module(identity, reconnect_s=30.0)
    await wait_until(lambda: checked(poller) and poller.source is Source.SERVER)
    through_relay = len(relay.requests)
    await wait_until(lambda: modbus_reads(module).count((11000, 114)) >= 3)
    assert len(relay.requests) == through_relay

    # It drops: the next updates go through the client again.
    module.drop_connection()
    await wait_until(lambda: poller.source is Source.CLIENT and len(relay.requests) > through_relay + 2)
    assert poller.session is None
    assert poller.serial == identity.inverter_serial


async def test_a_session_starting_during_a_client_update_waits_for_it(setup: PollerSetup) -> None:
    identity = random_identity()
    # About 13 reads 0.15 s apart: the update takes some 2 s.
    relay = await setup.start_relay(identity, latency_s=0.15)
    sent_at: list[float] = []
    relay.on_request = lambda frame: sent_at.append(time.monotonic())
    poller = await setup.start_poller(client=relay.port, send_handshake=False, settle_s=0.0)
    await wait_until(lambda: len(relay.requests) >= 2)
    module = setup.start_module(identity, reconnect_s=30.0)
    await wait_until(lambda: module.first_request_at is not None, timeout_s=6.0)
    await wait_until(lambda: poller.source is Source.SERVER)
    await asyncio.sleep(0.3)
    assert module.connected_at is not None and module.first_request_at is not None
    # The module connected during the update, and its first request came after the client's last one and its reply.
    # Windows' monotonic clock ticks in 15.6 ms steps: times a step apart may be equal.
    assert len(sent_at) > 2 and sent_at[0] <= module.connected_at <= sent_at[-2]
    assert module.first_request_at >= sent_at[-1] + 0.1


async def test_a_module_of_another_inverter_is_refused_and_the_client_goes_on(setup: PollerSetup) -> None:
    identity = random_identity()
    relay = await setup.start_relay(identity)
    live = dataclasses.replace(LIVE_GROUP, interval_s=0.2)
    poller = await setup.start_poller(Inverter((IDENTITY_GROUP, live)), client=relay.port)
    await wait_until(lambda: poller.serial == identity.inverter_serial)

    stranger = setup.start_module(reconnect_s=30.0)
    await wait_until(lambda: poller.session is not None and poller.session.refused is not None)
    assert poller.server_refused is not None and f"not {identity.inverter_serial}" in poller.server_refused
    # Its identity read, nothing else: no handshake, no group.
    assert [frame.raw for frame in stranger.requests] == [IDENTITY_READ]
    before = len(relay.requests)
    await wait_until(lambda: len(relay.requests) > before + 2)
    assert poller.source is Source.CLIENT
    assert serial_reading(poller) == identity.inverter_serial


async def test_the_serial_given_at_the_start_is_checked_not_learned(setup: PollerSetup) -> None:
    identity = random_identity()
    poller = await setup.start_poller(serial=identity.inverter_serial)
    learned: list[str] = []
    poller.on_serial_learned = learned.append
    setup.start_module(identity)
    await wait_until(lambda: checked(poller) and default_groups_done(poller))
    assert learned == []
