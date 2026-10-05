"""Frame tables, parsers and builders against frames of the protocol with made-up values."""

from __future__ import annotations

import pytest

from renac_protocol.crc import append_crc
from renac_protocol.frames import (
    AA55Frame,
    FrameError,
    ModbusFrame,
    ModbusKind,
    build_aa55_frame,
    build_read_reply,
    build_read_request,
    describe_frame,
    parse_aa55,
    parse_modbus,
)

IDENTITY_READ = bytes.fromhex("0103271000704f5f")
# A clock write at a made-up time, 2026-01-02 03:04:05, and its echo.
RTC_WORDS = (26, 1, 2, 3, 4, 5)
RTC_WRITE = append_crc(bytes.fromhex("01104e2100060c") + b"".join(word.to_bytes(2, "big") for word in RTC_WORDS))
RTC_WRITE_REPLY = bytes.fromhex("01104e2100060729")
EXCEPTION = bytes.fromhex("018302c0f1")
MODULE_INFO_REQUEST = bytes.fromhex("aa55010000fe1500000213")
# An inverter serial reply with a made-up serial; its checksum is the 16-bit sum of every byte before it.
SERIAL = b"1234567890123456"
SERIAL_PAYLOAD = SERIAL + bytes(16) + b"\x12\x34"
SERIAL_HEAD = bytes.fromhex("aa55010000fe0e80") + bytes([len(SERIAL_PAYLOAD)]) + SERIAL_PAYLOAD
SERIAL_REPLY = SERIAL_HEAD + (sum(SERIAL_HEAD) & 0xFFFF).to_bytes(2, "big")
# A read reply of six made-up words.
VERSIONS = (123, 102, 204, 0, 0, 39)
VERSIONS_REPLY = append_crc(bytes.fromhex("01030c") + b"".join(word.to_bytes(2, "big") for word in VERSIONS))


def test_read_request_round_trip() -> None:
    assert build_read_request(10000, 112) == IDENTITY_READ
    frame = parse_modbus(IDENTITY_READ, from_master=True)
    assert frame == ModbusFrame(ModbusKind.READ_REQUEST, 3, 1, address=10000, count=112, raw=IDENTITY_READ)
    assert frame.is_request
    assert describe_frame(frame) == "fn3 read 10000 x112"


def test_read_request_limits() -> None:
    with pytest.raises(ValueError):
        build_read_request(10000, 0)
    with pytest.raises(ValueError):
        build_read_request(10000, 126)
    with pytest.raises(ValueError):
        build_read_request(70000, 1)


def test_read_reply_registers() -> None:
    frame = parse_modbus(VERSIONS_REPLY, from_master=False)
    assert frame.kind is ModbusKind.READ_REPLY
    assert frame.count == 6
    assert frame.registers() == VERSIONS
    assert describe_frame(frame) == "fn3 reply 6 regs, 17 B"


def test_the_same_bytes_are_not_a_frame_in_the_other_direction() -> None:
    with pytest.raises(FrameError):
        parse_modbus(IDENTITY_READ, from_master=False)
    with pytest.raises(FrameError):
        parse_modbus(RTC_WRITE, from_master=False)


def test_write_multiple_request_and_reply() -> None:
    request = parse_modbus(RTC_WRITE, from_master=True)
    assert request.kind is ModbusKind.WRITE_MULTIPLE_REQUEST
    assert (request.address, request.count) == (20001, 6)
    assert request.registers() == RTC_WORDS
    reply = parse_modbus(RTC_WRITE_REPLY, from_master=False)
    assert reply.kind is ModbusKind.WRITE_MULTIPLE_REPLY
    assert (reply.address, reply.count) == (20001, 6)
    assert describe_frame(reply) == "fn16 reply 20001 x6"


def test_write_single_parses_both_ways() -> None:
    raw = append_crc(bytes.fromhex("01064e200001"))
    request = parse_modbus(raw, from_master=True)
    reply = parse_modbus(raw, from_master=False)
    assert request.kind is reply.kind is ModbusKind.WRITE_SINGLE
    assert request.address == 20000 and request.registers() == (1,)
    assert describe_frame(request) == "fn6 write 20000 = 1"


def test_exception_reply() -> None:
    frame = parse_modbus(EXCEPTION, from_master=False)
    assert frame.kind is ModbusKind.EXCEPTION
    assert frame.function == 3
    assert frame.exception_code == 2
    assert frame.is_exception
    assert describe_frame(frame) == "fn3 exception 02 (illegal data address)"
    with pytest.raises(FrameError):
        parse_modbus(EXCEPTION, from_master=True)


def test_bad_crc_is_refused() -> None:
    with pytest.raises(FrameError):
        parse_modbus(bytes.fromhex("01032b7100039df4"), from_master=True)


def test_bad_crc_without_the_check_is_marked() -> None:
    raw = bytes.fromhex("01032b7100039df4")
    frame = parse_modbus(raw, from_master=True, check_crc=False)
    assert (frame.kind, frame.address, frame.count, frame.crc_ok, frame.raw) == (ModbusKind.READ_REQUEST, 11121, 3, False, raw)
    assert describe_frame(frame) == "fn3 read 11121 x3, bad CRC"
    assert parse_modbus(IDENTITY_READ, from_master=True, check_crc=False).crc_ok


def test_read_reply_from_words() -> None:
    assert build_read_reply(VERSIONS) == VERSIONS_REPLY
    with pytest.raises(ValueError):
        build_read_reply(())
    with pytest.raises(ValueError):
        build_read_reply((0,) * 126)


def test_aa55_request_and_reply() -> None:
    request = parse_aa55(MODULE_INFO_REQUEST)
    assert request == AA55Frame(command=0x15, sub=0x00, is_reply=False, raw=MODULE_INFO_REQUEST)
    assert build_aa55_frame(0x15, 0x00) == MODULE_INFO_REQUEST
    assert describe_frame(request) == "AA55 0x15/0x00 request, 0 B payload"

    reply = parse_aa55(SERIAL_REPLY)
    assert (reply.command, reply.sub, reply.is_reply) == (0x0E, 0x00, True)
    assert reply.payload[:16] == SERIAL
    assert len(reply.payload) == 34
    assert build_aa55_frame(0x0E, 0x00, reply.payload, reply=True) == SERIAL_REPLY


def test_aa55_checksum_is_checked() -> None:
    broken = MODULE_INFO_REQUEST[:-1] + b"\x14"
    with pytest.raises(FrameError):
        parse_aa55(broken)
    with pytest.raises(FrameError):
        parse_aa55(b"\xaa\x55\x02\x00\x00\xfe\x15\x00\x00\x02\x14")
