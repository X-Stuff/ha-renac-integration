"""Splits one direction of a byte stream into frames.

Leading 0x00 / 0xFF padding is skipped and counted; a frame's length comes from the frame tables; a frame whose CRC
or checksum fails loses its first byte and the parser looks again (resync); a partial frame left alone for longer
than the idle timeout is dropped as garbage. With `pass_bad_crc` (the cloud's direction in server mode) a Modbus frame
whose CRC fails is handed on whole instead, marked `crc_ok` False, for the server to drop or forward.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import IDLE_TIMEOUT_S
from .frames import AA55_HEAD, UNIT_ID, Frame, FrameError, ModbusFrame, aa55_length, modbus_length, parse_frame, parse_modbus

PADDING_BYTES = (0x00, 0xFF)


@dataclass
class FramerCounters:
    """What the framer has seen."""

    frames: int = 0
    padding_bytes: int = 0
    garbage_bytes: int = 0
    crc_errors: int = 0
    partial_discards: int = 0


class Framer:
    """A stream parser for one direction: `from_master` True for a master's bytes (requests), False for the device's (replies)."""

    def __init__(self, from_master: bool, idle_timeout_s: float = IDLE_TIMEOUT_S, pass_bad_crc: bool = False) -> None:
        self.from_master = from_master
        self.idle_timeout_s = idle_timeout_s
        self.pass_bad_crc = pass_bad_crc
        self.counters = FramerCounters()
        self._buffer = bytearray()
        self._last_byte_at: float | None = None
        self._resyncing = False

    @property
    def pending_bytes(self) -> int:
        """Bytes waiting for the rest of their frame."""
        return len(self._buffer)

    def feed(self, data: bytes, now: float) -> list[Frame]:
        """Adds bytes received at `now` (monotonic seconds) and returns the complete frames found."""
        self.flush_idle(now)
        self._buffer += data
        self._last_byte_at = now
        return self._extract()

    def flush_idle(self, now: float) -> None:
        """Drops a partial frame that has waited longer than the idle timeout."""
        if self._buffer and self._last_byte_at is not None and now - self._last_byte_at > self.idle_timeout_s:
            self.drop_partial()

    def drop_partial(self) -> None:
        """Drops a partial frame at once, counted as garbage; for a new connection, whose bytes do not continue it."""
        if self._buffer:
            self.counters.partial_discards += 1
            self.counters.garbage_bytes += len(self._buffer)
            self._buffer.clear()
        self._resyncing = False

    def _extract(self) -> list[Frame]:
        frames: list[Frame] = []
        while self._buffer:
            self._skip_padding()
            if not self._buffer:
                break

            try:
                length = self._frame_length()
            except FrameError:
                self._drop_first_byte()
                continue
            if length is None or len(self._buffer) < length:
                break

            raw = bytes(self._buffer[:length])
            try:
                frame = self._parse(raw)
            except FrameError:
                if not self._resyncing:
                    self.counters.crc_errors += 1
                self._drop_first_byte()
                continue

            del self._buffer[:length]
            self._resyncing = False
            if isinstance(frame, ModbusFrame) and not frame.crc_ok:
                self.counters.crc_errors += 1
            else:
                self.counters.frames += 1
            frames.append(frame)
        return frames

    def _skip_padding(self) -> None:
        skipped = 0
        while skipped < len(self._buffer) and self._buffer[skipped] in PADDING_BYTES:
            skipped += 1
        if skipped:
            del self._buffer[:skipped]
            self.counters.padding_bytes += skipped

    def _drop_first_byte(self) -> None:
        del self._buffer[0]
        self.counters.garbage_bytes += 1
        self._resyncing = True

    def _frame_length(self) -> int | None:
        head = bytes(self._buffer[:9])
        if head[0] == AA55_HEAD[0]:
            if len(head) >= 2 and head[1] != AA55_HEAD[1]:
                raise FrameError("not AA55")
            return aa55_length(head) if len(head) >= 2 else None
        if head[0] != UNIT_ID:
            raise FrameError(f"unit {head[0]}")
        return modbus_length(head, self.from_master)

    def _parse(self, raw: bytes) -> Frame:
        if self.pass_bad_crc and raw[0] != AA55_HEAD[0]:
            return parse_modbus(raw, self.from_master, check_crc=False)
        return parse_frame(raw, self.from_master)
