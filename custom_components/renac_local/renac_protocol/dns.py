"""A small DNS client: one A query over UDP to a chosen DNS server, standard library only.

The LAN's DNS may answer for the cloud's name with the redirect's address; asking a public DNS server directly gives
the real one. `resolve_host()` also takes an IP literal as it is, or asks the system's resolver.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import random
import socket

from .const import DEFAULT_DNS_SERVER

DNS_PORT = 53
TIMEOUT_S = 3.0
ATTEMPTS = 2

TYPE_A = 1
CLASS_IN = 1
FLAG_REPLY = 0x8000
FLAG_TRUNCATED = 0x0200
FLAG_RECURSION_DESIRED = 0x0100
RCODE_NAME_ERROR = 3
HEADER_BYTES = 12
MAX_NAME_BYTES = 253
MAX_LABEL_BYTES = 63
LABEL_POINTER = 0xC0


class DnsError(Exception):
    """The name could not be resolved."""


def build_query(name: str, query_id: int) -> bytes:
    """An A query for the name with recursion desired. Raises DnsError for a name DNS cannot carry."""
    # Header: id, flags, one question, no answer, authority or additional records.
    header = query_id.to_bytes(2, "big") + FLAG_RECURSION_DESIRED.to_bytes(2, "big") + (1).to_bytes(2, "big") + bytes(6)
    question = encode_name(name) + TYPE_A.to_bytes(2, "big") + CLASS_IN.to_bytes(2, "big")
    return header + question


def encode_name(name: str) -> bytes:
    """The name as DNS labels, ending with the empty root label."""
    try:
        text = name.rstrip(".").encode("idna")
    except UnicodeError as error:
        raise DnsError(f"bad name {name!r}: {error}") from error
    if not text or len(text) > MAX_NAME_BYTES:
        raise DnsError(f"bad name {name!r}")
    encoded = bytearray()
    for label in text.split(b"."):
        if not 1 <= len(label) <= MAX_LABEL_BYTES:
            raise DnsError(f"bad name {name!r}")
        encoded += bytes([len(label)]) + label
    return bytes(encoded + b"\x00")


def parse_reply(reply: bytes, query_id: int) -> list[str]:
    """The IPv4 addresses of the A records in the reply's answers. Raises DnsError for an error reply or a malformed one."""
    if len(reply) < HEADER_BYTES:
        raise DnsError(f"reply of {len(reply)} bytes")
    if int.from_bytes(reply[0:2], "big") != query_id:
        raise DnsError("reply to another query")
    flags = int.from_bytes(reply[2:4], "big")
    if not flags & FLAG_REPLY:
        raise DnsError("not a reply")
    if flags & FLAG_TRUNCATED:
        raise DnsError("reply truncated")
    rcode = flags & 0x000F
    if rcode == RCODE_NAME_ERROR:
        raise DnsError("no such name")
    if rcode != 0:
        raise DnsError(f"server error, rcode {rcode}")

    questions = int.from_bytes(reply[4:6], "big")
    answers = int.from_bytes(reply[6:8], "big")
    offset = HEADER_BYTES
    for _ in range(questions):
        offset = _skip_name(reply, offset) + 4
    addresses = []
    for _ in range(answers):
        offset = _skip_name(reply, offset)
        if offset + 10 > len(reply):
            raise DnsError("malformed reply")
        record_type = int.from_bytes(reply[offset : offset + 2], "big")
        record_class = int.from_bytes(reply[offset + 2 : offset + 4], "big")
        length = int.from_bytes(reply[offset + 8 : offset + 10], "big")
        offset += 10
        if offset + length > len(reply):
            raise DnsError("malformed reply")
        if record_type == TYPE_A and record_class == CLASS_IN and length == 4:
            addresses.append(socket.inet_ntoa(reply[offset : offset + 4]))
        offset += length
    if offset > len(reply):
        raise DnsError("malformed reply")
    if not addresses:
        raise DnsError("no address in the reply")
    return addresses


def _skip_name(reply: bytes, offset: int) -> int:
    """The offset just past the name at `offset`: labels up to the root label, or up to a pointer."""
    while True:
        if offset >= len(reply):
            raise DnsError("malformed reply")
        length = reply[offset]
        if length & LABEL_POINTER == LABEL_POINTER:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


class _QueryProtocol(asyncio.DatagramProtocol):
    """Waits for the reply to one query; replies to other queries are ignored."""

    def __init__(self, query_id: int, reply: asyncio.Future[list[str]]) -> None:
        self.query_id = query_id
        self.reply = reply

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self.reply.done() or len(data) < 2 or int.from_bytes(data[0:2], "big") != self.query_id:
            return
        try:
            self.reply.set_result(parse_reply(data, self.query_id))
        except DnsError as error:
            self.reply.set_exception(error)

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(DnsError(str(exc)))


async def query_a(name: str, server: str = DEFAULT_DNS_SERVER, *, port: int = DNS_PORT, timeout_s: float = TIMEOUT_S, attempts: int = ATTEMPTS) -> list[str]:
    """Asks the DNS server for the name's A records: their IPv4 addresses. A query without a reply is sent again, up to
    `attempts` times. Raises DnsError."""
    loop = asyncio.get_running_loop()
    for attempt in range(attempts):
        query_id = random.randrange(0x10000)
        query = build_query(name, query_id)
        reply: asyncio.Future[list[str]] = loop.create_future()
        try:
            transport, _ = await loop.create_datagram_endpoint(functools.partial(_QueryProtocol, query_id, reply), remote_addr=(server, port))
        except OSError as error:
            raise DnsError(f"{server}:{port}: {error}") from error
        try:
            transport.sendto(query)
            return await asyncio.wait_for(reply, timeout_s)
        except TimeoutError:
            if attempt + 1 < attempts:
                continue
        finally:
            transport.close()
    raise DnsError(f"no reply from {server}:{port} within {timeout_s:g} s, {attempts} attempts")


def is_ip_literal(host: str) -> bool:
    """True when the host is an IP address, not a name."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def resolve_host(host: str, dns_server: str | None = DEFAULT_DNS_SERVER) -> list[str]:
    """The addresses of the host: an IP literal as it is; a name through the DNS server, or the system's resolver when
    `dns_server` is None. Raises DnsError."""
    if is_ip_literal(host):
        return [host]
    if dns_server is not None:
        return await query_a(host, dns_server)
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as error:
        raise DnsError(f"{host}: {error}") from error
    addresses = list(dict.fromkeys(str(address[4][0]) for address in found))
    if not addresses:
        raise DnsError(f"{host}: no address")
    return addresses
