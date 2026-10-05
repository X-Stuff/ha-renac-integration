"""The inverter device: one entry's inverter, its readings from the core's inverter, pushed to the entities.

`InverterDevice` builds the core's inverter and poller from the entry's data and options: server mode's and client
mode's settings as enabled, the cloud's with server mode when the entry names a cloud, the serial learned before, and
the settings of the Configure menu (settings.py). The poller reads on its own, whichever way is in use; every change
it reports is pushed to the entities as the inverter's readings by key. A
reading keeps its value until a read of its group fails; then its entity is unavailable. The serial the poller learns
at the first connection goes into the entry's data. The device is the entry's, registered at setup; the model, the
serial and the firmware fill it in as they are read. The entry is renamed `Renac <model> <serial>` once both are
known, while its title is the one the integration gave last. A cloud write outside the clock is reported as an event;
a blocked one is also a line in the entry's notification, which lists every blocked write since it was last dismissed.
While the inverter has never answered, a Repairs issue says why (issues.py). Live mode (PLAN §6.6), from its switch:
the poller's live intervals and, in client mode, the connection held; it turns itself off after the settings' auto-off.
The w2b relay's battery, when TCP polling's address is one (relay_battery.py), is read apart from the inverter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BIND,
    CONF_CLIENT,
    CONF_CLOUD_HOST,
    CONF_CLOUD_PORT,
    CONF_DNS_SERVER,
    CONF_GIVEN_TITLE,
    CONF_SERIAL,
    CONF_SERVER,
    DOMAIN,
    EVENT_CLOUD_WRITE,
)
from .issues import NoAnswerIssue
from .relay_battery import RelayBattery
from .renac_protocol import ChannelError, ClientSettings, CloudAddress, CloudSettings, CloudWrite, Inverter, Poller, Reading, ServerSettings
from .renac_protocol.const import CLOUD_PORT, DEFAULT_BIND, DEFAULT_DNS_SERVER, DEFAULT_SERVER_PORT
from .settings import CLIENT, CLOUD, SERVER, Settings, ways_on

LOGGER = logging.getLogger(__name__)

# The most blocked cloud writes the notification lists; the earlier ones are counted, each one is in the log.
LISTED_WRITES = 100


type RenacConfigEntry = ConfigEntry[InverterDevice]


def build_inverter(settings: Settings) -> Inverter:
    """The entry's inverter with the settings' schedule: the intervals, live mode's, the groups on, the age limit."""
    return Inverter(intervals=settings.intervals, live_intervals=settings.live_intervals, enabled=settings.enabled, age_intervals=settings.age_intervals)


def build_poller(inverter: Inverter, data: Mapping[str, Any], settings: Settings) -> Poller:
    """The entry's poller over the inverter: server mode and client mode as enabled, the cloud with server mode when
    one is named, each with its timing and policies from the settings. Raises ValueError when neither way is enabled."""
    server = data.get(CONF_SERVER) or {}
    client = data.get(CONF_CLIENT) or {}
    ways = ways_on(data)
    server_settings = None
    cloud_settings = None
    client_settings = None
    if SERVER in ways:
        bind, port = server.get(CONF_BIND, DEFAULT_BIND), server.get(CONF_PORT, DEFAULT_SERVER_PORT)
        server_settings = ServerSettings(bind, port, reply_timeout_s=settings.reply_timeout_s)
    if CLOUD in ways:
        address = CloudAddress(server[CONF_CLOUD_HOST], server.get(CONF_CLOUD_PORT, CLOUD_PORT), server.get(CONF_DNS_SERVER, DEFAULT_DNS_SERVER))
        cloud_settings = CloudSettings(address, forward_unknown_writes=settings.forward_writes, forward_bad_crc=settings.forward_bad_crc)
    if CLIENT in ways:
        client_settings = ClientSettings(
            client[CONF_HOST],
            client[CONF_PORT],
            reply_timeout_s=settings.reply_timeout_s,
            first_reply_timeout_s=settings.first_reply_timeout_s,
            quiet_time_s=settings.quiet_time_s,
            connect_timeout_s=settings.connect_timeout_s,
        )
    return Poller(
        inverter, server=server_settings, client=client_settings, cloud=cloud_settings, serial=data.get(CONF_SERIAL), send_handshake=settings.handshake
    )


class InverterDevice(DataUpdateCoordinator[dict[str, Reading]]):
    """The entry's inverter: the poller it runs, the data it pushes, the device it keeps up to date. Raises ValueError
    when the entry enables neither way in."""

    config_entry: RenacConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RenacConfigEntry) -> None:
        settings = Settings.from_options(entry.options)
        inverter = build_inverter(settings)
        poller = build_poller(inverter, entry.data, settings)
        super().__init__(hass, LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}", update_interval=None)
        self.settings = settings
        self.inverter = inverter
        self.poller = poller
        self.data = {}
        # The w2b relay's battery, when TCP polling's address is a w2b relay; found at the setup (__init__.py).
        self.relay_battery: RelayBattery | None = None
        # When live mode turns itself off, while it is on.
        self.live_off_at: datetime | None = None
        self._live_off_timer: Callable[[], None] | None = None
        self._device_info: tuple[str | None, str | None, str | None] | None = None
        # The cloud's blocked writes since the notification was last dismissed, one line each, and how many were left
        # out.
        self._writes_listed: list[str] = []
        self._writes_left_out = 0
        self._notification_id = f"{DOMAIN}_{entry.entry_id}_cloud_writes"
        self.no_answer = NoAnswerIssue(hass, entry.entry_id, entry.title, poller, inverter)
        poller.on_update = self._on_poller_update
        poller.on_serial_learned = self._on_serial_learned
        poller.on_cloud_write = self._on_cloud_write

    async def start(self) -> None:
        """Registers the device and starts the poller. Raises ConfigEntryNotReady when the server's port cannot be
        opened."""
        self._update_device()
        try:
            await self.poller.start()
        except ChannelError as error:
            raise ConfigEntryNotReady(f"cannot listen for the module: {error}") from error
        self.config_entry.async_on_unload(persistent_notification.async_register_callback(self.hass, self._on_notifications))
        self.no_answer.start()
        self.config_entry.async_on_unload(self.no_answer.stop)

    async def _async_update_data(self) -> dict[str, Reading]:
        """Nothing to fetch: the poller pushes the inverter's readings."""
        return self.inverter.readings()

    @callback
    def set_live_mode(self, on: bool) -> None:
        """Live mode on or off (PLAN §6.6). On, it turns itself off after the settings' auto-off."""
        if self._live_off_timer is not None:
            self._live_off_timer()
            self._live_off_timer = None
        self.poller.live_mode = on
        self.live_off_at = None
        if on:
            auto_off_s = self.settings.live_auto_off_s
            self.live_off_at = dt_util.utcnow() + timedelta(seconds=auto_off_s)
            self._live_off_timer = async_call_later(self.hass, auto_off_s, self._on_live_auto_off)
        self.async_set_updated_data(self.inverter.readings())

    @callback
    def _on_live_auto_off(self, _now: datetime) -> None:
        self._live_off_timer = None
        LOGGER.info("%s: live mode off by itself after %g s", self.config_entry.title, self.settings.live_auto_off_s)
        self.set_live_mode(False)

    @callback
    def _on_poller_update(self) -> None:
        self._update_device()
        self._update_title()
        self.async_set_updated_data(self.inverter.readings())
        self.no_answer.update()

    @callback
    def _on_serial_learned(self, serial: str) -> None:
        """The first connection's serial: kept in the entry, checked at every connection from now on."""
        entry = self.config_entry
        LOGGER.info("%s: the inverter %s", entry.title, serial)
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_SERIAL: serial})

    @callback
    def _on_cloud_write(self, write: CloudWrite) -> None:
        """A cloud write outside the clock: an event. A blocked one is also a line added to the notification, which
        lists every blocked write since it was last dismissed; a forwarded one, which the user allowed in the settings,
        gets no line."""
        entry = self.config_entry
        event = {"entry_id": entry.entry_id, "address": write.address, "count": write.count, "values": list(write.values), "forwarded": write.forwarded}
        self.hass.bus.async_fire(EVENT_CLOUD_WRITE, event)
        if write.forwarded:
            return
        at = dt_util.as_local(dt_util.utc_from_timestamp(write.at)).strftime("%Y-%m-%d %H:%M:%S")
        self._writes_listed.append(f"- {at}: registers {write.describe()}")
        if len(self._writes_listed) > LISTED_WRITES:
            self._writes_listed.pop(0)
            self._writes_left_out += 1
        lines = [f"Home Assistant blocked these writes from Renac's cloud to {entry.title} since this notice was last dismissed:", ""]
        if self._writes_left_out:
            lines.append(f"- {self._writes_left_out} earlier ones, each in the log")
        lines += self._writes_listed
        lines.append("")
        lines.append("The cloud may set only the inverter's clock.")
        lines.append('To let it change other settings too, turn on "pass the cloud\'s writes" in Configure, Settings.')
        persistent_notification.async_create(self.hass, "\n".join(lines), title="Renac cloud writes blocked", notification_id=self._notification_id)

    @callback
    def _on_notifications(self, update: persistent_notification.UpdateType, notifications: dict[str, persistent_notification.Notification]) -> None:
        """The notification dismissed: the next write starts a new list."""
        if update is persistent_notification.UpdateType.REMOVED and self._notification_id in notifications:
            self._writes_listed.clear()
            self._writes_left_out = 0

    def _model(self) -> str | None:
        model = self.inverter.readings().get("model")
        return model.text if model is not None and model.text else None

    def _update_title(self) -> None:
        """The entry renamed `Renac <model> <serial>` once both are known, while its title is the one the integration
        gave last: the setup's `Renac <module IP>`, or this name for the inverter before a forgotten serial. A title the
        user typed stays."""
        model, serial = self._model(), self.poller.serial
        entry = self.config_entry
        if model is None or serial is None or entry.title != entry.data.get(CONF_GIVEN_TITLE):
            return
        title = f"Renac {model} {serial}"
        if title != entry.title:
            LOGGER.info("%s renamed %s", entry.title, title)
            self.hass.config_entries.async_update_entry(entry, title=title, data={**entry.data, CONF_GIVEN_TITLE: title})

    def _update_device(self) -> None:
        """The entry's device, with the model, the serial and the firmware once they are known; named after the model,
        the entry's title until a model is first read. What is not known yet is left as the registry has it, so a
        restart changes nothing."""
        readings = self.inverter.readings()
        model_text = self._model()
        versions = [readings.get(key) for key in ("master_version", "slave_version", "hmi_version")]
        firmware = None
        if all(version is not None for version in versions):
            master, slave, hmi = (version.text for version in versions if version is not None)
            firmware = f"master {master}, slave {slave}, HMI {hmi}"
        info = (model_text, self.poller.serial, firmware)
        if info == self._device_info:
            return
        self._device_info = info
        entry = self.config_entry
        registry = dr.async_get(self.hass)
        new = registry.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id) is None
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Renac",
            model=model_text or UNDEFINED,
            name=model_text or (entry.title if new else UNDEFINED),
            serial_number=self.poller.serial or UNDEFINED,
            sw_version=firmware or UNDEFINED,
        )

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._live_off_timer is not None:
            self._live_off_timer()
            self._live_off_timer = None
        await self.poller.stop()
