"""CRC-16/MODBUS against its standard check value and a request of the protocol."""

from __future__ import annotations

from renac_protocol.crc import append_crc, crc16


def test_the_standard_check_value() -> None:
    # CRC-16/MODBUS of the ASCII digits 1 to 9, the catalogue's check value.
    assert crc16(b"123456789") == 0x4B37


def test_append_crc_puts_the_low_byte_first() -> None:
    assert append_crc(bytes.fromhex("010327100070")) == bytes.fromhex("0103271000704f5f")
