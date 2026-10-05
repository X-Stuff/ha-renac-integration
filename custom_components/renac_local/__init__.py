"""Renac local: the Renac N3-HV inverter over its HF-LPT270 module, read-only."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import CONF_CLIENT, DOMAIN
from .dashboard import DASHBOARD_KEY, RenacDashboard
from .inverter_device import InverterDevice, RenacConfigEntry
from .relay_battery import RelayBattery
from .removal import check_socket_b
from .settings import CLIENT, ways_on

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Once per start, with the first entry: the Renac dashboard's keeper, which follows the registries from now on
    (dashboard.py)."""
    hass.data[DASHBOARD_KEY] = RenacDashboard(hass)
    hass.data[DASHBOARD_KEY].start()
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RenacConfigEntry) -> bool:
    """Starts the inverter device: server mode listens, client mode polls; neither waits for data. With client mode,
    an address that answers as a w2b relay gets its battery read too. An entry with neither way in (set up later) has
    its device only, no entities, until the reconfigure sets one up. The Renac dashboard is added with the first entry
    and written with each."""
    ways = ways_on(entry.data)
    if not ways:
        dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, manufacturer="Renac", name=entry.title)
    else:
        try:
            device = InverterDevice(hass, entry)
        except ValueError as error:
            raise ConfigEntryError(str(error)) from error
        await device.start()
        if CLIENT in ways:
            device.relay_battery = await RelayBattery.find(hass, entry, entry.data[CONF_CLIENT][CONF_HOST])
        entry.runtime_data = device
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hass.data[DASHBOARD_KEY].update()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RenacConfigEntry) -> bool:
    """Unloads the entry; the inverter device stops with it. An entry with neither way in has nothing to unload."""
    if getattr(entry, "runtime_data", None) is None:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RenacConfigEntry) -> None:
    """The entry is deleted and the module's socket B left as it is; it is checked (removal.py). The last entry takes
    the Renac dashboard with it."""
    await check_socket_b(hass, entry)
    dashboard = hass.data.get(DASHBOARD_KEY)
    if dashboard is not None and not [other for other in hass.config_entries.async_entries(DOMAIN) if other.entry_id != entry.entry_id]:
        await dashboard.remove()
