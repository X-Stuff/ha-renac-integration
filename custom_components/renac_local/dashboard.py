"""The Renac dashboard (PLAN §6.6): the integration's own, in the sidebar, its views built from the registry
(dashboard_views.py) and written while the user has not edited them.

Home Assistant has no public call for an integration to add a dashboard. The integration puts a storage dashboard
into lovelace's dashboards and registers its sidebar panel with the frontend's panel call, when an entry is set up.
HA's dashboards list shows it, but its title, icon and deletion there are lovelace's own collection, which an
integration cannot reach: they are the integration's. Two of lovelace's internals are used, its data and its storage
class; a change in them costs the dashboard, not the integration. The views are plain stored views, no script of the
integration's: a script an integration registers reaches only pages loaded after its setup, so every page loaded while
Home Assistant starts would lack it.

The views are written at an entry's setup, a second after a change of the integration's entities or devices in the
registries (a new inverter's entities, an entity enabled, a rename), and at an entry's removal, while the stored
dashboard is still what the integration wrote last, which its own store remembers. The user's first edit, in HA's
editor, ends that: the dashboard is theirs, until Rebuild in the Configure menu writes the views over it. The last
entry's removal takes the sidebar item with it, and deletes the stored views when they are the integration's; edited
ones stay for the next setup.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import frontend
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .dashboard_views import InverterEntities, Shown, build_dashboard

try:
    from homeassistant.components.lovelace.const import LOVELACE_DATA, ConfigNotFound
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    LOVELACE_INTERNALS = True
except ImportError:  # Changed in this Home Assistant: no dashboard; the rest of the integration works.
    LOVELACE_INTERNALS = False

LOGGER = logging.getLogger(__name__)

URL_PATH = "renac-local"
TITLE = "Renac"
ICON = "mdi:solar-power-variant"
# What lovelace keeps about a storage dashboard; `id` names its store, lovelace.renac_local.
DASHBOARD = {"id": DOMAIN, "url_path": URL_PATH, "title": TITLE, "icon": ICON, "require_admin": False, "show_in_sidebar": True, "mode": "storage"}
# A burst of registry changes, an inverter's entities created, is one rebuild.
REBUILD_DELAY_S = 1.0

DASHBOARD_KEY: HassKey[RenacDashboard] = HassKey(f"{DOMAIN}_dashboard")


def inverters_in_registry(hass: HomeAssistant) -> list[InverterEntities]:
    """Each entry's inverter as the registries have it: the device's name and serial, the entities enabled and not
    hidden, by key; an entry without entities is left out. Sorted by name."""
    devices, entities = dr.async_get(hass), er.async_get(hass)
    found = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        device = devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        shown = {}
        for registered in er.async_entries_for_config_entry(entities, entry.entry_id):
            if registered.disabled_by is None and registered.hidden_by is None and registered.translation_key:
                shown[registered.translation_key] = Shown(registered.entity_id, registered.name or registered.original_name or registered.entity_id)
        if device is not None and shown:
            found.append(InverterEntities(device.name_by_user or device.name or entry.title, device.serial_number, entry.entry_id, shown))
    return sorted(found, key=lambda inverter: inverter.name)


class RenacDashboard:
    """The dashboard's keeper: puts it in lovelace and the sidebar, writes its views while they are the integration's,
    follows the registries, takes it out with the last entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # The views written last: the stored dashboard is the user's once it differs from them.
        self._written = Store[dict[str, Any]](hass, 1, f"{DOMAIN}.dashboard")
        self._rebuild = Debouncer(hass, LOGGER, cooldown=REBUILD_DELAY_S, immediate=False, function=self.update)

    @callback
    def start(self) -> None:
        """Follows the registries: a change of one of the integration's entities or devices rebuilds the views."""
        self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_change, event_filter=self._is_our_entity)
        self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_registry_change, event_filter=self._is_our_device)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)

    async def update(self) -> None:
        """The views rebuilt from the registry and written, unless the user edited the dashboard: then it is theirs."""
        if not self.hass.config_entries.async_entries(DOMAIN):
            # The last entry gone: its removal took the dashboard, and a late change does not bring it back.
            return
        storage = self._storage()
        if storage is None:
            return
        stored = await self._stored(storage)
        if stored is not None and stored != await self._last_written():
            return
        await self._write(storage, stored)

    async def rebuild(self) -> None:
        """The views rebuilt and written over whatever is there, the user's edits included: the Configure menu's
        Rebuild."""
        storage = self._storage()
        if storage is not None:
            await self._write(storage, await self._stored(storage))

    async def remove(self) -> None:
        """Out of the sidebar and lovelace, with the last entry. The stored views are deleted when they are still the
        integration's; edited ones stay for the next setup."""
        self._rebuild.async_cancel()
        if not LOVELACE_INTERNALS or LOVELACE_DATA not in self.hass.data:
            return
        storage = self.hass.data[LOVELACE_DATA].dashboards.pop(URL_PATH, None)
        if storage is None:
            return
        frontend.async_remove_panel(self.hass, URL_PATH, warn_if_unknown=False)
        stored = await self._stored(storage)
        if stored is None or stored == await self._last_written():
            await storage.async_delete()
        await self._written.async_remove()

    def _storage(self) -> Any:
        """The dashboard in lovelace's dashboards, put there with its panel on the first call; None without lovelace."""
        if not LOVELACE_INTERNALS or LOVELACE_DATA not in self.hass.data:
            LOGGER.debug("no lovelace: the Renac dashboard is not added")
            return None
        dashboards = self.hass.data[LOVELACE_DATA].dashboards
        if URL_PATH not in dashboards:
            try:
                frontend.async_register_built_in_panel(
                    self.hass, "lovelace", sidebar_title=TITLE, sidebar_icon=ICON, frontend_url_path=URL_PATH, config={"mode": "storage"}, require_admin=False
                )
            except ValueError as error:
                LOGGER.warning("the Renac dashboard is not added: %s", error)
                return None
            dashboards[URL_PATH] = LovelaceStorage(self.hass, DASHBOARD)
            LOGGER.info("the Renac dashboard is in the sidebar")
        return dashboards[URL_PATH]

    async def _stored(self, storage: Any) -> dict[str, Any] | None:
        try:
            return await storage.async_load(False)
        except ConfigNotFound:
            return None

    async def _last_written(self) -> dict[str, Any] | None:
        written = await self._written.async_load()
        return written["config"] if written else None

    async def _write(self, storage: Any, stored: dict[str, Any] | None) -> None:
        config = build_dashboard(inverters_in_registry(self.hass), URL_PATH)
        if config == stored:
            return
        await storage.async_save(config)
        await self._written.async_save({"config": config})
        LOGGER.debug("the Renac dashboard's views written, %d views", len(config["views"]))

    @callback
    def _is_our_entity(self, data: Any) -> bool:
        registered = er.async_get(self.hass).async_get(data["entity_id"])
        return registered is not None and registered.platform == DOMAIN

    @callback
    def _is_our_device(self, data: Any) -> bool:
        device = dr.async_get(self.hass).async_get(data["device_id"])
        return device is not None and any(domain == DOMAIN for domain, _ in device.identifiers)

    @callback
    def _on_registry_change(self, _event: Event[Any]) -> None:
        self._rebuild.async_schedule_call()

    @callback
    def _on_stop(self, _event: Event[Any]) -> None:
        self._rebuild.async_shutdown()
