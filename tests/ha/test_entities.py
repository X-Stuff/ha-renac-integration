"""The entities and the diagnostics, client mode through the mock relay: the readings shown, the values shown as one,
the defaults of the per-phase sensors, an entity unavailable while its group's last read failed; the live-mode switch;
the diagnostics with the inverter's serial redacted."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.renac_local.const import DOMAIN
from custom_components.renac_local.diagnostics import async_get_config_entry_diagnostics
from custom_components.renac_local.relay_battery import battery_percent
from custom_components.renac_local.renac_protocol.groups import LIVE_GROUP
from custom_components.renac_local.renac_protocol.register_table import BATTERY_MODES
from custom_components.renac_local.renac_protocol.registers import fault_summary
from tests.ha.common import entry, groups_shown, set_up, state_of, wait_until
from tests.mocks.mock_hf_lpt270 import Identity, initial_words, random_identity
from tests.mocks.mock_relay import MockRelay

REDACTED = "**REDACTED**"


async def test_the_entities_show_the_readings_until_their_group_fails(hass: HomeAssistant, socket_enabled: None, identity: Identity) -> None:
    words = initial_words(identity)
    # PV1 and PV2 1000 and 500 W; inverter power L1-L3 100, 250 and -50 W; fault word 1 bits 0 and 2; the battery
    # charging, forced charge requested; charging forbidden.
    words.update({11002: 1000, 11005: 500, 11082: 100, 11083: 250, 11084: 0x10000 - 50, 11060: 0b101, 11040: (1 << 3) | 1, 11050: 1})
    relay = MockRelay(words, latency_s=0.005)
    await relay.start()
    try:
        config = entry(client=relay.port, serial=identity.inverter_serial)
        device = await set_up(hass, config)
        await wait_until(lambda: groups_shown(device))
        await hass.async_block_till_done()
        readings = device.data

        def state(key: str, platform: str = "sensor") -> str:
            return state_of(hass, config, platform, key).state

        pv1 = state_of(hass, config, "sensor", "pv1_voltage")
        assert pv1.entity_id == f"sensor.renac_inverter_{identity.inverter_serial[-3:]}_pv1_voltage"
        assert float(pv1.state) == readings["pv1_voltage"].value
        inverter_power = state_of(hass, config, "sensor", "inverter_power")
        phases = (inverter_power.attributes["l1"], inverter_power.attributes["l2"], inverter_power.attributes["l3"])
        assert (float(inverter_power.state), *phases) == (300, 100, 250, -50)
        pv = state_of(hass, config, "sensor", "pv_power")
        assert (float(pv.state), pv.attributes["pv1"], pv.attributes["pv2"]) == (1500, 1000, 500)
        assert float(state("meter1_power")) == readings["meter1_power"].value
        registry = er.async_get(hass)
        phase = registry.async_get(registry.async_get_entity_id("sensor", DOMAIN, f"{config.entry_id}_inverter_power_l1") or "")
        grid = registry.async_get(registry.async_get_entity_id("sensor", DOMAIN, f"{config.entry_id}_grid_voltage_l1") or "")
        assert phase is not None and phase.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert grid is not None and grid.disabled_by is None
        faults = state_of(hass, config, "sensor", "faults")
        assert (faults.state, faults.attributes["fault_1"]) == (fault_summary(readings)[0], "0005")
        battery = state_of(hass, config, "sensor", "battery_status")
        assert (battery.state, battery.attributes["forced_charge_request"], battery.attributes["balance_charge_request"]) == (BATTERY_MODES[1], True, False)
        # The mock's clock stands still at 2026-01-01 00:00:00.
        assert dt_util.parse_datetime(state("clock")) == datetime(2026, 1, 1, tzinfo=dt_util.get_default_time_zone())
        assert (state("meter_connected", "binary_sensor"), state("charge_forbidden", "binary_sensor")) == ("on", "on")
        assert (state("tcp_polling"), state("cloud_relay")) == ("active", "off")

        device.inverter.fail([LIVE_GROUP], "no connection")
        device.async_set_updated_data(device.inverter.readings())
        await hass.async_block_till_done()
        assert (state("pv1_voltage"), state("inverter_power"), state("faults")) == ("unavailable",) * 3
        assert float(state("pv_energy")) == readings["pv_energy"].value
    finally:
        await relay.stop()


async def test_the_live_mode_switch_reads_faster_holds_the_connection_and_turns_itself_off(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    config = entry(client=relay.port, serial=identity.inverter_serial, options={"live_mode_live_s": 0.5, "live_auto_off_s": 2.0})
    device = await set_up(hass, config)
    await wait_until(lambda: groups_shown(device))
    poller = device.poller
    switch = state_of(hass, config, "switch", "live_mode")
    assert switch.state == "off" and switch.attributes.get("turns_off_at") is None

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch.entity_id}, blocking=True)
    switch = state_of(hass, config, "switch", "live_mode")
    assert switch.state == "on" and isinstance(switch.attributes.get("turns_off_at"), datetime)
    assert poller.live_mode and device.inverter.interval_of(LIVE_GROUP) == 0.5
    # The live group is read again within its live interval, on a connection kept open...
    live_read_at = device.inverter.results["live"].finished_at
    await wait_until(lambda: device.inverter.results["live"].finished_at > live_read_at, timeout_s=1.5)
    await wait_until(lambda: poller.client_connection is not None and poller.client_connection.is_connected)
    # ...until live mode turns itself off, which closes it.
    await wait_until(lambda: not poller.live_mode, timeout_s=5.0)
    await hass.async_block_till_done()
    assert state_of(hass, config, "switch", "live_mode").state == "off"
    await wait_until(lambda: poller.client_connection is None)
    assert device.inverter.interval_of(LIVE_GROUP) == LIVE_GROUP.interval_s


async def test_the_relay_battery_sensors_come_with_a_w2b_relay_and_not_without(
    hass: HomeAssistant, relay: MockRelay, identity: Identity, aioclient_mock: AiohttpClientMocker
) -> None:
    # The relay page's curve: full from 4.2 V up, 50 % at 3.84 V, a straight line between the points, empty below 3.27 V.
    assert [battery_percent(mv) for mv in (4300, 3840, 3860, 3610, 3000)] == [100, 50, 58, 5, 0]
    # TCP polling's address answers as a w2b relay: two sensors from its status page, read once at the setup.
    aioclient_mock.get("http://127.0.0.1/api/status", json={"device": {"battery_mv": 3840}})
    config = entry(client=relay.port, serial=identity.inverter_serial)
    device = await set_up(hass, config)
    await wait_until(lambda: groups_shown(device))
    await hass.async_block_till_done()
    assert (state_of(hass, config, "sensor", "relay_battery").state, state_of(hass, config, "sensor", "relay_battery_voltage").state) == ("50", "3.84")
    assert aioclient_mock.call_count == 1
    # An address without the page, another bridge: no relay sensors.
    aioclient_mock.clear_requests()
    aioclient_mock.get("http://127.0.0.1/api/status", status=404)
    other_identity = random_identity()
    other = MockRelay(initial_words(other_identity), latency_s=0.005)
    await other.start()
    try:
        second = entry(client=other.port, serial=other_identity.inverter_serial, title="other")
        device = await set_up(hass, second)
        await wait_until(lambda: groups_shown(device))
        assert device.relay_battery is None
        assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{second.entry_id}_relay_battery") is None
    finally:
        await other.stop()


async def test_the_diagnostics_hide_the_inverter_serial_only(hass: HomeAssistant, relay: MockRelay) -> None:
    config = entry(client=relay.port)
    device = await set_up(hass, config)
    await wait_until(lambda: groups_shown(device))
    data = await async_get_config_entry_diagnostics(hass, config)
    assert (data["entry"]["serial"], data["poller"]["serial"], data["readings"]["serial"]) == (REDACTED,) * 3
    assert (data["entry"]["client"]["host"], data["poller"]["source"]) == (config.data["client"]["host"], "client")
    # Client mode closes its connection after every update: the last one is shown, closed, with its counters.
    assert data["connection"]["open"] is False and data["connection"]["requests"]["replies"] > 0
    assert data["groups"]["live"]["error"] is None and data["readings"]["model"] == device.data["model"].text
