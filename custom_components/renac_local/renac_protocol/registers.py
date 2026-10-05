"""The register model and the decoding of a read reply into values.

A `Register` says how to read one value out of the words of a block: its address, how many words, their type,
the divisor or the step, and the unit. `decode_block()` turns the words of one fn 3 reply into the `Reading`s of every register
inside it. The table itself is in `register_table.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

# The three phases, L1, L2, L3, as key suffixes; the vendor document calls them R, S, T.
PHASES = ("l1", "l2", "l3")


class Kind(Enum):
    """How the words of a register are read."""

    U16 = "u16"
    I16 = "i16"
    U32 = "u32"
    I32 = "i32"
    TEXT = "text"
    BITS = "bits"
    ENUM = "enum"
    VERSION = "version"


@dataclass(frozen=True)
class Register:
    """One value in the register map. `scale` divides the raw number: the vendor document's gain. `step` multiplies
    it, for a gain below 1: gain 0.1 is step 10, each count 10 of the unit. `max_age_s` is how long a word read from it
    stays valid in the cloud relay's state, when the register has a limit of its own; otherwise its group's limit applies."""

    address: int
    key: str
    name: str
    kind: Kind = Kind.U16
    scale: int = 1
    unit: str = ""
    step: int = 1
    words: int = 1
    enum: Mapping[int, str] | None = None
    bit_names: tuple[str, ...] | None = None
    text_of: Callable[[int], str] | None = None
    diagnostic: bool = False
    disabled: bool = False
    max_age_s: float | None = None

    def __post_init__(self) -> None:
        if self.kind in (Kind.U32, Kind.I32):
            object.__setattr__(self, "words", 2)
        if self.kind is Kind.TEXT and self.words == 1:
            object.__setattr__(self, "words", 16)

    @property
    def end(self) -> int:
        """The first address past this register."""
        return self.address + self.words

    def decode(self, words: tuple[int, ...]) -> Reading:
        """The reading of this register from its own words."""
        if len(words) != self.words:
            raise ValueError(f"{self.key}: {len(words)} words, expected {self.words}")
        value: float | int | str
        text: str

        if self.kind is Kind.TEXT:
            raw_bytes = b"".join(word.to_bytes(2, "big") for word in words)
            value = raw_bytes.replace(b"\x00", b" ").decode("ascii", "replace").strip()
            text = value
        elif self.kind is Kind.VERSION:
            value = words[0] / 100
            text = f"{words[0] // 100}.{words[0] % 100:02d}"
        elif self.kind is Kind.ENUM:
            value = words[0]
            text = (self.enum or {}).get(words[0], f"unknown ({words[0]})")
        elif self.kind is Kind.BITS:
            value = words[0]
            text = self.text_of(words[0]) if self.text_of is not None else (", ".join(self.bit_names_of(words[0])) or "none")
        else:
            value = _scaled(_integer(self.kind, words) * self.step, self.scale)
            text = _number_text(value, self.scale) + (f" {self.unit}" if self.unit else "")

        return Reading(self, words, value, text)

    def bit_names_of(self, word: int) -> list[str]:
        """The names of the bits set in the word; unnamed bits as `bit N`."""
        names = []
        for bit in range(16):
            if word & (1 << bit):
                name = self.bit_names[bit] if self.bit_names and bit < len(self.bit_names) else ""
                names.append(name or f"bit {bit}")
        return names


@dataclass(frozen=True)
class Reading:
    """One decoded value: the register, its raw words, the value with the scale applied, and a text with the unit."""

    register: Register
    words: tuple[int, ...]
    value: float | int | str
    text: str

    @property
    def key(self) -> str:
        return self.register.key

    @property
    def words_hex(self) -> str:
        """The raw words as hex, space separated."""
        return " ".join(f"{word:04x}" for word in self.words)


@dataclass
class Block:
    """The words of one fn 3 reply, with the address they start at."""

    address: int
    words: tuple[int, ...] = field(default_factory=tuple)

    @property
    def end(self) -> int:
        return self.address + len(self.words)

    def holds(self, register: Register) -> bool:
        """True when every word of the register is inside the block."""
        return self.address <= register.address and register.end <= self.end

    def words_of(self, register: Register) -> tuple[int, ...]:
        start = register.address - self.address
        return self.words[start : start + register.words]


def decode_block(block: Block, registers: Iterable[Register]) -> dict[str, Reading]:
    """The readings of every register the block holds, keyed by register key, in table order."""
    readings: dict[str, Reading] = {}
    for register in registers:
        if block.holds(register):
            readings[register.key] = register.decode(block.words_of(register))
    return readings


# The sixteen fault words, shown and reported as one.
FAULT_WORD_ADDRESSES = range(11060, 11076)


def fault_summary(readings: Mapping[str, Reading]) -> tuple[str, str]:
    """The fault words among the readings as one text: the set bits by name, `fault N bit M` for a bit without one,
    `none` when every word is clear; and the non-zero words as `address=hex`."""
    names = []
    words = []
    for reading in readings.values():
        register = reading.register
        word = reading.words[0]
        if register.address not in FAULT_WORD_ADDRESSES or word == 0:
            continue
        number = register.address - FAULT_WORD_ADDRESSES.start + 1
        for bit in range(16):
            if word & (1 << bit):
                name = register.bit_names[bit] if register.bit_names and bit < len(register.bit_names) else ""
                names.append(name or f"fault {number} bit {bit}")
        words.append(f"{register.address}={word:04x}")
    return ", ".join(names) or "none", " ".join(words)


# The inverter's clock, six registers from 20001, shown as one.
CLOCK_KEYS = ("rtc_year", "rtc_month", "rtc_day", "rtc_hour", "rtc_minute", "rtc_second")


def clock_text(readings: Mapping[str, Reading]) -> str | None:
    """The inverter's clock as `hh:mm:ss - dd/MM/yy`; None unless all six clock registers are among the readings."""
    if not all(key in readings for key in CLOCK_KEYS):
        return None
    year, month, day, hour, minute, second = (readings[key].words[0] for key in CLOCK_KEYS)
    return f"{hour:02d}:{minute:02d}:{second:02d} - {day:02d}/{month:02d}/{year:02d}"


def _integer(kind: Kind, words: tuple[int, ...]) -> int:
    if kind in (Kind.U32, Kind.I32):
        value = (words[0] << 16) | words[1]
        if kind is Kind.I32 and value & 0x80000000:
            value -= 0x100000000
        return value
    value = words[0]
    if kind is Kind.I16 and value & 0x8000:
        value -= 0x10000
    return value


def _scaled(value: int, scale: int) -> float | int:
    return value if scale == 1 else value / scale


def _number_text(value: float | int, scale: int) -> str:
    decimals = {1: 0, 10: 1, 100: 2, 1000: 3}.get(scale, 1)
    return f"{value:.{decimals}f}"
