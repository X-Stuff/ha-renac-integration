"""Reads the inverter through the ble-relay (client mode) and prints the decoded values.

Read-only: it sends fn 3 requests and nothing else. Run it from the repo's venv with the relay's host and the port of
its rule for the inverter:

    .venv\\Scripts\\python.exe tools\\relay_client.py w2b.local 8899
    .venv\\Scripts\\python.exe tools\\relay_client.py w2b.local --groups live,energy --period 5 --live
    .venv\\Scripts\\python.exe tools\\relay_client.py w2b.local --all --raw

With --period the terminal is cleared and everything printed again every cycle; Ctrl+C stops it.
Exit code 0 when every group read, 1 when a group failed, 2 when the relay could not be reached.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "renac_local"))

import printing  # noqa: E402

from renac_protocol.channel import ChannelError  # noqa: E402
from renac_protocol.connection import Connection, dial  # noqa: E402
from renac_protocol.const import DEFAULT_CLIENT_PORT, FIRST_REPLY_TIMEOUT_S, QUIET_TIME_S, REPLY_TIMEOUT_S, REQUEST_GAP_S  # noqa: E402
from renac_protocol.group_reader import GroupResult, read_group  # noqa: E402
from renac_protocol.groups import GROUPS, PollGroup  # noqa: E402
from renac_protocol.inverter import Inverter  # noqa: E402
from renac_protocol.poller import quiet_time  # noqa: E402

LIVE_GROUPS = ("live", "energy")

EXIT_OK = 0
EXIT_GROUP_FAILED = 1
EXIT_NO_CONNECTION = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reads the Renac inverter through the ble-relay and prints the decoded values. Read-only.")
    parser.add_argument("host", help="the relay's host name or address")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_CLIENT_PORT, help=f"the relay rule's port (default {DEFAULT_CLIENT_PORT})")
    printing.add_group_arguments(parser, LIVE_GROUPS)
    parser.add_argument(
        "--period",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="repeat every SECONDS, clearing the terminal before each print; 0 reads once (default); Ctrl+C stops",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="keep the connection between cycles, as the integration's live mode does; without it every cycle connects and disconnects",
    )
    parser.add_argument("--raw", action="store_true", help="print every chunk of bytes in and out, and every frame with its decoded form")
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables, one object per cycle")
    parser.add_argument("--timeout", type=float, default=REPLY_TIMEOUT_S, help=f"reply timeout in seconds (default {REPLY_TIMEOUT_S:g})")
    parser.add_argument(
        "--first-timeout",
        type=float,
        default=FIRST_REPLY_TIMEOUT_S,
        help=f"timeout of the first reply after a connect, while the relay brings its BLE link up (default {FIRST_REPLY_TIMEOUT_S:g})",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=QUIET_TIME_S,
        help=f"the quiet time: seconds to hold requests after a frame nobody asked for, the cloud's polling (default {QUIET_TIME_S:g}, 0 none)",
    )
    parser.add_argument("--gap", type=float, default=REQUEST_GAP_S, help=f"seconds between two requests (default {REQUEST_GAP_S:g})")
    args = parser.parse_args(argv)
    if args.period < 0:
        parser.error("--period must be 0 or more")
    return args


class Screen:
    """Where the cycles go: a terminal cleared and printed again every cycle, or lines one after another."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.clear = args.period > 0 and not args.json and sys.stdout.isatty()
        if self.clear:
            printing.enable_ansi_on_windows()

    def show(self, cycle: int, results: list[GroupResult], inverter: Inverter, connection: Connection | None) -> None:
        if self.args.json:
            data = printing.results_json(results, inverter.readings_of, connection)
            data["cycle"] = cycle
            data["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(json.dumps(data, ensure_ascii=False), flush=True)
            return

        header = f"relay {self.args.host}:{self.args.port}, cycle {cycle}, {time.strftime('%Y-%m-%d %H:%M:%S')}"
        if self.args.period > 0:
            header += f", every {self.args.period:g} s, Ctrl+C stops"
        lines = [header, ""]
        for result in results:
            lines += printing.group_lines(result, inverter.readings_of(result.group), self.args.full)
            lines.append("")
        lines += printing.counter_lines(connection)
        lines.append("")

        text = "\n".join(lines)
        sys.stdout.write((printing.CLEAR_SCREEN if self.clear else "") + text + "\n")
        sys.stdout.flush()


async def read_cycle(connection: Connection, inverter: Inverter, groups: list[PollGroup]) -> list[GroupResult]:
    """One cycle: the groups read in order into the inverter; a lost connection ends it."""
    results = []
    for group in groups:
        result = await read_group(connection, group)
        inverter.store_result(result)
        results.append(result)
        if not connection.is_connected:
            break
    return results


async def run(args: argparse.Namespace) -> int:
    groups = printing.chosen_groups(args, LIVE_GROUPS)
    inverter = Inverter(enabled={group.key: group in groups for group in GROUPS})
    if args.raw:
        printing.show_raw_frames()
    screen = Screen(args)

    connection: Connection | None = None
    ever_connected = False
    worst = EXIT_OK
    cycle = 0
    try:
        while True:
            cycle += 1
            if connection is None or not connection.is_connected:
                try:
                    connection = await dial(
                        args.host, args.port, first_reply_timeout_s=args.first_timeout, reply_timeout_s=args.timeout, request_gap_s=args.gap
                    )
                except ChannelError as error:
                    connection = None
                    results = inverter.fail(groups, str(error))
                else:
                    ever_connected = True
                    if args.backoff > 0:
                        connection.on_unsolicited = quiet_time(connection, args.backoff)
                    results = await read_cycle(connection, inverter, groups)
            else:
                results = await read_cycle(connection, inverter, groups)
            screen.show(cycle, results, inverter, connection)
            if any(not result.ok for result in results):
                worst = max(worst, EXIT_GROUP_FAILED if ever_connected else EXIT_NO_CONNECTION)
            if connection is not None and not args.live:
                await connection.close()
                connection = None
            if args.period <= 0:
                break
            await asyncio.sleep(args.period)
    finally:
        if connection is not None:
            await connection.close()
    return worst


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
