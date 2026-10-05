"""Reads or sets the module's socket B, its cloud connection, over AT on UDP 48899, through the core's AT session.
Setting is STATE-CHANGING: the module dials the new address at once. The module is found by a broadcast of
HF-A11ASSISTHREAD unless --module is given.

.venv\\Scripts\\python.exe tools\\set_sockb.py
.venv\\Scripts\\python.exe tools\\set_sockb.py 192.168.1.20:20145
.venv\\Scripts\\python.exe tools\\set_sockb.py homeassistant.lan:20145
.venv\\Scripts\\python.exe tools\\set_sockb.py --reset --module 192.168.1.10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "renac_local"))

from renac_protocol.at_session import AtError, AtSession, SocketB, discover  # noqa: E402
from renac_protocol.const import AT_PORT, CLOUD_PORT  # noqa: E402

DEFAULT = SocketB("germany.le-pv.com", CLOUD_PORT)


def parse_target(text: str) -> SocketB:
    try:
        return SocketB.from_address(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reads the module's socket B, its cloud connection, or sets it (STATE-CHANGING).")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("server", nargs="?", type=parse_target, metavar="HOST:PORT", help="the IPv4 address or name socket B dials from now on")
    target.add_argument("--reset", action="store_true", help=f"back to {DEFAULT}")
    parser.add_argument("--module", help=f"the module's IP address; found by a broadcast on UDP {AT_PORT} by default")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    value: SocketB | None = DEFAULT if args.reset else args.server
    module = args.module
    if module is None:
        print("discovering modules in local network...")
        found = await discover()
        if len(found) != 1:
            print("no module answered" if not found else "more than one module answered", "; pick one with --module", sep="", file=sys.stderr)
            for each in found:
                print(f"  {each}", file=sys.stderr)
            return 1
        module = found[0].ip

    try:
        async with AtSession(module) as session:
            if value is None:
                print("current:", await session.read_socket_b())
            else:
                print("after:", await session.set_socket_b(value))
    except AtError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    # The core's log on the screen: the module as it answered, a set with the value before and after.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
