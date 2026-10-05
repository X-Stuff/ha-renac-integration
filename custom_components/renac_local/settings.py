"""The entry's settings (PLAN §6.8): the Configure menu's Settings form, kept in the entry's options, and what the
core's blocks get from them. A setting the options lack is the core's default, so an entry made before the form runs
as before; a saved change reloads the entry.

The form: each group's interval; TCP polling's quiet time; whether the cloud relay passes the cloud's writes outside
the clock to the inverter (D18); live mode's intervals and its auto-off; the optional groups with their intervals;
and, collapsed, Advanced: the reply, first-reply and connect timeouts, how many intervals a word stays valid for, the
cloud relay's handshake and its forwarding of frames with a bad CRC. A setting of a way in that is off is not shown
and keeps its value. The options hold every time in seconds; the form shows each in its own unit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import CONF_CLIENT, CONF_CLOUD_HOST, CONF_ENABLED, CONF_SERVER
from .renac_protocol.const import CONNECT_TIMEOUT_S, FIRST_REPLY_TIMEOUT_S, QUIET_TIME_S, REPLY_TIMEOUT_S, STATE_AGE_INTERVALS
from .renac_protocol.groups import ENERGY_GROUP, LIVE_GROUP, POWER32_GROUP, PROTECTION_GROUP, SETTINGS_GROUP, UNDOCUMENTED_GROUP

# Live mode turns itself off after this (PLAN §6.6): a look at the live values, as the Renac app's local screen.
LIVE_AUTO_OFF_S = 60.0
# The form's sections; Advanced is collapsed.
LIVE_MODE = "live_mode"
MORE_GROUPS = "more_groups"
ADVANCED = "advanced"
# The ways in a setting is for: shown only while that way is on. The cloud: server mode with a cloud to relay to.
SERVER = "server"
CLIENT = "client"
CLOUD = "cloud"
# Seconds in each unit a time is shown in; a count of intervals has no unit.
UNIT_S = {"s": 1.0, "min": 60.0, "h": 3600.0, "intervals": 1.0}


@dataclass(frozen=True)
class Setting:
    """One setting: its key in the options, the core's default (a bool: a switch), the unit the form shows it in, the
    least the form takes in that unit, its section in the form (None: the top), and the way in it is for (None: any)."""

    key: str
    default: float | bool
    unit: str = ""
    minimum: float = 1.0
    section: str | None = None
    way: str | None = None

    @property
    def is_switch(self) -> bool:
        return isinstance(self.default, bool)

    def value(self, options: Mapping[str, Any]) -> Any:
        """The option, or the default."""
        return options.get(self.key, self.default)

    def shown(self, options: Mapping[str, Any]) -> float | bool:
        """The form's value: a switch as it is, a time in the form's unit."""
        value = self.value(options)
        return bool(value) if self.is_switch else float(value) / UNIT_S[self.unit]

    def stored(self, shown: Any) -> float | bool:
        """The option from the form's value: a time in seconds."""
        return bool(shown) if self.is_switch else float(shown) * UNIT_S[self.unit]


# In the form's order. The power32 group extends the live group and follows its intervals; the identity group is read
# at every connection and has no setting.
SETTINGS: tuple[Setting, ...] = (
    Setting("live_interval_s", LIVE_GROUP.interval_s, "s"),
    Setting("energy_interval_s", ENERGY_GROUP.interval_s, "min"),
    Setting("settings_interval_s", SETTINGS_GROUP.interval_s, "min"),
    Setting("quiet_time_s", QUIET_TIME_S, "s", minimum=0.0, way=CLIENT),
    Setting("forward_writes", False, way=CLOUD),
    Setting("live_mode_live_s", LIVE_GROUP.live_interval_s or LIVE_GROUP.interval_s, "s", section=LIVE_MODE),
    Setting("live_mode_energy_s", ENERGY_GROUP.live_interval_s or ENERGY_GROUP.interval_s, "s", section=LIVE_MODE),
    Setting("live_auto_off_s", LIVE_AUTO_OFF_S, "min", section=LIVE_MODE),
    Setting(POWER32_GROUP.key, False, section=MORE_GROUPS),
    Setting(PROTECTION_GROUP.key, False, section=MORE_GROUPS),
    Setting("protection_interval_s", PROTECTION_GROUP.interval_s, "h", section=MORE_GROUPS),
    Setting(UNDOCUMENTED_GROUP.key, False, section=MORE_GROUPS),
    Setting("undocumented_interval_s", UNDOCUMENTED_GROUP.interval_s, "min", section=MORE_GROUPS),
    Setting("reply_timeout_s", REPLY_TIMEOUT_S, "s", minimum=0.5, section=ADVANCED),
    Setting("first_reply_timeout_s", FIRST_REPLY_TIMEOUT_S, "s", minimum=0.5, section=ADVANCED, way=CLIENT),
    Setting("connect_timeout_s", CONNECT_TIMEOUT_S, "s", minimum=0.5, section=ADVANCED, way=CLIENT),
    Setting("age_intervals", STATE_AGE_INTERVALS, "intervals", section=ADVANCED),
    Setting("handshake", True, section=ADVANCED, way=SERVER),
    Setting("forward_bad_crc", False, section=ADVANCED, way=CLOUD),
)
SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}


@dataclass(frozen=True)
class Settings:
    """The settings as the core's blocks take them: the inverter's intervals, live intervals, enabled groups and age
    limit; client mode's quiet time and timeouts; the reply timeout of both ways; the cloud relay's policies."""

    intervals: dict[str, float]
    live_intervals: dict[str, float]
    enabled: dict[str, bool]
    age_intervals: float
    live_auto_off_s: float
    quiet_time_s: float
    forward_writes: bool
    reply_timeout_s: float
    first_reply_timeout_s: float
    connect_timeout_s: float
    handshake: bool
    forward_bad_crc: bool

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        def value(key: str) -> Any:
            return SETTINGS_BY_KEY[key].value(options)

        live, live_mode_live = value("live_interval_s"), value("live_mode_live_s")
        return cls(
            intervals={
                LIVE_GROUP.key: live,
                POWER32_GROUP.key: live,
                ENERGY_GROUP.key: value("energy_interval_s"),
                SETTINGS_GROUP.key: value("settings_interval_s"),
                PROTECTION_GROUP.key: value("protection_interval_s"),
                UNDOCUMENTED_GROUP.key: value("undocumented_interval_s"),
            },
            live_intervals={LIVE_GROUP.key: live_mode_live, POWER32_GROUP.key: live_mode_live, ENERGY_GROUP.key: value("live_mode_energy_s")},
            enabled={group.key: value(group.key) for group in (POWER32_GROUP, PROTECTION_GROUP, UNDOCUMENTED_GROUP)},
            age_intervals=value("age_intervals"),
            live_auto_off_s=value("live_auto_off_s"),
            quiet_time_s=value("quiet_time_s"),
            forward_writes=value("forward_writes"),
            reply_timeout_s=value("reply_timeout_s"),
            first_reply_timeout_s=value("first_reply_timeout_s"),
            connect_timeout_s=value("connect_timeout_s"),
            handshake=value("handshake"),
            forward_bad_crc=value("forward_bad_crc"),
        )


def ways_on(data: Mapping[str, Any]) -> set[str]:
    """The ways in the entry's data has on, and the cloud when server mode has one to relay to."""
    ways = set()
    server = data.get(CONF_SERVER) or {}
    if server.get(CONF_ENABLED):
        ways.add(SERVER)
        if server.get(CONF_CLOUD_HOST):
            ways.add(CLOUD)
    if (data.get(CONF_CLIENT) or {}).get(CONF_ENABLED):
        ways.add(CLIENT)
    return ways


def settings_shown(ways: set[str]) -> list[Setting]:
    """The settings the form shows: those of any way, and those of the ways on."""
    return [setting for setting in SETTINGS if setting.way is None or setting.way in ways]


def options_from_form(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The options the form's values make: the sections' fields taken up, times in seconds."""
    options: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, Mapping):
            options.update(options_from_form(value))
        else:
            options[key] = SETTINGS_BY_KEY[key].stored(value)
    return options
