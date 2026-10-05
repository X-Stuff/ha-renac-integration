"""What the handshake's replies say, with random module and inverter data, and sending the handshake to the fake
module through a listener's session."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest

from renac_protocol.connection import Connection
from renac_protocol.frames import AA55Frame, build_aa55_frame, parse_aa55
from renac_protocol.handshake import (
    HANDSHAKE,
    INVERTER_SERIAL,
    INVERTER_SERIAL_FIELD,
    MODULE_INFO,
    MODULE_INFO_FIELDS,
    UNKNOWN_15_0B,
    ModuleInfo,
    build_request,
    send_handshake,
)
from renac_protocol.listener import Listener
from tests.helpers import Modules, wait_until
from tests.mocks.mock_hf_lpt270 import TcpModule


def random_text(alphabet: str, length: int) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def with_texts(length: int, texts: dict[tuple[int, int], str]) -> bytes:
    """A payload of `length` bytes holding each text at its field, NUL padded."""
    payload = bytearray(length)
    for (start, end), text in texts.items():
        payload[start:end] = text.encode("ascii").ljust(end - start, b"\x00")
    return bytes(payload)


@pytest.fixture
async def session() -> AsyncIterator[tuple[Connection, TcpModule]]:
    """A listener's session with the fake module dialling in."""
    sessions: list[Connection] = []
    listener = Listener("127.0.0.1", 0, sessions.append, reply_timeout_s=0.2, request_gap_s=0.0)
    await listener.start()
    modules = Modules()
    try:
        module = modules.dial(listener.port)
        await wait_until(lambda: len(sessions) == 1)
        yield sessions[0], module
    finally:
        await modules.stop()
        await listener.stop()


def test_the_replies_give_the_module_and_the_inverter() -> None:
    module_serial = random_text("0123456789ABCDEF", 16)
    mac = ":".join(f"{random.randrange(256):02X}" for _ in range(6))
    firmware = "LFWIFI" + random_text("0123456789", 12)
    ip = ".".join(str(random.randrange(1, 255)) for _ in range(4))
    inverter_serial = random_text("0123456789", 16)
    fields = MODULE_INFO_FIELDS
    module_info = with_texts(150, {fields["module_serial"]: module_serial, fields["mac"]: mac, fields["firmware"]: firmware, fields["ip"]: ip})
    serial = with_texts(34, {INVERTER_SERIAL_FIELD: inverter_serial})

    info = ModuleInfo()
    assert info.is_empty
    assert info.update_from(parse_aa55(build_aa55_frame(*MODULE_INFO, module_info, reply=True)))
    assert not info.update_from(parse_aa55(build_aa55_frame(*UNKNOWN_15_0B, b"\x01" + module_serial[:7].encode("ascii"), reply=True)))
    assert info.update_from(parse_aa55(build_aa55_frame(*INVERTER_SERIAL, serial, reply=True)))
    assert info == ModuleInfo(module_serial, mac, firmware, ip, inverter_serial)


async def test_send_handshake_sends_the_clouds_requests_and_takes_the_replies(session: tuple[Connection, TcpModule]) -> None:
    connection, module = session
    info = ModuleInfo()
    await send_handshake(connection, info)
    assert [(frame.command, frame.sub) for frame in module.requests if isinstance(frame, AA55Frame)] == list(HANDSHAKE)
    identity = module.module.identity
    assert info == ModuleInfo(identity.module_serial, identity.mac, identity.module_firmware, identity.ip, identity.inverter_serial)


async def test_a_handshake_request_without_a_reply_is_skipped(session: tuple[Connection, TcpModule]) -> None:
    connection, module = session
    module.drop.add(build_request(*MODULE_INFO))
    info = ModuleInfo()
    await send_handshake(connection, info)
    assert info.module_serial == "" and info.inverter_serial == module.module.identity.inverter_serial
