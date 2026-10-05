"""The poll groups: the register ranges each one reads, the registers it decodes, its interval, and the order of a
cycle. When each one is due is the inverter's (inverter.py)."""

from __future__ import annotations

from dataclasses import dataclass

from .register_table import DAY_S, ENERGY, HOUR_S, IDENTITY, LIVE, POWER_32BIT, PROTECTION, SETTINGS, UNDOCUMENTED
from .registers import Register


@dataclass(frozen=True)
class RegisterRange:
    """One fn 3 request: the first address and the number of registers."""

    address: int
    count: int

    def covers(self, address: int) -> bool:
        return self.address <= address < self.address + self.count


@dataclass(frozen=True)
class PollGroup:
    """Ranges read together on one interval; `registers` are decoded from them."""

    key: str
    name: str
    ranges: tuple[RegisterRange, ...]
    registers: tuple[Register, ...]
    interval_s: float
    live_interval_s: float | None = None
    enabled: bool = True


# What every new connection reads first: the model and the serial.
IDENTITY_RANGE = RegisterRange(10000, 32)
# The BMS serials, 10032-10175, not decoded: read in the cloud's shapes so the inverter answers its reads of them.
BMS_SERIAL_RANGES = (RegisterRange(10032, 16), RegisterRange(10048, 80), RegisterRange(10128, 48))
IDENTITY_GROUP = PollGroup("identity", "Identity", (IDENTITY_RANGE, RegisterRange(10176, 6), *BMS_SERIAL_RANGES), IDENTITY, interval_s=DAY_S)
SETTINGS_GROUP = PollGroup(
    "settings",
    "Settings",
    (RegisterRange(20000, 1), RegisterRange(20001, 6), RegisterRange(20007, 7), RegisterRange(20100, 14), RegisterRange(21000, 1), RegisterRange(21016, 6)),
    SETTINGS,
    interval_s=HOUR_S,
)
LIVE_GROUP = PollGroup("live", "Live", (RegisterRange(11000, 114),), LIVE, interval_s=30.0, live_interval_s=5.0)
# The undocumented 32-bit copies of the live group's power values, under the same keys: read after live, its readings
# stand in for the 16-bit ones while its last read succeeded. Off by default: other models may not have the block.
POWER32_GROUP = PollGroup("power32", "32-bit power", (RegisterRange(11124, 34),), POWER_32BIT, interval_s=30.0, live_interval_s=5.0, enabled=False)
ENERGY_GROUP = PollGroup("energy", "Energy", (RegisterRange(14000, 27),), ENERGY, interval_s=300.0, live_interval_s=60.0)
PROTECTION_GROUP = PollGroup(
    "protection",
    "Protection",
    (
        RegisterRange(22000, 9),
        RegisterRange(22100, 11),
        RegisterRange(22200, 10),
        RegisterRange(23000, 11),
        RegisterRange(23100, 10),
        RegisterRange(23200, 26),
        RegisterRange(23300, 17),
        RegisterRange(23400, 2),
    ),
    PROTECTION,
    interval_s=DAY_S,
    enabled=False,
)
UNDOCUMENTED_GROUP = PollGroup(
    "undocumented",
    "Undocumented",
    (RegisterRange(20114, 1), RegisterRange(20130, 1), RegisterRange(50028, 1), RegisterRange(50029, 1), RegisterRange(50030, 1)),
    UNDOCUMENTED,
    interval_s=HOUR_S,
    enabled=False,
)

# The order a cycle runs them in; a later group's readings replace an earlier one's under the same key.
GROUPS: tuple[PollGroup, ...] = (IDENTITY_GROUP, SETTINGS_GROUP, LIVE_GROUP, POWER32_GROUP, ENERGY_GROUP, PROTECTION_GROUP, UNDOCUMENTED_GROUP)
GROUPS_BY_KEY: dict[str, PollGroup] = {group.key: group for group in GROUPS}
