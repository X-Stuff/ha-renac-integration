"""The Renac dashboard: the views built from what the registry has of an inverter; the dashboard in lovelace, written
and following the registry until the user edits it, rebuilt on request, and an edited one kept through the last
entry's removal while an untouched one goes."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.lovelace.const import LOVELACE_DATA, ConfigNotFound
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.renac_local.const import DOMAIN
from custom_components.renac_local.dashboard import DASHBOARD, DASHBOARD_KEY, URL_PATH
from custom_components.renac_local.dashboard_views import POWER_GRAPH, InverterEntities, Shown, build_dashboard
from tests.ha.common import entry, groups_shown, set_up, wait_until
from tests.mocks.mock_hf_lpt270 import Identity
from tests.mocks.mock_relay import MockRelay


def test_the_views_hold_what_the_inverter_has_and_nothing_empty() -> None:
    shown = {
        key: Shown(f"sensor.x_{key}", key.replace("_", " ").capitalize())
        for key in ("pv_power", "internal_temperature", "bms_max_cell_voltage", "faults", "relay_battery")
    }
    shown["live_mode"] = Shown("switch.x_live_mode", "Live mode")
    inverter = InverterEntities("N3-HV-10.0", "8301033231001064", "entry1", shown)
    tab, live, energy = build_dashboard([inverter], "renac-local")["views"]

    assert (tab["title"], tab["path"], tab["header"]["card"]["content"]) == ("N3-HV-10.0", "inverter-064", "# N3-HV-10.0")
    # The badges in their order, each named, the relay's battery only while it has a value, live mode's leading to the
    # live subview.
    assert [badge["name"] for badge in tab["badges"]] == ["Faults", "Relay battery", "Live mode"]
    assert tab["badges"][1]["visibility"][0] == {"condition": "state", "entity": "sensor.x_relay_battery", "state_not": "unavailable"}
    assert tab["badges"][-1]["tap_action"]["navigation_path"] == "/renac-local/inverter-064-live"
    headings = [section["cards"][0]["heading"] for section in tab["sections"]]
    assert headings == ["Now", "Inverter", "PV", "Today", "Battery"]
    inverter_section = tab["sections"][1]["cards"][1:]
    assert [card["type"] for card in inverter_section] == ["sensor"] and inverter_section[0]["name"] == "Internal temperature"
    # The power graph alone, this inverter having no SoC for the second one.
    graphs = tab["sections"][3]["cards"][1:]
    assert tab["sections"][3]["column_span"] == 2 and [graph["type"] for graph in graphs] == ["statistics-graph"]
    assert graphs[0]["entities"] == [{"entity": "sensor.x_pv_power", "name": "Pv power", "color": POWER_GRAPH["pv_power"]}]
    assert tab["sections"][4]["cards"][1]["entities"] == [{"entity": "sensor.x_bms_max_cell_voltage", "name": "Bms max cell voltage"}]

    assert (live["path"], live["subview"]) == ("inverter-064-live", True)
    assert [section["cards"][0]["heading"] for section in live["sections"]] == ["Live mode", "PV", "Inverter", "Battery"]
    assert live["sections"][0]["column_span"] == 4 and live["sections"][2]["cards"][1]["entities"][1]["name"] == "Faults"
    assert energy["title"] == "Energy"
    # No inverter: a note, and the Energy tab all the same.
    assert [view["title"] for view in build_dashboard([], "renac-local")["views"]] == ["Renac", "Energy"]


async def test_the_dashboard_follows_the_registry_until_edited_and_an_edited_one_outlives_the_last_entry(
    hass: HomeAssistant, relay: MockRelay, identity: Identity
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    config = entry(client=relay.port)
    device = await set_up(hass, config)
    await wait_until(lambda: groups_shown(device))
    dashboards = hass.data[LOVELACE_DATA].dashboards
    storage = dashboards[URL_PATH]
    assert frontend.async_panel_exists(hass, URL_PATH)

    async def stored() -> dict[str, Any] | None:
        try:
            return await storage.async_load(False)
        except ConfigNotFound:
            return None

    async def wait_for(condition: Any, timeout_s: float = 5.0) -> dict[str, Any]:
        for _ in range(int(timeout_s / 0.05)):
            if (current := await stored()) is not None and condition(current):
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("the dashboard did not change in time")

    def row_names(current: dict[str, Any]) -> list[str]:
        return [row["name"] for section in current["views"][0]["sections"] if section["cards"][0]["heading"] == "PV" for row in section["cards"][1]["entities"]]

    # The inverter's tab, written a moment after its entities appeared.
    written = await wait_for(lambda config: config["views"][0]["title"] == identity.model)
    assert "PV1 voltage" in row_names(written)
    # A rename in the registry follows.
    registry = er.async_get(hass)
    pv1 = registry.async_get_entity_id("sensor", DOMAIN, f"{config.entry_id}_pv1_voltage")
    assert pv1 is not None
    registry.async_update_entity(pv1, name="String 1 voltage")
    await wait_for(lambda config: "String 1 voltage" in row_names(config))

    # The user's edit ends the following; Rebuild writes over it.
    edited = {**written, "title": "Mine"}
    await storage.async_save(edited)
    registry.async_update_entity(pv1, name="PV1 voltage")
    await asyncio.sleep(1.5)
    assert await stored() == edited
    await hass.data[DASHBOARD_KEY].rebuild()
    assert (await stored())["title"] == "Renac" and "PV1 voltage" in row_names(await stored())

    # Edited again, the dashboard outlives the last entry: out of the sidebar, its views kept for the next setup.
    await storage.async_save(edited)
    await hass.config_entries.async_remove(config.entry_id)
    await hass.async_block_till_done()
    assert URL_PATH not in dashboards and not frontend.async_panel_exists(hass, URL_PATH)
    assert await LovelaceStorage(hass, DASHBOARD).async_load(False) == edited

    # Untouched, it goes with the last entry.
    again = entry(client=relay.port, serial=identity.inverter_serial)
    await set_up(hass, again)
    assert await hass.data[LOVELACE_DATA].dashboards[URL_PATH].async_load(False) == edited
    await hass.data[DASHBOARD_KEY].rebuild()
    await hass.config_entries.async_remove(again.entry_id)
    await hass.async_block_till_done()
    try:
        await LovelaceStorage(hass, DASHBOARD).async_load(False)
    except ConfigNotFound:
        pass
    else:
        raise AssertionError("the untouched dashboard stayed")
