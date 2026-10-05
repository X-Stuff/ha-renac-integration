"""Server mode on the console: waits for the inverter's module, polls the inverter, relays the cloud and shows what it
read.

The poller of the core runs it (PLAN §6.5), clearing what was read when a module session ends: a session's settle
time, the identity read, the handshake while no cloud is connected, then the groups on their timers. The console is
drawn again after every change and everything the core logs, the frames included, goes to a log file. The tool's own
requests are fn 3 reads and the module's handshake. The cloud's reads are answered from what the polls read when that is
fresh enough, else passed to the module; its clock writes pass; its other writes are blocked unless
--forward-unknown-writes (state-changing: they then reach the inverter); its frames with a bad CRC are dropped unless
--forward-bad-crc. With --proxy the connection to the cloud goes through a SOCKS5 proxy (no authentication), the
cloud's name resolved by the proxy; the module's connection stays direct. Run it from the repo's venv:

    .venv\\Scripts\\python.exe tools\\cloud_relay.py
    .venv\\Scripts\\python.exe tools\\cloud_relay.py --cloud germany.le-pv.com
    .venv\\Scripts\\python.exe tools\\cloud_relay.py --cloud germany.le-pv.com --proxy 127.0.0.1:1080
    .venv\\Scripts\\python.exe tools\\cloud_relay.py --proxy 127.0.0.1:1080 --check-proxy
    .venv\\Scripts\\python.exe tools\\cloud_relay.py --bind 127.0.0.1 --groups live,energy --live

The real module dials it once the router sends the module's cloud connection to this computer; the fake module dials
it from the same computer:

    .venv\\Scripts\\python.exe -m tests.mocks.mock_hf_lpt270 --tcp 127.0.0.1:20145

Ctrl+C stops it. Exit code 0, or 2 when the port cannot be opened; --check-proxy: 0, or 1 when the proxy failed.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import ipaddress
import logging
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "renac_local"))

import printing  # noqa: E402

from renac_protocol.channel import ChannelError, Opener  # noqa: E402
from renac_protocol.cloud_session import CloudAddress, CloudSettings  # noqa: E402
from renac_protocol.const import CLOUD_PORT, DEFAULT_BIND, DEFAULT_DNS_SERVER, DEFAULT_SERVER_PORT  # noqa: E402
from renac_protocol.dns import is_ip_literal  # noqa: E402
from renac_protocol.groups import GROUPS  # noqa: E402
from renac_protocol.inverter import Inverter  # noqa: E402
from renac_protocol.poller import Poller, ServerSettings  # noqa: E402

LOGGER = logging.getLogger("cloud_relay")

DEFAULT_LOG = "cloud_relay.log"
# The groups the integration polls by default: identity, settings, live, energy.
DEFAULT_GROUPS = tuple(group.key for group in GROUPS if group.enabled)
SYSTEM_RESOLVER = "system"

# The service --check-proxy asks over plain HTTP; its answer is the address it sees the request come from.
IP_ECHO_HOST = "api.ipify.org"
IP_ECHO_PORT = 80
CHECK_TIMEOUT_S = 15.0

# SOCKS5, RFC 1928: the version, the "no authentication" method, the CONNECT command, the address types.
SOCKS_VERSION = 5
SOCKS_NO_AUTHENTICATION = 0
SOCKS_CONNECT = 1
SOCKS_IPV4 = 1
SOCKS_DOMAIN = 3
SOCKS_IPV6 = 4
SOCKS_REPLIES = {
    1: "general failure",
    2: "not allowed by the proxy's rules",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}

EXIT_OK = 0
EXIT_PROXY_FAILED = 1
EXIT_NO_PORT = 2


def split_host_port(text: str, default_port: int) -> tuple[str, int] | None:
    """HOST[:PORT] as a pair, the default port without one; None when the port is not a port."""
    host, separator, port = text.rpartition(":")
    if not separator:
        return text, default_port
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host, int(port)


def parse_proxy(text: str) -> tuple[str, int] | None:
    """IP:PORT of the SOCKS5 proxy as (ip, port); None when the text is not that."""
    pair = split_host_port(text, 0)
    if pair is None or pair[1] == 0 or not is_ip_literal(pair[0]):
        return None
    return pair


async def open_through_socks5(proxy_host: str, proxy_port: int, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A TCP connection to host:port through a SOCKS5 proxy without authentication; a name goes to the proxy
    unresolved. Raises ConnectionError when the proxy refuses."""
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    try:
        writer.write(bytes([SOCKS_VERSION, 1, SOCKS_NO_AUTHENTICATION]))
        await writer.drain()
        version, method = await reader.readexactly(2)
        if version != SOCKS_VERSION or method != SOCKS_NO_AUTHENTICATION:
            raise ConnectionError(f"proxy {proxy_host}:{proxy_port} is not SOCKS5 without authentication")
        if is_ip_literal(host) and ipaddress.ip_address(host).version == 4:
            address = bytes([SOCKS_IPV4]) + socket.inet_aton(host)
        else:
            name = host.encode("idna")
            if len(name) > 255:
                raise ConnectionError(f"{host}: name longer than 255 bytes")
            address = bytes([SOCKS_DOMAIN, len(name)]) + name
        writer.write(bytes([SOCKS_VERSION, SOCKS_CONNECT, 0]) + address + port.to_bytes(2, "big"))
        await writer.drain()
        _, reply, _, address_type = await reader.readexactly(4)
        if reply != 0:
            raise ConnectionError(f"proxy {proxy_host}:{proxy_port}: {SOCKS_REPLIES.get(reply, f'reply {reply}')}")
        # The address and port the proxy connected from; not needed.
        if address_type == SOCKS_IPV4:
            await reader.readexactly(4 + 2)
        elif address_type == SOCKS_DOMAIN:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length + 2)
        elif address_type == SOCKS_IPV6:
            await reader.readexactly(16 + 2)
        else:
            raise ConnectionError(f"proxy {proxy_host}:{proxy_port}: address type {address_type}")
    except asyncio.IncompleteReadError as error:
        writer.close()
        raise ConnectionError(f"proxy {proxy_host}:{proxy_port} closed the connection") from error
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def ask_ip(opener: Opener) -> str:
    """The address the IP echo service sees, asked over plain HTTP on a connection the opener makes."""
    reader, writer = await asyncio.wait_for(opener(IP_ECHO_HOST, IP_ECHO_PORT), CHECK_TIMEOUT_S)
    try:
        writer.write(f"GET / HTTP/1.0\r\nHost: {IP_ECHO_HOST}\r\n\r\n".encode("ascii"))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(-1), CHECK_TIMEOUT_S)
    finally:
        writer.close()
    head, _, body = response.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = status.split()
    if len(parts) < 2 or parts[1] != "200":
        raise ConnectionError(f"{IP_ECHO_HOST} answered {status!r}")
    return body.decode("ascii", "replace").strip()


async def ask_ip_text(opener: Opener) -> tuple[bool, str]:
    """(True, the address) when the IP echo service answered, (False, why not) otherwise."""
    try:
        return True, await ask_ip(opener)
    except (OSError, TimeoutError) as error:
        return False, f"failed: {error or 'timeout'}"


async def check_proxy(proxy: tuple[str, int]) -> int:
    """Prints the address the IP echo service sees directly and through the proxy."""
    print(f"asking {IP_ECHO_HOST} which address it sees")
    _, direct = await ask_ip_text(asyncio.open_connection)
    proxy_ok, through_proxy = await ask_ip_text(functools.partial(open_through_socks5, *proxy))
    lines = [f"  direct:                {direct}", f"  through {proxy[0]}:{proxy[1]}:  {through_proxy}"]
    for line in lines:
        print(line)
        LOGGER.info("check-proxy %s", line.strip())
    return EXIT_OK if proxy_ok else EXIT_PROXY_FAILED


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Server mode: waits for the inverter's module, polls the inverter, relays the cloud and shows the decoded values. "
        "Its own requests are reads; the cloud's clock writes pass, its other writes only with --forward-unknown-writes. Ctrl+C stops it."
    )
    parser.add_argument("--bind", default=DEFAULT_BIND, help=f"the address to wait on (default {DEFAULT_BIND}, every address)")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help=f"the port the module dials (default {DEFAULT_SERVER_PORT})")
    cloud = parser.add_mutually_exclusive_group()
    cloud.add_argument("--cloud", metavar="NAME[:PORT]", help=f"the cloud's host name, resolved through --dns; port {CLOUD_PORT} by default")
    cloud.add_argument("--cloud-ip", metavar="ADDRESS[:PORT]", help=f"the cloud's IP address; port {CLOUD_PORT} by default")
    parser.add_argument(
        "--dns",
        default=DEFAULT_DNS_SERVER,
        metavar="SERVER",
        help=f"the DNS server's IP address that resolves --cloud (default {DEFAULT_DNS_SERVER}); '{SYSTEM_RESOLVER}' for the system's resolver",
    )
    parser.add_argument(
        "--proxy",
        metavar="IP:PORT",
        help="connect to the cloud through this SOCKS5 proxy, no authentication; the proxy resolves the cloud's name, --dns is not used",
    )
    parser.add_argument(
        "--check-proxy",
        action="store_true",
        help=f"ask {IP_ECHO_HOST} which address it sees, directly and through --proxy, then exit",
    )
    parser.add_argument("--no-handshake", action="store_true", help="do not send the module's handshake while no cloud is connected")
    parser.add_argument(
        "--forward-unknown-writes",
        action="store_true",
        help="STATE-CHANGING: pass the cloud's writes outside the clock (20001-20006) to the inverter; blocked by default",
    )
    parser.add_argument("--forward-bad-crc", action="store_true", help="pass the cloud's frames with a bad CRC to the module as received; dropped by default")
    printing.add_group_arguments(parser, DEFAULT_GROUPS)
    parser.add_argument("--live", action="store_true", help="live mode, as the integration's: the live group every 5 s, energy every 60 s")
    parser.add_argument(
        "--log", default=DEFAULT_LOG, metavar="FILE", help=f"the log file, appended; everything the core logs, the frames included (default {DEFAULT_LOG})"
    )
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error(f"--port {args.port} is not a port")
    args.cloud_address = cloud_address(parser, args)
    args.proxy_address = None
    if args.proxy is not None:
        args.proxy_address = parse_proxy(args.proxy)
        if args.proxy_address is None:
            parser.error(f"--proxy {args.proxy!r} is not IP:PORT")
        if args.cloud_address is None and not args.check_proxy:
            parser.error("--proxy needs --cloud or --cloud-ip")
    if args.check_proxy and args.proxy_address is None:
        parser.error("--check-proxy needs --proxy")
    return args


def cloud_address(parser: argparse.ArgumentParser, args: argparse.Namespace) -> CloudAddress | None:
    """The cloud --cloud or --cloud-ip gives, None without either."""
    if args.dns == SYSTEM_RESOLVER:
        dns_server = None
    elif is_ip_literal(args.dns):
        dns_server = args.dns
    else:
        parser.error(f"--dns {args.dns!r} is neither an IP address nor '{SYSTEM_RESOLVER}'")
    if args.cloud is not None:
        pair = split_host_port(args.cloud, CLOUD_PORT)
        if pair is None:
            parser.error(f"--cloud {args.cloud!r} is not NAME[:PORT]")
        return CloudAddress(pair[0], pair[1], dns_server)
    if args.cloud_ip is not None:
        pair = split_host_port(args.cloud_ip, CLOUD_PORT)
        if pair is None or not is_ip_literal(pair[0]):
            parser.error(f"--cloud-ip {args.cloud_ip!r} is not ADDRESS[:PORT]")
        return CloudAddress(pair[0], pair[1], None)
    return None


class Screen:
    """The console: cleared and drawn again after every change on a terminal; one block after another otherwise."""

    def __init__(self, args: argparse.Namespace, poller: Poller) -> None:
        self.args = args
        self.poller = poller
        self.clear = sys.stdout.isatty()
        if self.clear:
            printing.enable_ansi_on_windows()

    def show(self) -> None:
        text = "\n".join(self.lines())
        sys.stdout.write((printing.CLEAR_SCREEN if self.clear else "") + text + "\n")
        sys.stdout.flush()

    def lines(self) -> list[str]:
        poller = self.poller
        listener = poller.listener
        assert listener is not None
        header = f"cloud relay on {listener.name}, {time.strftime('%Y-%m-%d %H:%M:%S')}"
        if poller.live_mode:
            header += ", live mode"
        header += f", log {self.args.log}, Ctrl+C stops"
        lines = [header, "", "Module", "------", *self.module_lines(time.monotonic()), "", "Cloud", "-----", *self.cloud_lines(), ""]
        inverter = poller.inverter
        for group in GROUPS:
            result = inverter.results.get(group.key)
            if result is not None:
                lines += printing.group_lines(result, inverter.readings_of(group), self.args.full, show_time=True)
                lines.append("")
        session = poller.session
        lines += printing.counter_lines(session.connection if session is not None else None)
        lines.append("")
        return lines

    def module_lines(self, now: float) -> list[str]:
        poller = self.poller
        listener = poller.listener
        assert listener is not None
        session = poller.session
        if session is None:
            line = f"  waiting for the module on {listener.name}"
            if poller.last_session_end:
                line += f"; the last session ended: {poller.last_session_end}"
            return [line]
        connection = session.connection
        lines = [f"  connected from {connection.name} for {now - connection.connected_at:.0f} s, session {listener.sessions}, {session.state}"]
        if session.refused is not None:
            lines.append(f"  {session.refused}")
        if not poller.send_handshake:
            lines.append("  no handshake sent (--no-handshake)")
        elif session.module.is_empty:
            lines.append("  no handshake reply yet")
        else:
            lines.append(f"  {session.module.describe()}")
        return lines

    def cloud_lines(self) -> list[str]:
        settings = self.poller.cloud_settings
        if settings is None:
            return ["  none (--cloud or --cloud-ip sets one)"]
        line = f"  {settings.route}"
        cloud = self.poller.cloud
        if cloud is None:
            return [line + ", not connected: no module session"]
        status = cloud.status
        if status.address and status.address != settings.address.name:
            line += f", at {status.address}"
        line += ", connected" if status.connected else ", not connected"
        if status.error:
            line += f": {status.error}"
        channel = cloud.channel
        frames_in, bytes_in, bytes_out = (
            (channel.counters.frames_in, channel.counters.bytes_in, channel.counters.bytes_out) if channel is not None else (0, 0, 0)
        )
        traffic = f"  connects {cloud.counters.connects}, frames in {frames_in}, bytes in {bytes_in}, out {bytes_out}"
        handled = cloud.counters
        policy = (
            f"  reads answered from the state {handled.answered_from_state}, requests forwarded {handled.forwarded}, "
            f"writes blocked {handled.writes_blocked}, bad-CRC frames dropped {handled.bad_crc_dropped}"
        )
        lines = [line, traffic, policy]
        write = cloud.last_write
        if write is not None:
            kind = "clock write" if write.is_clock else "write"
            outcome = "forwarded" if write.forwarded else "blocked"
            lines.append(f"  last {kind} {write.describe()} at {time.strftime('%H:%M:%S', time.localtime(write.at))}, {outcome}")
        return lines


def start_log(path: str) -> None:
    """Everything logged, debug level included, goes to the file, appended."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def run(args: argparse.Namespace) -> int:
    groups = printing.chosen_groups(args, DEFAULT_GROUPS)
    inverter = Inverter(enabled={group.key: group in groups for group in GROUPS})
    open_cloud: Opener | None = None
    cloud_via = ""
    if args.proxy_address is not None:
        open_cloud = functools.partial(open_through_socks5, *args.proxy_address)
        cloud_via = f"SOCKS5 proxy {args.proxy_address[0]}:{args.proxy_address[1]}"
    cloud = None
    if args.cloud_address is not None:
        cloud = CloudSettings(
            args.cloud_address,
            forward_unknown_writes=args.forward_unknown_writes,
            forward_bad_crc=args.forward_bad_crc,
            open_cloud=open_cloud,
            cloud_via=cloud_via,
        )
    poller = Poller(inverter, server=ServerSettings(args.bind, args.port), cloud=cloud, send_handshake=not args.no_handshake, clear_on_session_end=True)
    if args.live:
        poller.live_mode = True
    screen = Screen(args, poller)
    poller.on_update = screen.show
    try:
        await poller.start()
    except ChannelError as error:
        LOGGER.error("%s", error)
        print(f"cannot wait for the module: {error}", file=sys.stderr)
        return EXIT_NO_PORT
    screen.show()
    try:
        await asyncio.Event().wait()
    finally:
        await poller.stop()
    return EXIT_OK


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        start_log(args.log)
    except OSError as error:
        raise SystemExit(f"cannot open the log file {args.log}: {error}") from error
    LOGGER.info("started: %s", " ".join(sys.argv[1:]) or "no options")
    try:
        if args.check_proxy:
            return asyncio.run(check_proxy(args.proxy_address))
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        LOGGER.info("stopped by Ctrl+C")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
