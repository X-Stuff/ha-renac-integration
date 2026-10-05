"""The inverter's binary sensors: the BMS and Meter 1 connected, the battery's charging and discharging forbidden, and
in server mode the cloud connected."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import BINARY_KEYS
from .entity import RegisterEntity, RenacEntity, add_when_serial_known, registers_shown
from .inverter_device import InverterDevice, RenacConfigEntry
from .renac_protocol import Register

# The binary sensors that say whether a part is connected; the others are flags.
CONNECTED_KEYS = ("bms_connected", "meter_connected")


async def async_setup_entry(hass: HomeAssistant, entry: RenacConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    add_when_serial_known(BINARY_SENSOR_DOMAIN, entry, async_add_entities, lambda: binary_sensors(entry.runtime_data))


def binary_sensors(device: InverterDevice) -> list[RenacEntity]:
    entities: list[RenacEntity] = [RegisterBinarySensor(device, register) for register in registers_shown(device.inverter) if register.key in BINARY_KEYS]
    if device.poller.cloud_settings is not None:
        entities.append(CloudSensor(device))
    return entities


class RegisterBinarySensor(RegisterEntity, BinarySensorEntity):
    """A register that is on or off, 0 off: a part connected, or a flag set."""

    def __init__(self, device: InverterDevice, register: Register) -> None:
        super().__init__(device, register)
        if register.key in CONNECTED_KEYS:
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        reading = self.reading
        return reading.value != 0 if reading is not None else None


class CloudSensor(RenacEntity, BinarySensorEntity):
    """Server mode's cloud connected, through the current module session; its address and why it is not connected as
    attributes."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "cloud", "Cloud", diagnostic=True)

    @property
    def is_on(self) -> bool:
        cloud = self.coordinator.poller.cloud
        return cloud is not None and cloud.status.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = self.coordinator.poller.cloud
        if cloud is None:
            return {"address": None, "error": None}
        return {"address": cloud.status.address, "error": cloud.status.error}
