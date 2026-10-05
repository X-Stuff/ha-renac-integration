"""The frames on the wire: Modbus RTU to unit 1, and the module's AA55 management frames.

A frame's length depends on the direction it came from: a master sends requests, the device sends replies.
The core builds fn 3 read requests, and in server mode fn 3 replies to the cloud from the cloud relay's state; it
never writes to the inverter.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum

from .crc import append_crc, crc16

UNIT_ID = 1

# Modbus function codes.
READ = 0x03
WRITE_SINGLE = 0x06
WRITE_MULTIPLE = 0x10
EXCEPTION_BIT = 0x80
FUNCTIONS = (READ, WRITE_SINGLE, WRITE_MULTIPLE)

# Modbus limits per frame.
MAX_READ_COUNT = 125
MAX_ADDRESS = 0xFFFF

# AA55 management frames: AA 55 01 00 00 FE cmd sub len payload checksum.
AA55_HEAD = b"\xaa\x55\x01\x00\x00\xfe"
AA55_REPLY_BIT = 0x80
AA55_MIN_LENGTH = len(AA55_HEAD) + 3 + 2

# Modbus exception codes by number.
EXCEPTION_NAMES = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "slave device failure",
    6: "slave device busy",
    11: "gateway target failed",
}


class FrameError(ValueError):
    """The bytes are not a valid frame."""


class ModbusKind(Enum):
    """What a Modbus frame is, given the direction it came from."""

    READ_REQUEST = "read request"
    READ_REPLY = "read reply"
    WRITE_SINGLE = "write single"
    WRITE_MULTIPLE_REQUEST = "write multiple request"
    WRITE_MULTIPLE_REPLY = "write multiple reply"
    EXCEPTION = "exception"


@dataclass(frozen=True)
class ModbusFrame:
    """One Modbus RTU frame. `data` holds the register bytes of a read reply, a write-single value or a write-multiple request.
    `crc_ok` is False only for a frame parsed without the CRC check whose CRC is wrong."""

    kind: ModbusKind
    function: int
    unit: int = UNIT_ID
    address: int | None = None
    count: int | None = None
    data: bytes = b""
    exception_code: int | None = None
    raw: bytes = b""
    crc_ok: bool = True

    def registers(self) -> tuple[int, ...]:
        """The data as big-endian 16-bit words."""
        return tuple(int.from_bytes(self.data[i : i + 2], "big") for i in range(0, len(self.data) - 1, 2))

    @property
    def is_exception(self) -> bool:
        return self.kind is ModbusKind.EXCEPTION

    @property
    def is_request(self) -> bool:
        return self.kind in (ModbusKind.READ_REQUEST, ModbusKind.WRITE_SINGLE, ModbusKind.WRITE_MULTIPLE_REQUEST)


@dataclass(frozen=True)
class AA55Frame:
    """One module-management frame. `sub` is the sub-command without its reply bit."""

    command: int
    sub: int
    is_reply: bool
    payload: bytes = b""
    raw: bytes = b""


Frame = ModbusFrame | AA55Frame


def _word(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 2], "big")


def modbus_length(head: bytes, from_master: bool) -> int | None:
    """The whole frame's length from its first bytes, or None while more bytes are needed to tell.

    Raises FrameError when these bytes cannot start a frame in this direction.
    """
    if len(head) < 2:
        return None
    unit, function = head[0], head[1]
    if unit != UNIT_ID:
        raise FrameError(f"unit {unit}")

    if function & EXCEPTION_BIT:
        if from_master or (function & ~EXCEPTION_BIT) not in FUNCTIONS:
            raise FrameError(f"function 0x{function:02x}")
        return 5

    if function == READ:
        if from_master:
            return 8
        if len(head) < 3:
            return None
        return 5 + head[2]

    if function == WRITE_SINGLE:
        return 8

    if function == WRITE_MULTIPLE:
        if not from_master:
            return 8
        if len(head) < 7:
            return None
        return 9 + head[6]

    raise FrameError(f"function 0x{function:02x}")


def parse_modbus(raw: bytes, from_master: bool, check_crc: bool = True) -> ModbusFrame:
    """The frame these bytes hold, CRC checked. Raises FrameError otherwise. Without `check_crc` a wrong CRC gives the
    frame marked `crc_ok` False."""
    length = modbus_length(raw[:7], from_master)
    if length is None or len(raw) != length:
        raise FrameError(f"length {len(raw)}, expected {length}")
    crc_ok = crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little")
    if check_crc and not crc_ok:
        raise FrameError("crc")
    frame = _modbus_frame(raw, from_master)
    return frame if crc_ok else dataclasses.replace(frame, crc_ok=False)


def _modbus_frame(raw: bytes, from_master: bool) -> ModbusFrame:
    unit, function = raw[0], raw[1]
    if function & EXCEPTION_BIT:
        return ModbusFrame(ModbusKind.EXCEPTION, function & ~EXCEPTION_BIT, unit, exception_code=raw[2], raw=raw)

    if function == READ:
        if from_master:
            return ModbusFrame(ModbusKind.READ_REQUEST, READ, unit, address=_word(raw, 2), count=_word(raw, 4), raw=raw)
        byte_count = raw[2]
        if byte_count % 2:
            raise FrameError(f"odd byte count {byte_count}")
        return ModbusFrame(ModbusKind.READ_REPLY, READ, unit, count=byte_count // 2, data=raw[3 : 3 + byte_count], raw=raw)

    if function == WRITE_SINGLE:
        return ModbusFrame(ModbusKind.WRITE_SINGLE, WRITE_SINGLE, unit, address=_word(raw, 2), count=1, data=raw[4:6], raw=raw)

    if from_master:
        return ModbusFrame(
            ModbusKind.WRITE_MULTIPLE_REQUEST, WRITE_MULTIPLE, unit, address=_word(raw, 2), count=_word(raw, 4), data=raw[7 : 7 + raw[6]], raw=raw
        )
    return ModbusFrame(ModbusKind.WRITE_MULTIPLE_REPLY, WRITE_MULTIPLE, unit, address=_word(raw, 2), count=_word(raw, 4), raw=raw)


def aa55_length(head: bytes) -> int | None:
    """The whole frame's length from its first nine bytes, or None while more bytes are needed."""
    if len(head) < 9:
        return None
    return AA55_MIN_LENGTH + head[8]


def aa55_checksum(data: bytes) -> int:
    """The 16-bit sum of every byte."""
    return sum(data) & 0xFFFF


def parse_aa55(raw: bytes) -> AA55Frame:
    """The frame these bytes hold, checksum checked. Raises FrameError otherwise."""
    length = aa55_length(raw[:9])
    if length is None or len(raw) != length:
        raise FrameError(f"length {len(raw)}, expected {length}")
    if raw[:6] != AA55_HEAD:
        raise FrameError("head")
    if aa55_checksum(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
        raise FrameError("checksum")

    sub = raw[7]
    return AA55Frame(command=raw[6], sub=sub & ~AA55_REPLY_BIT, is_reply=bool(sub & AA55_REPLY_BIT), payload=raw[9 : 9 + raw[8]], raw=raw)


def parse_frame(raw: bytes, from_master: bool) -> Frame:
    """The AA55 or Modbus frame these bytes hold, checked. Raises FrameError otherwise."""
    if raw[:1] == AA55_HEAD[:1]:
        return parse_aa55(raw)
    return parse_modbus(raw, from_master)


def build_read_request(address: int, count: int, unit: int = UNIT_ID) -> bytes:
    """A fn 3 request, CRC appended. The only Modbus request the core builds."""
    if not 1 <= count <= MAX_READ_COUNT:
        raise ValueError(f"count {count}")
    if not 0 <= address <= MAX_ADDRESS:
        raise ValueError(f"address {address}")
    body = bytes([unit, READ]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return append_crc(body)


def build_read_reply(words: tuple[int, ...], unit: int = UNIT_ID) -> bytes:
    """A fn 3 reply holding the words, CRC appended: the cloud relay's answer to the cloud from its state."""
    if not 1 <= len(words) <= MAX_READ_COUNT:
        raise ValueError(f"count {len(words)}")
    data = b"".join(word.to_bytes(2, "big") for word in words)
    return append_crc(bytes([unit, READ, len(data)]) + data)


def build_aa55_frame(command: int, sub: int, payload: bytes = b"", reply: bool = False) -> bytes:
    """An AA55 frame with its checksum."""
    if len(payload) > 0xFF:
        raise ValueError(f"payload {len(payload)} bytes")
    body = AA55_HEAD + bytes([command, sub | AA55_REPLY_BIT if reply else sub, len(payload)]) + payload
    return body + aa55_checksum(body).to_bytes(2, "big")


def describe_frame(frame: Frame) -> str:
    """One short line about the frame, for logs and hexdumps."""
    if isinstance(frame, AA55Frame):
        kind = "reply" if frame.is_reply else "request"
        return f"AA55 0x{frame.command:02x}/0x{frame.sub:02x} {kind}, {len(frame.payload)} B payload"
    if not frame.crc_ok:
        return _describe_modbus(frame) + ", bad CRC"
    return _describe_modbus(frame)


def describe_bytes(data: bytes, from_master: bool) -> str:
    """One short line about bytes that are one whole frame, for the logs."""
    try:
        return describe_frame(parse_frame(data, from_master))
    except FrameError as error:
        return f"not a frame: {error}"


def _describe_modbus(frame: ModbusFrame) -> str:
    if frame.kind is ModbusKind.EXCEPTION:
        name = EXCEPTION_NAMES.get(frame.exception_code or 0, "unknown")
        return f"fn{frame.function} exception {frame.exception_code:02d} ({name})"
    if frame.kind is ModbusKind.READ_REQUEST:
        return f"fn3 read {frame.address} x{frame.count}"
    if frame.kind is ModbusKind.READ_REPLY:
        return f"fn3 reply {frame.count} regs, {len(frame.raw)} B"
    if frame.kind is ModbusKind.WRITE_SINGLE:
        return f"fn6 write {frame.address} = {_word(frame.data, 0)}"
    if frame.kind is ModbusKind.WRITE_MULTIPLE_REQUEST:
        return f"fn16 write {frame.address} x{frame.count}: {frame.data.hex()}"
    return f"fn16 reply {frame.address} x{frame.count}"
