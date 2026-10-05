"""CRC-16/MODBUS: init 0xFFFF, polynomial 0xA001, appended low byte first."""

from __future__ import annotations


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16(data: bytes) -> int:
    """The CRC of the bytes as a 16-bit integer."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def append_crc(body: bytes) -> bytes:
    """The body with its CRC appended, low byte first."""
    return body + crc16(body).to_bytes(2, "little")
