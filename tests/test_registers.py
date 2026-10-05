"""The register model and the decoders on registers made up for the test; the table's own consistency."""

from __future__ import annotations

from renac_protocol.register_table import ALL_REGISTERS, POWER_32BIT, REGISTERS_BY_KEY, battery_status_text
from renac_protocol.registers import Block, Kind, Register, decode_block


def test_every_key_is_unique_and_addresses_do_not_overlap() -> None:
    assert len(REGISTERS_BY_KEY) == len(ALL_REGISTERS)
    by_address = sorted(ALL_REGISTERS + POWER_32BIT, key=lambda r: r.address)
    for earlier, later in zip(by_address, by_address[1:], strict=False):
        assert earlier.end <= later.address, f"{earlier.key} overlaps {later.key}"


def test_the_32bit_registers_stand_for_16bit_ones_under_their_keys() -> None:
    for register in POWER_32BIT:
        documented = REGISTERS_BY_KEY[register.key]
        assert register.kind is Kind.I32 and documented.kind is Kind.I16, register.key
        assert (register.name, register.unit, register.disabled) == (documented.name, documented.unit, documented.disabled), register.key


def test_numbers_signed_scaled_and_stepped() -> None:
    assert Register(1, "a", "A", Kind.I16, 10, "A").decode((0xFFF6,)).value == -1.0
    assert Register(1, "a", "A", Kind.I32, 1, "W").decode((0xFFFF, 0xFFFD)).value == -3
    assert Register(1, "a", "A", Kind.U32, 10, "kWh").decode((1, 0)).value == 6553.6
    assert Register(1, "a", "A", Kind.U16, 1000, "V").decode((3300,)).text == "3.300 V"
    stepped = Register(1, "a", "A", Kind.U16, unit="ms", step=10).decode((50,))
    assert (stepped.value, stepped.text) == (500, "500 ms")


def test_text_and_version() -> None:
    data = b"N3-HV-10.0".ljust(32, b"\x00")
    words = tuple(int.from_bytes(data[at : at + 2], "big") for at in range(0, 32, 2))
    assert Register(1, "t", "T", Kind.TEXT).decode(words).value == "N3-HV-10.0"
    version = Register(1, "v", "V", Kind.VERSION).decode((123,))
    assert (version.value, version.text) == (1.23, "1.23")


def test_bits_and_enums() -> None:
    bits = Register(1, "b", "B", Kind.BITS, bit_names=("first", "", "third"))
    assert bits.decode((0b101,)).text == "first, third"
    assert bits.decode((0b10,)).text == "bit 1"
    assert bits.decode((0,)).text == "none"
    assert Register(1, "b", "B", Kind.BITS, text_of=lambda word: f"word {word}").decode((5,)).text == "word 5"
    state = Register(1, "s", "S", Kind.ENUM, enum={2: "running"})
    assert state.decode((2,)).text == "running"
    assert state.decode((99,)).text == "unknown (99)"


def test_battery_status_takes_the_state_and_the_request_bits() -> None:
    plain, with_request = battery_status_text(2), battery_status_text(2 | (1 << 3))
    assert with_request.startswith(plain + ", ") and with_request != plain


def test_a_block_decodes_the_registers_it_holds_whole() -> None:
    registers = (Register(10, "a", "A"), Register(11, "b", "B", Kind.U32), Register(13, "c", "C"))
    readings = decode_block(Block(10, (1, 0, 2)), registers)
    assert [key for key in readings] == ["a", "b"] and readings["b"].value == 2
