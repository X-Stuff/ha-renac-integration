"""The rows the tools print: the clock as one row, the sixteen fault words as one row, the three phases of a value as
one row, registers off by default only with `full`."""

from __future__ import annotations

from renac_protocol.register_table import LIVE, PHASES, REGISTERS_BY_KEY, SETTINGS
from renac_protocol.registers import Block, decode_block, fault_summary
from tools.printing import reading_rows


def test_the_clock_is_one_row_when_all_six_are_there() -> None:
    clock = decode_block(Block(20001, (26, 1, 2, 3, 4, 5)), SETTINGS)
    assert reading_rows(clock) == [("Clock", "03:04:05 - 02/01/26", "20001", "001a 0001 0002 0003 0004 0005")]
    partial = {key: reading for key, reading in clock.items() if key != "rtc_second"}
    assert len(reading_rows(partial)) == 5


def test_faults_phases_and_disabled_registers_in_the_rows() -> None:
    words = [0] * 114
    voltages = [REGISTERS_BY_KEY[f"grid_voltage_{phase}"] for phase in PHASES]
    for register, word in zip(voltages, (2312, 2298, 2305), strict=True):
        words[register.address - 11000] = word
    fault_1 = REGISTERS_BY_KEY["fault_1"]
    words[fault_1.address - 11000] = 0b1
    words[fault_1.address + 2 - 11000] = 1 << 4
    readings = decode_block(Block(11000, tuple(words)), LIVE)
    rows = reading_rows(readings)
    names = [row[0] for row in rows]

    # The sixteen fault words: one row, the set bits by name or by word and bit.
    faults = [row for row in rows if row[0] == "Faults"]
    assert len(faults) == 1 and not any(name.startswith("Fault word") for name in names)
    assert fault_summary(readings) == (f"{fault_1.bit_names[0]}, fault 3 bit 4", f"{fault_1.address}=0001 {fault_1.address + 2}=0010")
    # The three phases: one row, in the place of L1.
    phase_texts = "/".join(readings[register.key].text.removesuffix(" V") for register in voltages)
    assert (f"{voltages[0].name.removesuffix(' L1')} L1/L2/L3", f"{phase_texts} V") in [row[:2] for row in rows]
    assert not any(name.endswith((" L1", " L2", " L3")) for name in names)
    partial = {key: reading for key, reading in readings.items() if key != voltages[2].key}
    assert {voltages[0].name, voltages[1].name} <= {row[0] for row in reading_rows(partial)}
    # Registers off by default only with `full`.
    disabled = next(register for register in LIVE if register.disabled and register.key.rpartition("_")[2] not in PHASES)
    assert disabled.name not in names and disabled.name in [row[0] for row in reading_rows(readings, full=True)]
