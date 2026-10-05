"""The Renac dashboard's views (PLAN §6.6), built from what the registry holds of each inverter: one tab per inverter,
its live subview, and an Energy tab of Home Assistant's energy cards. A card names its entity by the entity's own name,
the tab's header carries the inverter's; an entity the registry lacks, disabled or hidden, gets no card, and a section
without a card is left out. dashboard.py reads the registry and writes what is built here."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

TITLE = "Renac"
PHASES = ("l1", "l2", "l3")


def per_phase(key: str) -> list[str]:
    return [f"{key}_{phase}" for phase in PHASES]


def with_phases(key: str) -> list[str]:
    return [key, *per_phase(key)]


# The powers and the state of charge, now, each with its day's small graph.
NOW = ("pv_power", "inverter_power", "battery_power", "soc", "meter1_power", "consumption_power")
# The energy counters shown as tiles, today's and the lifetime ones.
ENERGY = ("pv_energy", "consumption_energy", "charge_energy", "discharge_energy", "grid_export_energy", "grid_import_energy")
# Every energy counter in the register table's order, each lifetime then today.
COUNTERS = (
    "pv_energy",
    "charge_energy",
    "discharge_energy",
    "grid_export_energy",
    "grid_import_energy",
    "output_energy",
    "input_energy",
    "consumption_energy",
    "eps_energy",
)
ALL_COUNTERS = [key for counter in COUNTERS for key in (counter, f"{counter}_today")]
# The day's two graphs, the powers and the SoC, each the recorder's 5-minute means: the states themselves, read every
# few seconds, draw as steps with gaps. Each power in its own colour, HA's named ones, chosen on the dark theme.
POWER_GRAPH = {"pv_power": "light-blue", "battery_power": "green", "consumption_power": "blue-grey", "meter1_power": "deep-orange"}
SOC_GRAPH = {"soc": None}
# The status, badges under the tab's title; live mode's badge follows them, the way into the live subview.
STATUS = ("inverter_state", "faults", "cloud_relay", "tcp_polling", "cloud")
# The w2b relay's battery, when TCP polling's address is one: badges too, hidden while they have no value.
RELAY_BATTERY = ("relay_battery", "relay_battery_voltage")
# The live group's registers by part; the tab's Inverter list is the AC parts together.
GRID = per_phase("grid_voltage") + per_phase("grid_frequency")
INVERTER_AC = per_phase("inverter_current") + with_phases("inverter_power")
METERS = with_phases("meter1_power") + with_phases("meter2_power") + with_phases("meter3_power")
HOUSE = with_phases("consumption_power")
EPS = per_phase("eps_voltage") + per_phase("eps_current") + with_phases("eps_power") + ["eps_frequency"]
INVERTER = GRID + INVERTER_AC + METERS + HOUSE + EPS
# The inverter's temperatures, small graphs under its list.
TEMPERATURES = ("internal_temperature", "charger_temperature", "boost_temperature", "inverter_temperature")
PV = ("pv_power", "pv1_voltage", "pv1_current", "pv1_power", "pv2_voltage", "pv2_current", "pv2_power")
BATTERY = (
    "soc",
    "soh",
    "battery_status",
    "battery_voltage",
    "battery_current",
    "battery_power",
    "battery_cycles",
    "battery_capacity",
    "charge_forbidden",
    "discharge_forbidden",
    "bms_connected",
    "bms_voltage",
    "bms_current",
    "bms_temperature",
    "cell_count",
    "module_count",
    "cells_per_module",
    "voltage_level",
)
# The live subview's lists: everything live mode reads every few seconds, by part; the battery and the energy
# counters follow them.
LIVE_LISTS = (
    ("PV", PV),
    ("Grid", (*GRID, *METERS, "meter_connected")),
    ("Inverter", ("inverter_state", *INVERTER_AC, *TEMPERATURES, "faults")),
    ("House", HOUSE),
    ("Backup (EPS)", EPS),
)
FULL = {"grid_options": {"columns": "full"}}
NO_INVERTER = {
    "title": TITLE,
    "type": "sections",
    "sections": [
        {"type": "grid", "cards": [{"type": "markdown", "content": "No Renac inverter has answered Home Assistant yet. Its tab appears here when one does."}]}
    ],
}


@dataclass(frozen=True)
class Shown:
    """One entity as a card shows it: its id and its own name, without the device's."""

    entity_id: str
    name: str


@dataclass(frozen=True)
class InverterEntities:
    """One inverter as the registry has it: its device's name, its serial when known, its entry, and its entities by
    key, those enabled and not hidden."""

    name: str
    serial: str | None
    entry_id: str
    entities: dict[str, Shown]

    @property
    def path(self) -> str:
        """The tab's URL part: the serial's last three digits, as the entity ids, else the start of the entry's id."""
        tail = self.serial[-3:] if self.serial else self.entry_id[:6]
        return "inverter-" + "".join(char for char in tail.lower() if char.isalnum())


class Cards:
    """The cards of one inverter, each showing an entity under its own name; None for an entity the registry lacks."""

    def __init__(self, inverter: InverterEntities) -> None:
        self.entities = inverter.entities

    def _named(self, key: str) -> dict[str, str]:
        shown = self.entities[key]
        return {"entity": shown.entity_id, "name": shown.name}

    def tile(self, key: str, **extra: Any) -> dict[str, Any] | None:
        return {"type": "tile", **self._named(key), **extra} if key in self.entities else None

    def tiles(self, keys: Iterable[str]) -> list[dict[str, Any] | None]:
        return [self.tile(key) for key in keys]

    def badge(self, key: str, **extra: Any) -> dict[str, Any] | None:
        return {"type": "entity", **self._named(key), "show_name": True, **extra} if key in self.entities else None

    def badge_when_known(self, key: str) -> dict[str, Any] | None:
        """A badge shown only while the entity has a value: not while it is unavailable or unknown."""
        if key not in self.entities:
            return None
        entity_id = self.entities[key].entity_id
        return self.badge(key, visibility=[{"condition": "state", "entity": entity_id, "state_not": state} for state in ("unavailable", "unknown")])

    def graph(self, key: str) -> dict[str, Any] | None:
        """A small graph of the last day under the value, half the section: two to a row."""
        return {"type": "sensor", **self._named(key), "graph": "line"} if key in self.entities else None

    def rows(self, keys: Iterable[str]) -> list[dict[str, str]]:
        return [self._named(key) for key in keys if key in self.entities]

    def entity_list(self, keys: Iterable[str]) -> dict[str, Any] | None:
        rows = self.rows(keys)
        return {"type": "entities", "entities": rows} if rows else None

    def statistics(self, colours: dict[str, str | None]) -> dict[str, Any] | None:
        """The last day's 5-minute means as lines, across the section, each entity in its colour when it has one."""
        rows = [{**self._named(key), **({"color": colour} if colour else {})} for key, colour in colours.items() if key in self.entities]
        if not rows:
            return None
        return {"type": "statistics-graph", "entities": rows, "period": "5minute", "stat_types": ["mean"], "chart_type": "line", "days_to_show": 1, **FULL}


def section(heading: str, cards: Iterable[dict[str, Any] | None], **extra: Any) -> dict[str, Any] | None:
    """A section under a heading, None when it has no card; `extra` is the section's own options, its column span."""
    shown = [card for card in cards if card]
    return {"type": "grid", **extra, "cards": [{"type": "heading", "heading": heading}, *shown]} if shown else None


def subtitle(heading: str) -> dict[str, str]:
    return {"type": "heading", "heading": heading, "heading_style": "subtitle"}


def energy_section(cards: Cards) -> dict[str, Any] | None:
    """The energy counters as tiles: today's, and the lifetime ones below."""
    today = [tile for tile in cards.tiles(f"{key}_today" for key in ENERGY) if tile]
    total = [tile for tile in cards.tiles(ENERGY) if tile]
    return section("Energy", [*([subtitle("Today"), *today] if today else []), *([subtitle("Total"), *total] if total else [])])


def battery_keys(inverter: InverterEntities) -> list[str]:
    """The battery list: the named keys and every other BMS key the inverter has."""
    extra = sorted(key for key in inverter.entities if key.startswith("bms_") and key not in BATTERY)
    return [*BATTERY, *extra]


def inverter_view(inverter: InverterEntities, url_path: str) -> dict[str, Any]:
    """The inverter's tab: the header with its name, the status badges, then the sections."""
    cards = Cards(inverter)
    badges = [
        *(cards.badge(key) for key in STATUS),
        *(cards.badge_when_known(key) for key in RELAY_BATTERY),
        cards.badge("live_mode", tap_action={"action": "navigate", "navigation_path": f"/{url_path}/{inverter.path}-live"}),
    ]
    sections = [
        section("Now", (cards.graph(key) for key in NOW)),
        energy_section(cards),
        section("Inverter", [cards.entity_list(INVERTER), *(cards.graph(key) for key in TEMPERATURES)]),
        section("PV", [cards.entity_list(PV)]),
        # The second row starts here: the two graphs half the width, one above the other.
        section("Today", [cards.statistics(POWER_GRAPH), cards.statistics(SOC_GRAPH)], column_span=2),
        section("Battery", [cards.entity_list(battery_keys(inverter))]),
        section("Energy counters", [cards.entity_list(ALL_COUNTERS)]),
    ]
    return {
        "title": inverter.name,
        "path": inverter.path,
        "type": "sections",
        "max_columns": 4,
        "header": {"card": {"type": "markdown", "text_only": True, "content": f"# {inverter.name}"}, "layout": "center", "badges_position": "bottom"},
        "badges": [badge for badge in badges if badge],
        "sections": [one for one in sections if one],
    }


def live_view(inverter: InverterEntities) -> dict[str, Any]:
    """The live subview: the switch with the time it turns itself off, then everything live mode reads while it is on,
    the live group every few seconds and the energy counters every minute, in lists by part."""
    cards = Cards(inverter)
    # The switch's section across the whole view, the lists below it; the tile one column's width: a spanning
    # section's grid has twelve columns per column it spans.
    switch = cards.tile("live_mode", features=[{"type": "toggle"}], state_content=["state", "turns_off_at"], grid_options={"columns": 12})
    sections = [
        section("Live mode", [switch], column_span=4),
        *(section(heading, [cards.entity_list(keys)]) for heading, keys in LIVE_LISTS),
        section("Battery", [cards.entity_list(battery_keys(inverter))]),
        section("Energy", [cards.entity_list(ALL_COUNTERS)]),
    ]
    return {
        "title": f"{inverter.name} live",
        "path": f"{inverter.path}-live",
        "subview": True,
        "type": "sections",
        "max_columns": 4,
        "sections": [one for one in sections if one],
    }


def energy_view() -> dict[str, Any]:
    """Home Assistant's energy cards for the whole home: they need the Energy dashboard set up, and follow the date
    picker."""
    return {
        "title": "Energy",
        "path": "energy",
        "type": "sections",
        "max_columns": 4,
        "sections": [
            {"type": "grid", "cards": [{"type": "energy-date-selection", **FULL}]},
            section("Solar", [{"type": "energy-solar-consumed-gauge"}, {"type": "energy-self-sufficiency-gauge"}]),
            section("Where the energy went", [{"type": "energy-distribution", "link_dashboard": True, **FULL}]),
            section("Over time", [{"type": "energy-solar-graph", **FULL}, {"type": "energy-usage-graph", **FULL}]),
        ],
    }


def build_dashboard(inverters: Iterable[InverterEntities], url_path: str) -> dict[str, Any]:
    """The dashboard: each inverter's tab and live subview, a note when there is none, and the Energy tab."""
    views: list[dict[str, Any]] = [view for inverter in inverters for view in (inverter_view(inverter, url_path), live_view(inverter))]
    if not views:
        views.append(NO_INVERTER)
    views.append(energy_view())
    return {"title": TITLE, "views": views}
