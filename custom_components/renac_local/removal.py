"""The removal's check (PLAN §6.8): deleting an entry leaves the module's socket B as it is. When the module answered
Home Assistant before, its socket B is read after the deletion; when it is not the cloud the entry kept, or the module
does not answer, a notification says so: the Renac app gets no new data until socket B is set back."""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .config_flow import kept_cloud
from .const import CONF_MAC, CONF_MODULE, DOMAIN
from .renac_protocol import AtError, AtSession
from .renac_protocol.const import AT_PORT

LOGGER = logging.getLogger(__name__)


async def check_socket_b(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reads the module's socket B, the AT query, which changes nothing, and notifies when it is not the kept cloud or
    the module does not answer. Nothing for an entry whose module never answered: no module IP or no MAC."""
    module = entry.data.get(CONF_MODULE) or {}
    ip = module.get(CONF_HOST)
    if not ip or not module.get(CONF_MAC):
        return
    cloud = kept_cloud(entry.data)
    # Markdown: host names and addresses in italics.
    try:
        async with AtSession(ip, AT_PORT) as session:
            socket_b = await session.read_socket_b()
    except AtError as error:
        LOGGER.warning("%s removed; its module %s: %s", entry.title, ip, error)
        found = f"Home Assistant could not check where its WiFi module sends its data:\n- the module did not answer ({error})."
        start = "If the Renac app gets no new data, "
    else:
        if socket_b == cloud:
            return
        LOGGER.warning("%s removed; socket B of its module %s dials %s, the cloud kept is %s", entry.title, ip, socket_b, cloud)
        if cloud is not None:
            found = f"Its WiFi module sends its data to _{socket_b.address}_, not to Renac's cloud (_{cloud.address}_).\n- So the Renac app gets no new data."
            start = "To change this, "
        else:
            found = f"Its WiFi module sends its data to _{socket_b.address}_.\n- Home Assistant does not know the address of Renac's cloud."
            start = "If the Renac app gets no new data, "
    fix = start + "add the inverter again, then use Configure and Set cloud URL" + (f" with _{cloud.address}_." if cloud else ".")
    persistent_notification.async_create(
        hass, f"You removed _{entry.title}_.\n\n{found}\n\n{fix}", title="Renac inverter removed", notification_id=f"{DOMAIN}_{entry.entry_id}_removed"
    )
