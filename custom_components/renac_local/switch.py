"""The live-mode switch (PLAN §6.6): on, the live data is read every few seconds and the energy counters every minute,
as the settings say, and TCP polling keeps its connection open; it turns itself off after the settings' auto-off, the
time it does so as an attribute. Added with the other entities, once the inverter's serial is known."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RenacEntity, add_when_serial_known
from .inverter_device import InverterDevice, RenacConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: RenacConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    add_when_serial_known(SWITCH_DOMAIN, entry, async_add_entities, lambda: [LiveModeSwitch(entry.runtime_data)])


class LiveModeSwitch(RenacEntity, SwitchEntity):
    """Live mode on or off; `turns_off_at` says when it turns itself off."""

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "live_mode", "Live mode")

    @property
    def is_on(self) -> bool:
        return self.coordinator.poller.live_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"turns_off_at": self.coordinator.live_off_at}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_live_mode(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_live_mode(False)
