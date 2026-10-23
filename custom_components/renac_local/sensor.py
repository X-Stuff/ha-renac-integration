"""The inverter's sensors: one per register of the enabled groups, and those shown as one.

Shown as one: the sixteen fault words as `Faults`; the six clock registers as `Clock`, a diagnostic; a three-phase
power whose sum means something as that sum, the phases as attributes, its per-phase sensors disabled by default
(Inverter and backup power have no register for the sum: it is worked out); the PV power, the sum of the two strings,
which stay sensors too; the battery status as its mode, the request flags as attributes. A quantity whose sum means
nothing stays per phase, only Grid voltage enabled by default. The device shows the model, the serial and the
firmware; the connection states and the battery's flags are binary sensors. The two ways in, the cloud relay (server
mode) and TCP polling (client mode), are diagnostic sensors of their own, each off, standing by or in use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import BINARY_KEYS, DEVICE_KEYS
from .entity import RegisterEntity, RenacEntity, add_when_serial_known, registers_shown
from .inverter_device import InverterDevice, RenacConfigEntry
from .relay_battery import RelayBattery
from .renac_protocol import REGISTERS_BY_KEY, Kind, Register, Source
from .renac_protocol.register_table import BATTERY_MODES, BATTERY_STATUS_BALANCE_CHARGE_BIT, BATTERY_STATUS_FORCED_CHARGE_BIT, BATTERY_STATUS_MODE_MASK
from .renac_protocol.registers import CLOCK_KEYS, FAULT_WORD_ADDRESSES, PHASES, fault_summary

# The three-phase powers shown as the sum of their phases.
PHASE_SUMS = ("inverter_power", "eps_power", "meter1_power", "meter2_power", "meter3_power", "consumption_power")
# The two PV strings' powers, summed as the PV power; the strings stay sensors of their own as well.
PV_POWERS = {"pv1": "pv1_power", "pv2": "pv2_power"}
# The per-phase quantities enabled by default.
PHASES_SHOWN = ("grid_voltage",)
BATTERY_STATUS_KEY = "battery_status"

DEVICE_CLASSES = {
    "V": SensorDeviceClass.VOLTAGE,
    "A": SensorDeviceClass.CURRENT,
    "W": SensorDeviceClass.POWER,
    "var": SensorDeviceClass.REACTIVE_POWER,
    "°C": SensorDeviceClass.TEMPERATURE,
    "Hz": SensorDeviceClass.FREQUENCY,
    "kWh": SensorDeviceClass.ENERGY,
    "ms": SensorDeviceClass.DURATION,
    "s": SensorDeviceClass.DURATION,
}
NUMBERS = (Kind.U16, Kind.I16, Kind.U32, Kind.I32)
# A state is at most 255 characters.
STATE_LENGTH = 255


async def async_setup_entry(hass: HomeAssistant, entry: RenacConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    add_when_serial_known(SENSOR_DOMAIN, entry, async_add_entities, lambda: sensors(entry.runtime_data))


def sensors(device: InverterDevice) -> list[RenacEntity]:
    """The sensors: the registers of the enabled groups, those shown as one, the two ways in."""
    registers = registers_shown(device.inverter)
    keys = {register.key for register in registers}
    shown_as_one = {*DEVICE_KEYS, *BINARY_KEYS, *CLOCK_KEYS, BATTERY_STATUS_KEY, *PHASE_SUMS}
    entities: list[RenacEntity] = [
        RegisterSensor(device, register) for register in registers if register.key not in shown_as_one and register.address not in FAULT_WORD_ADDRESSES
    ]
    entities += [SumSensor.of_phases(device, base) for base in PHASE_SUMS if f"{base}_l1" in keys]
    if keys.issuperset(PV_POWERS.values()):
        entities.append(SumSensor(device, "pv_power", "PV power", PV_POWERS))
    if any(register.address in FAULT_WORD_ADDRESSES for register in registers):
        entities.append(FaultsSensor(device))
    if keys.issuperset(CLOCK_KEYS):
        entities.append(ClockSensor(device))
    if BATTERY_STATUS_KEY in keys:
        entities.append(BatteryStatusSensor(device, REGISTERS_BY_KEY[BATTERY_STATUS_KEY]))
    entities += [CloudRelaySensor(device), TcpPollingSensor(device)]
    if device.relay_battery is not None:
        entities += [RelayBatterySensor(device, device.relay_battery, voltage=False), RelayBatterySensor(device, device.relay_battery, voltage=True)]
    return entities


class RegisterSensor(RegisterEntity, SensorEntity):
    """One register: a number with its unit, an enum, or a text. An enum's state is the table's name as a key,
    lowercase with underscores, the form HA wants for the icons' and translations' state keys; the name itself is the
    translation."""

    def __init__(self, device: InverterDevice, register: Register) -> None:
        base, _, phase = register.key.rpartition("_")
        super().__init__(device, register, enabled=phase not in PHASES or base in PHASES_SHOWN)
        if register.kind in NUMBERS:
            self._attr_native_unit_of_measurement = register.unit or None
            self._attr_device_class = SensorDeviceClass.BATTERY if register.key == "soc" else DEVICE_CLASSES.get(register.unit)
            self._attr_suggested_display_precision = {10: 1, 100: 2, 1000: 3}.get(register.scale, 0)
            if register.unit == "kWh":
                self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            elif not register.diagnostic:
                self._attr_state_class = SensorStateClass.MEASUREMENT
        elif register.enum is not None:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [slugify(name) for name in register.enum.values()]

    @property
    def native_value(self) -> float | int | str | None:
        reading = self.reading
        if reading is None:
            return None
        if self.register.kind in NUMBERS:
            return reading.value
        if self.register.enum is not None:
            # A value the table has no name for is unknown.
            return slugify(reading.text) if reading.text in self.register.enum.values() else None
        return reading.text


class SumSensor(RenacEntity, SensorEntity):
    """A power that is the sum of its parts, the parts as attributes, by name: a three-phase power, its phases `l1`,
    `l2`, `l3`; the PV power, its strings `pv1`, `pv2`. Its own register where the table has one, else the sum of the
    parts."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, device: InverterDevice, key: str, name: str, parts: dict[str, str], *, enabled: bool = True) -> None:
        super().__init__(device, key, name, enabled=enabled)
        self.parts = parts
        self.has_register = key in REGISTERS_BY_KEY

    @classmethod
    def of_phases(cls, device: InverterDevice, base: str) -> SumSensor:
        first = REGISTERS_BY_KEY[f"{base}_{PHASES[0]}"]
        parts = {phase: f"{base}_{phase}" for phase in PHASES}
        return cls(device, base, first.name.removesuffix(" L1"), parts, enabled=not first.disabled)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> int | float | None:
        readings = self.readings
        if self.has_register:
            total = readings.get(self.key)
            return total.value if total is not None else None
        if not all(key in readings for key in self.parts.values()):
            return None
        return sum(readings[key].value for key in self.parts.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self.readings
        return {name: readings[key].value for name, key in self.parts.items() if key in readings}


class FaultsSensor(RenacEntity, SensorEntity):
    """The sixteen fault words as one: the set bits by name, `none` when every word is clear; the words that are not
    zero as attributes."""

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "faults", "Faults", diagnostic=True)
        self.fault_keys = [register.key for register in REGISTERS_BY_KEY.values() if register.address in FAULT_WORD_ADDRESSES]

    @property
    def available(self) -> bool:
        return super().available and all(key in self.readings for key in self.fault_keys)

    @property
    def native_value(self) -> str:
        return fault_summary(self.readings)[0][:STATE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self.readings
        return {key: f"{readings[key].words[0]:04x}" for key in self.fault_keys if key in readings and readings[key].words[0]}


class ClockSensor(RenacEntity, SensorEntity):
    """The inverter's clock, read hourly with the settings, taken as Home Assistant's time zone; unknown when the six
    registers make no date."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "clock", "Clock", diagnostic=True)

    @property
    def available(self) -> bool:
        return super().available and all(key in self.readings for key in CLOCK_KEYS)

    @property
    def native_value(self) -> datetime | None:
        readings = self.readings
        year, month, day, hour, minute, second = (readings[key].words[0] for key in CLOCK_KEYS)
        try:
            return datetime(2000 + year, month, day, hour, minute, second, tzinfo=dt_util.get_default_time_zone())
        except ValueError:
            return None


class BatteryStatusSensor(RegisterEntity, SensorEntity):
    """The battery's mode, its request flags as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [slugify(mode) for mode in BATTERY_MODES.values()]

    @property
    def native_value(self) -> str | None:
        reading = self.reading
        mode = BATTERY_MODES.get(reading.words[0] & BATTERY_STATUS_MODE_MASK) if reading is not None else None
        return slugify(mode) if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self.reading
        word = reading.words[0] if reading is not None else 0
        return {
            "forced_charge_request": bool(word & (1 << BATTERY_STATUS_FORCED_CHARGE_BIT)),
            "balance_charge_request": bool(word & (1 << BATTERY_STATUS_BALANCE_CHARGE_BIT)),
        }


class CloudRelaySensor(RenacEntity, SensorEntity):
    """Server mode: off; waiting for the module to connect; checking, the module's inverter not checked yet; active,
    the poller reads through it; refused, another inverter answers. The port, the module's address, why it was
    refused and how the last session ended, as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "waiting", "checking", "active", "refused"]

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "cloud_relay", "Cloud relay", diagnostic=True)

    @property
    def native_value(self) -> str:
        poller = self.coordinator.poller
        session = poller.session
        if poller.listener is None:
            return "off"
        if session is None:
            return "waiting"
        if session.refused is not None:
            return "refused"
        return "active" if session.checked else "checking"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        poller = self.coordinator.poller
        listener, session = poller.listener, poller.session
        return {
            "port": listener.port if listener is not None else None,
            "module": session.connection.name if session is not None else None,
            "refused": poller.server_refused,
            "last_end": poller.last_session_end,
        }


class TcpPollingSensor(RenacEntity, SensorEntity):
    """Client mode: off; standby while the cloud relay reads, or its module is being checked; active, the poller reads
    through it; failing, its last update could not connect or read the identity, or lost the connection; refused,
    another inverter answers. The address polled and the last error as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "standby", "active", "failing", "refused"]

    def __init__(self, device: InverterDevice) -> None:
        super().__init__(device, "tcp_polling", "TCP polling", diagnostic=True)

    @property
    def native_value(self) -> str:
        poller = self.coordinator.poller
        if poller.client_settings is None:
            return "off"
        if poller.current_source is not Source.CLIENT:
            return "standby"
        if poller.client_refused is not None:
            return "refused"
        return "failing" if poller.client_error is not None else "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        poller = self.coordinator.poller
        settings = poller.client_settings
        return {"address": settings.name if settings is not None else None, "error": poller.client_error}


class RelayBatterySensor(RenacEntity, SensorEntity):
    """The w2b relay's battery (relay_battery.py): the percentage the relay's page makes of its voltage, or the
    voltage itself. Unavailable while the relay's page does not answer, unknown while the relay reports no battery."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: InverterDevice, relay: RelayBattery, *, voltage: bool) -> None:
        key, name = ("relay_battery_voltage", "Relay battery voltage") if voltage else ("relay_battery", "Relay battery")
        super().__init__(device, key, name, diagnostic=True, coordinator=relay)
        self.relay = relay
        self.voltage = voltage
        if voltage:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_native_unit_of_measurement = "V"
            self._attr_suggested_display_precision = 2
        else:
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> float | int | None:
        reading = self.relay.data
        if reading is None:
            return None
        return reading.volts if self.voltage else reading.percent
