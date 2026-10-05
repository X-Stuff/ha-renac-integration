"""What every entity of the inverter device shares: the device it belongs to, its key, its entity id, and the readings
it shows; and when the platforms add them: once the inverter's serial is known."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import slugify

from .const import DOMAIN
from .inverter_device import InverterDevice, RenacConfigEntry
from .renac_protocol import Inverter, Reading, Register


class RenacEntity(CoordinatorEntity[InverterDevice]):
    """One entity of the entry's device, its unique id the entry's id and `key`; its name is the register table's
    until the names are translated. `key` is its translation key as well, which icons.json and the dashboard know
    it by. It follows the device's updates, or those of `coordinator` when the entity has one of its own, the relay's
    battery."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: InverterDevice,
        key: str,
        name: str,
        *,
        diagnostic: bool = False,
        enabled: bool = True,
        coordinator: DataUpdateCoordinator[Any] | None = None,
    ) -> None:
        super().__init__(coordinator if coordinator is not None else device)
        self.device = device
        entry_id = device.config_entry.entry_id
        self.key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if diagnostic else None
        self._attr_entity_registry_enabled_default = enabled

    @property
    def readings(self) -> dict[str, Reading]:
        return self.device.data


class RegisterEntity(RenacEntity):
    """The entity of one register: available while its group's last read gave it."""

    def __init__(self, device: InverterDevice, register: Register, *, enabled: bool = True) -> None:
        super().__init__(device, register.key, register.name, diagnostic=register.diagnostic, enabled=enabled and not register.disabled)
        self.register = register

    @property
    def available(self) -> bool:
        return super().available and self.key in self.readings

    @property
    def reading(self) -> Reading | None:
        return self.readings.get(self.key)


def add_when_serial_known(domain: str, entry: RenacConfigEntry, add_entities: AddConfigEntryEntitiesCallback, build: Callable[[], list[RenacEntity]]) -> None:
    """Adds the platform's entities once the inverter's serial is known: at once when the entry holds it, else at the
    first update after it is read. Until then the entry has no entities; the Repairs issue says why (issues.py). The
    entry's registered entities of the platform that are not built any more, a group turned off in the settings, are
    taken out of the registry."""
    device = entry.runtime_data
    added = False

    @callback
    def add_once() -> None:
        nonlocal added
        if added or device.poller.serial is None:
            return
        added = True
        entities = build()
        ask_entity_ids(domain, entities)
        remove_entities_gone(device.hass, domain, entry.entry_id, entities)
        add_entities(entities)

    add_once()
    if not added:
        entry.async_on_unload(device.async_add_listener(add_once))


def ask_entity_ids(domain: str, entities: Iterable[RenacEntity]) -> None:
    """Each entity's id `<domain>.renac_inverter_<the serial's last three digits>_<key>`, `001` should no serial be
    known. HA takes it for an entity it has not registered yet; a registered one keeps the id it has."""
    for entity in entities:
        serial = entity.device.poller.serial
        entity.entity_id = f"{domain}.renac_inverter_{slugify(serial[-3:]) if serial else '001'}_{entity.key}"


def remove_entities_gone(hass: HomeAssistant, domain: str, entry_id: str, entities: Iterable[RenacEntity]) -> None:
    """Takes out of the registry the entry's entities of the platform that are not among those built."""
    registry = er.async_get(hass)
    kept = {entity.unique_id for entity in entities}
    for registered in er.async_entries_for_config_entry(registry, entry_id):
        if registered.domain == domain and registered.unique_id not in kept:
            registry.async_remove(registered.entity_id)


def registers_shown(inverter: Inverter) -> list[Register]:
    """The registers of the enabled groups, each key once, in group order: the power32 group's keys are the live
    group's, one entity each."""
    registers: dict[str, Register] = {}
    for group in inverter.groups:
        if inverter.enabled.get(group.key, False):
            for register in group.registers:
                registers.setdefault(register.key, register)
    return list(registers.values())
