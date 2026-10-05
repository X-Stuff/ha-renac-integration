"""The inverter device against the mocks: client mode through the mock relay, server mode with the fake module dialling
in and the mock cloud, both in one entry; the serial learned into the entry and checked; the entry's device and its
title; a cloud write outside the clock reported."""

from __future__ import annotations

import asyncio
import time

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.renac_local import issues
from custom_components.renac_local.const import CONF_CLOUD_HOST, CONF_CLOUD_PORT, CONF_GIVEN_TITLE, CONF_SERIAL, DOMAIN, EVENT_CLOUD_WRITE
from custom_components.renac_local.inverter_device import InverterDevice
from custom_components.renac_local.renac_protocol import CloudWrite
from custom_components.renac_local.renac_protocol.poller import Source
from tests.ha.common import entry, free_port, groups_shown, set_up, state_of, wait_until
from tests.mocks.mock_cloud import OUTSIDE_CLOCK_WRITE, MockCloud
from tests.mocks.mock_hf_lpt270 import FakeModule, Identity, SimulatedInverter, TcpModule, initial_words
from tests.mocks.mock_relay import MockRelay

FIRMWARE = "master 1.23, slave 1.02, HMI 2.04"


def assert_device(hass: HomeAssistant, entry_id: str, identity: Identity) -> None:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry_id), entry_id)
    assert device is not None
    assert (device.manufacturer, device.model, device.name) == ("Renac", identity.model, identity.model)
    assert (device.serial_number, device.sw_version) == (identity.inverter_serial, FIRMWARE)


def dial(port: int, identity: Identity) -> asyncio.Task[None]:
    """The fake module, as `identity`, dialling 127.0.0.1:`port` until the task is cancelled."""
    fake = FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True)
    return asyncio.create_task(TcpModule(fake, "127.0.0.1", port, reconnect_s=0.2).run())


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def session_checked(device: InverterDevice) -> bool:
    return device.poller.session is not None and device.poller.session.checked


async def test_client_mode_learns_the_serial_fills_the_device_and_pushes_updates(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    config = entry(client=relay.port)
    device = await set_up(hass, config)
    updates: list[None] = []
    remove = device.async_add_listener(lambda: updates.append(None))
    try:
        await wait_until(lambda: groups_shown(device))
    finally:
        remove()

    assert config.data[CONF_SERIAL] == identity.inverter_serial and config.unique_id is None
    assert device.poller.source is Source.CLIENT
    assert device.data["model"].value == identity.model
    assert all(result.ok for result in device.inverter.results.values())
    assert updates
    assert_device(hass, config.entry_id, identity)


async def test_the_learned_serial_comes_back_with_the_entry(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    config = entry(client=relay.port)
    device = await set_up(hass, config)
    await wait_until(lambda: CONF_SERIAL in config.data)
    assert await hass.config_entries.async_reload(config.entry_id)
    await hass.async_block_till_done()
    device = config.runtime_data
    assert device.poller.serial == identity.inverter_serial
    await wait_until(lambda: groups_shown(device))
    assert device.poller.client_refused is None


@pytest.mark.parametrize(("title", "renamed"), [("Renac 127.0.0.1", True), ("My inverter", False)])
async def test_the_entry_is_named_after_the_inverter_unless_the_user_named_it(
    hass: HomeAssistant, relay: MockRelay, identity: Identity, title: str, renamed: bool
) -> None:
    # The serial known at setup, as after the setup's TCP test: named at the first read all the same.
    config = entry(client=relay.port, title=title, serial=identity.inverter_serial, given_title="Renac 127.0.0.1")
    device = await set_up(hass, config)
    await wait_until(lambda: "model" in device.data)
    name = f"Renac {identity.model} {identity.inverter_serial}"
    assert (config.title, config.data[CONF_GIVEN_TITLE]) == ((name, name) if renamed else (title, "Renac 127.0.0.1"))


async def test_a_serial_other_than_the_entrys_is_refused_and_nothing_is_read(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    config = entry(client=relay.port, serial="1234567890123456")
    device = await set_up(hass, config)
    await wait_until(lambda: device.poller.client_refused is not None)
    assert f"the inverter {identity.inverter_serial} answers, not 1234567890123456" == device.poller.client_refused
    assert device.data == {}
    assert config.data[CONF_SERIAL] == "1234567890123456"


async def test_server_mode_relays_the_cloud_learns_the_serial_and_reports_a_blocked_write(
    hass: HomeAssistant, socket_enabled: None, identity: Identity
) -> None:
    cloud = MockCloud()
    cloud_port = await cloud.start()
    port = free_port()
    config = entry(server=port, cloud={CONF_CLOUD_HOST: "127.0.0.1", CONF_CLOUD_PORT: cloud_port})
    writes = async_capture_events(hass, EVENT_CLOUD_WRITE)
    running = dial(port, identity)
    try:
        device = await set_up(hass, config)
        # The cloud's first poll, about 55 requests 100 ms apart besides the poller's own reads: some 8 s.
        await wait_until(cloud.session_done.is_set, timeout_s=30.0)
        await wait_until(lambda: groups_shown(device))
        assert session_checked(device) and device.poller.source is Source.SERVER
        assert device.poller.cloud is not None and device.poller.cloud.status.connected
        assert config.data[CONF_SERIAL] == identity.inverter_serial
        identity_read = next(exchange for exchange in cloud.exchanges if exchange.request[:4] == bytes.fromhex("01032710"))
        assert identity_read.reply is not None
        assert_device(hass, config.entry_id, identity)
        address = int.from_bytes(OUTSIDE_CLOCK_WRITE[2:4], "big")
        assert [(write.data["address"], write.data["forwarded"]) for write in writes] == [(address, False)]
        await hass.async_block_till_done()
        assert state_of(hass, config, "binary_sensor", "cloud").state == "on"
        assert (state_of(hass, config, "sensor", "cloud_relay").state, state_of(hass, config, "sensor", "tcp_polling").state) == ("active", "off")
    finally:
        await stop(running)
        await cloud.stop()


async def test_both_ways_the_module_first_the_client_while_it_is_away(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    port = free_port()
    config = entry(server=port, client=relay.port)
    device = await set_up(hass, config)
    await wait_until(lambda: device.poller.source is Source.CLIENT and "live" in device.inverter.results)

    running = dial(port, identity)
    try:
        await wait_until(lambda: device.poller.source is Source.SERVER and session_checked(device))
    finally:
        await stop(running)
    assert device.poller.serial == identity.inverter_serial


async def test_no_entities_and_an_issue_until_the_inverter_first_answers(
    hass: HomeAssistant, socket_enabled: None, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(issues, "NO_ANSWER_GRACE_S", 0.1)
    # Nothing listens on the client's port: its dial fails, as on the module's own 8899.
    port, closed = free_port(), free_port()
    config = entry(server=port, client=closed)
    await set_up(hass, config)
    issue_id = f"no_answer_{config.entry_id}"

    def reasons() -> str:
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        return issue.translation_placeholders["reasons"] if issue is not None and issue.translation_placeholders else ""

    await wait_until(lambda: f"127.0.0.1:{closed}" in reasons())
    assert f"the module has not connected to Home Assistant (port {port})" in reasons()
    # Its Fix opens the entry's reconfigure.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable and issue.data == {"entry_id": config.entry_id}
    registry = er.async_get(hass)
    assert er.async_entries_for_config_entry(registry, config.entry_id) == []

    running = dial(port, identity)
    try:
        pv1 = f"sensor.renac_inverter_{identity.inverter_serial[-3:]}_pv1_voltage"
        await wait_until(lambda: registry.async_get(pv1) is not None)
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    finally:
        await stop(running)


async def test_cloud_writes_are_events_and_the_blocked_ones_add_up_in_one_notification_until_it_is_dismissed(hass: HomeAssistant, relay: MockRelay) -> None:
    device = await set_up(hass, entry(client=relay.port))
    events = async_capture_events(hass, EVENT_CLOUD_WRITE)
    shown: dict[str, str] = {}

    @callback
    def on_notifications(update: persistent_notification.UpdateType, notifications: dict[str, persistent_notification.Notification]) -> None:
        if update is not persistent_notification.UpdateType.REMOVED:
            shown.update({key: notification["message"] for key, notification in notifications.items()})

    remove = persistent_notification.async_register_callback(hass, on_notifications)
    try:
        report = device.poller.on_cloud_write
        assert report is not None
        report(CloudWrite(20007, 1, (1,), forwarded=False, at=time.time()))
        report(CloudWrite(21000, 1, (2,), forwarded=True, at=time.time()))
        assert [(event.data["address"], event.data["forwarded"]) for event in events] == [(20007, False), (21000, True)]
        # The forwarded one, allowed in the settings, gets no line.
        [(notification_id, message)] = shown.items()
        assert "20007 x1 = 1" in message and "21000" not in message
        persistent_notification.async_dismiss(hass, notification_id)
        report(CloudWrite(21016, 1, (3,), forwarded=False, at=time.time()))
        assert "21016 x1 = 3" in shown[notification_id] and "20007" not in shown[notification_id]
    finally:
        remove()
