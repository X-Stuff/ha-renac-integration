"""What the command-line tools share: the group options, tables of readings, the connection's counters, the raw frames
on the screen, the JSON shape and the terminal's screen."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from renac_protocol.connection import Connection
from renac_protocol.group_reader import GroupResult
from renac_protocol.groups import GROUPS, GROUPS_BY_KEY, PollGroup
from renac_protocol.registers import CLOCK_KEYS, FAULT_WORD_ADDRESSES, PHASES, Reading, clock_text, fault_summary

# ANSI: clear the screen and its scrollback, cursor to the top left.
CLEAR_SCREEN = "\x1b[2J\x1b[3J\x1b[H"

# The loggers whose debug lines are the raw view: every chunk of bytes, and every frame with its decoded form.
RAW_LOGGERS = ("renac_protocol.raw", "renac_protocol.connection")


def add_group_arguments(parser: argparse.ArgumentParser, default_keys: Iterable[str]) -> None:
    """The options that pick the groups and their registers: --groups, --all, --full, --use-32bit."""
    parser.add_argument("--groups", help=f"comma-separated group keys of {', '.join(GROUPS_BY_KEY)} (default {','.join(default_keys)})")
    parser.add_argument("--all", action="store_true", help="every group, power32, protection and undocumented included")
    parser.add_argument("--full", action="store_true", help="also the registers that are off by default: BMS detail, Meter 2 and 3")
    parser.add_argument(
        "--use-32bit",
        action="store_true",
        help="add the power32 group: the power values from the undocumented 32-bit block 11124, standing in for the 16-bit ones while it reads",
    )


def chosen_groups(args: argparse.Namespace, default_keys: Iterable[str]) -> list[PollGroup]:
    """The groups --groups or --all picks, `default_keys` without either, plus power32 with --use-32bit, in cycle order."""
    if args.all:
        keys = set(GROUPS_BY_KEY)
    elif args.groups:
        keys = {key.strip() for key in args.groups.split(",") if key.strip()}
        unknown = keys - set(GROUPS_BY_KEY)
        if unknown:
            raise SystemExit(f"unknown group(s): {', '.join(sorted(unknown))}; known: {', '.join(GROUPS_BY_KEY)}")
    else:
        keys = set(default_keys)
    if args.use_32bit:
        keys.add("power32")
    return [group for group in GROUPS if group.key in keys]


def enable_ansi_on_windows() -> None:
    """Turns on the console's escape-sequence processing on Windows; elsewhere it is on already."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def show_raw_frames(write: Callable[[str], None] = print) -> None:
    """Every chunk of bytes in and out and every frame parsed, as the core logs them at debug level, to `write`."""

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            write(record.getMessage())

    for name in RAW_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(Handler())


def reading_rows(readings: Mapping[str, Reading], full: bool = False) -> list[tuple[str, str, str, str]]:
    """Table rows (name, text, address, words hex) of the readings: the fault words as one `Faults` row in the place
    of the first of them; the six clock registers as one `Clock` row in the place of the year; the three phases of a
    value as one row, `L1/L2/L3 unit`, in the place of L1; registers that are off by default only with `full`."""
    rows: list[tuple[str, str, str, str]] = []
    faults_shown = False
    clock = clock_text(readings)
    for reading in readings.values():
        register = reading.register
        if register.address in FAULT_WORD_ADDRESSES:
            if not faults_shown:
                text, words = fault_summary(readings)
                rows.append(("Faults", text, str(FAULT_WORD_ADDRESSES.start), words))
                faults_shown = True
            continue
        if clock is not None and register.key in CLOCK_KEYS:
            if register.key == CLOCK_KEYS[0]:
                rows.append(("Clock", clock, str(register.address), " ".join(readings[key].words_hex for key in CLOCK_KEYS)))
            continue
        if register.disabled and not full:
            continue
        phases = phase_readings(readings, register.key)
        if phases is None:
            rows.append((register.name, reading.text, str(register.address), reading.words_hex))
        elif reading is phases[0]:
            rows.append(phase_row(phases))
    return rows


def phase_readings(readings: Mapping[str, Reading], key: str) -> list[Reading] | None:
    """The L1, L2 and L3 readings of the value `key` is a phase of, when all three are there; None otherwise."""
    base, _, phase = key.rpartition("_")
    if phase not in PHASES:
        return None
    phases = [readings[f"{base}_{suffix}"] for suffix in PHASES if f"{base}_{suffix}" in readings]
    return phases if len(phases) == len(PHASES) else None


def phase_row(phases: list[Reading]) -> tuple[str, str, str, str]:
    """One table row for the three phases of a value: the name as `<value> L1/L2/L3` (a sum of the phases has a row
    of its own under the plain name), the numbers as `L1/L2/L3` with the unit, the address of L1, the words of each
    phase."""
    first = phases[0].register
    unit = f" {first.unit}" if first.unit else ""
    numbers = "/".join(reading.text.removesuffix(unit) if unit else reading.text for reading in phases)
    words = " / ".join(reading.words_hex for reading in phases)
    return f"{first.name.removesuffix(' L1')} L1/L2/L3", numbers + unit, str(first.address), words


def group_lines(result: GroupResult, readings: Mapping[str, Reading], full: bool = False, show_time: bool = False) -> list[str]:
    """The group's readings as a table: name, value with unit, register address and raw words.

    The sixteen fault words are one `Faults` row. Registers that are off by default (BMS detail, Meter 2 and 3)
    show only with `full`. With `show_time` the title says at what time of day the group was read.
    """
    title = f"{result.group.name} ({result.group.key})"
    title += f"  FAILED: {result.error}" if result.error else f"  {result.duration_s * 1000:.0f} ms"
    if show_time:
        # finished_at is monotonic, for the schedule; the time of day is now minus how long ago that was.
        read_at = time.time() - (time.monotonic() - result.finished_at)
        title += f", read at {time.strftime('%H:%M:%S', time.localtime(read_at))}"
    lines = [title, "-" * len(title)]
    rows = reading_rows(readings, full)
    if not rows:
        lines.append("  (no values)")
        return lines
    return lines + table_lines(rows)


def table_lines(rows: list[tuple[str, str, str, str]]) -> list[str]:
    """Rows of (name, text, address, words) as aligned lines."""
    name_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    return [f"  {name:<{name_width}}  {text:>{value_width}}   reg {address:>5}  {words}" for name, text, address, words in rows]


def counter_lines(connection: Connection | None) -> list[str]:
    """The connection's counters: requests and replies, round trips, unsolicited frames, framer errors, bytes."""
    if connection is None:
        return ["Connection", "----------", "  none"]
    requests = connection.counters
    frames = connection.channel.framer.counters
    bytes_ = connection.channel.counters
    return [
        "Connection",
        "----------",
        f"  requests {requests.requests}, replies {requests.replies}, exceptions {requests.exceptions}, "
        f"timeouts {requests.timeouts}, retries {requests.retries}",
        f"  round trip last {requests.last_round_trip_s * 1000:.0f} ms, mean {requests.mean_round_trip_s * 1000:.0f} ms, "
        f"max {requests.max_round_trip_s * 1000:.0f} ms",
        f"  unsolicited frames {requests.unsolicited}, next request held for {connection.held_for_s:.0f} s",
        f"  crc errors {frames.crc_errors}, padding bytes {frames.padding_bytes}, garbage bytes {frames.garbage_bytes}, "
        f"partial frames dropped {frames.partial_discards}",
        f"  bytes in {bytes_.bytes_in}, out {bytes_.bytes_out}",
    ]


def results_json(results: Iterable[GroupResult], readings_of: Callable[[PollGroup], Mapping[str, Reading]], connection: Connection | None) -> dict[str, Any]:
    """The results, their readings and the counters as plain dicts for JSON output."""
    groups: dict[str, Any] = {}
    for result in results:
        groups[result.group.key] = {
            "ok": result.ok,
            "error": result.error,
            "duration_ms": round(result.duration_s * 1000),
            "values": {
                reading.key: {
                    "name": reading.register.name,
                    "value": reading.value,
                    "text": reading.text,
                    "unit": reading.register.unit,
                    "address": reading.register.address,
                    "words": list(reading.words),
                }
                for reading in readings_of(result.group).values()
            },
        }
    data: dict[str, Any] = {"groups": groups, "connection": None}
    if connection is not None:
        requests = connection.counters
        frames = connection.channel.framer.counters
        data["connection"] = {
            "requests": requests.requests,
            "replies": requests.replies,
            "exceptions": requests.exceptions,
            "timeouts": requests.timeouts,
            "retries": requests.retries,
            "round_trip_last_ms": round(requests.last_round_trip_s * 1000),
            "round_trip_mean_ms": round(requests.mean_round_trip_s * 1000),
            "round_trip_max_ms": round(requests.max_round_trip_s * 1000),
            "unsolicited": requests.unsolicited,
            "crc_errors": frames.crc_errors,
            "padding_bytes": frames.padding_bytes,
            "garbage_bytes": frames.garbage_bytes,
            "partial_discards": frames.partial_discards,
            "bytes_in": connection.channel.counters.bytes_in,
            "bytes_out": connection.channel.counters.bytes_out,
        }
    return data
