"""The DNS client: queries to Google's DNS 8.8.8.8 (marker `network`), and the reply parser on hand-made replies.

No test here resolves the cloud's name.
"""

from __future__ import annotations

import pytest

from renac_protocol.dns import DnsError, build_query, is_ip_literal, parse_reply, query_a, resolve_host


def test_the_query_bytes() -> None:
    expected = "1234 0100 0001 0000 0000 0000 03 646e73 06 676f6f676c65 00 0001 0001"
    assert build_query("dns.google", 0x1234) == bytes.fromhex(expected)
    assert build_query("dns.google.", 0x1234) == bytes.fromhex(expected)


@pytest.mark.parametrize("name", ["", "a..b", "x" * 64 + ".test", ("y" * 60 + ".") * 5 + "test"])
def test_a_name_dns_cannot_carry_is_refused(name: str) -> None:
    with pytest.raises(DnsError):
        build_query(name, 1)


@pytest.mark.network
async def test_resolve_host_through_google_dns() -> None:
    assert {"8.8.8.8", "8.8.4.4"} <= set(await resolve_host("dns.google", "8.8.8.8"))


async def test_no_dns_server_on_the_port_is_an_error() -> None:
    with pytest.raises(DnsError):
        await query_a("dns.google", "127.0.0.1", port=9, timeout_s=0.5, attempts=1)


async def test_resolve_host_takes_an_ip_address_as_it_is() -> None:
    assert await resolve_host("192.0.2.1", "127.0.0.1") == ["192.0.2.1"]
    assert is_ip_literal("192.0.2.1") and not is_ip_literal("dns.google")


async def test_resolve_host_through_the_systems_resolver() -> None:
    assert "127.0.0.1" in await resolve_host("localhost", None)


def reply(flags: int = 0x8180, answers: bytes = b"", count: int = 0, query_id: int = 7) -> bytes:
    question = bytes.fromhex("04 74657374 00 0001 0001")
    return query_id.to_bytes(2, "big") + flags.to_bytes(2, "big") + bytes.fromhex("0001") + count.to_bytes(2, "big") + bytes(4) + question + answers


# An A record named by a pointer to the question: 192.0.2.10.
A_RECORD = bytes.fromhex("c00c 0001 0001 0000003c 0004 c000020a")
# A CNAME record named by the pointer, its target written out: target.test.
CNAME_RECORD = bytes.fromhex("c00c 0005 0001 0000003c 000d 06 746172676574 04 74657374 00")
# The target's A record, the name written out: 192.0.2.20.
TARGET_A_RECORD = bytes.fromhex("06 746172676574 04 74657374 00 0001 0001 0000003c 0004 c0000214")


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"\x00\x07\x81", "bytes"),
        (reply(query_id=8), "another query"),
        (reply(flags=0x0100), "not a reply"),
        (reply(flags=0x8380), "truncated"),
        (reply(flags=0x8183), "no such name"),
        (reply(flags=0x8182), "rcode 2"),
        (reply(answers=A_RECORD[:-2], count=1), "malformed"),
        (reply(answers=A_RECORD, count=2), "malformed"),
        (reply(), "no address"),
        (reply(answers=CNAME_RECORD, count=1), "no address"),
    ],
)
def test_a_bad_reply_is_an_error(data: bytes, error: str) -> None:
    with pytest.raises(DnsError, match=error):
        parse_reply(data, 7)


def test_a_good_reply() -> None:
    assert parse_reply(reply(answers=A_RECORD, count=1), 7) == ["192.0.2.10"]


def test_a_cname_and_then_the_targets_address() -> None:
    assert parse_reply(reply(answers=CNAME_RECORD + TARGET_A_RECORD, count=2), 7) == ["192.0.2.20"]
