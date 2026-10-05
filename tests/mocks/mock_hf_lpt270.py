"""A fake HF-LPT270 on this computer's Bluetooth: service FEE7 with FED5 (write) and FED6 (indicate), a simulated N3-HV behind it.

The relay connects to it as it would to the real module. Every register the poll groups read, the undocumented ones
left out, starts from made-up values: its identity, made-up BMS serials, the vendor's defaults where its document
gives one, values inside the document's scopes for the rest of the settings and protection, the energy counters random
(today's at 0). Then it moves every second as a real inverter's would: the sun rises and sets over a day (--day), PV
power, voltages and currents follow it, the house's consumption wanders, the grid meter is consumption minus output,
temperatures follow the output, the energy counters add up. The clock starts on UTC+8, the demo plant's time zone,
and runs on from the last write to it. A fn 3 read inside the known registers is answered from that state, as one
indication or in pieces of the subscriber's MTU - 3; an unknown range gets exception 02; a wrong CRC gets nothing; a
write into the settings is taken and echoed.

With --tcp HOST:PORT it is the module of server mode instead, without Bluetooth: socket B dials the server as the module
dials the cloud, sends nothing until asked, answers the AA55 handshake besides Modbus, and dials again after a drop. It
then also answers AT on UDP (--at-port): the handshake, AT+SOCKB, the query and the set, which moves socket B at once,
and AT+Q. With --link PORT it listens on TCP in place of Bluetooth, for the mock relay. --tcp and --link together are
one module on both paths: the inverter's replies go out on both, as the real module's one UART feeds both.

Its identity is Renac's demo plant's; USE_RANDOM_SN_INV and USE_RANDOM_SN_MOD give the inverter's and the module's
serial a random tail, a new one every run; --model and --serial give the inverter's model and serial, the rated power
following the model. Its MAC is made from the module serial; its IP is its own address, on socket B's connection and
toward whoever asks on the AT side. The tests give it a random identity, `random_identity()`.

It uses the core (renac_protocol) and no data of a real unit; the Bluetooth part is bless, in the repo's venv. From
the repo's root:

    .venv\\Scripts\\python.exe -m tests.mocks.mock_hf_lpt270
    .venv\\Scripts\\python.exe -m tests.mocks.mock_hf_lpt270 -v
    .venv\\Scripts\\python.exe -m tests.mocks.mock_hf_lpt270 --tcp 127.0.0.1:20145
    python3 -m tests.mocks.mock_hf_lpt270 --tcp 127.0.0.1:20146 --link 18898
    python3 -m tests.mocks.mock_hf_lpt270 --tcp 127.0.0.1:20146 --model N3-HV-6.0 --serial 8301063231000206

Windows advertises the computer's own name, not the --name: in the relay's page, scan, pick this computer; the
service and both characteristics resolve by Auto (FED5 is write only, FED6 indicate only). Ctrl+C stops it.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import contextlib
import logging
import math
import random
import socket
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "renac_local"))

from renac_protocol.const import AT_PORT  # noqa: E402
from renac_protocol.crc import append_crc  # noqa: E402
from renac_protocol.framer import Framer  # noqa: E402
from renac_protocol.frames import (  # noqa: E402
    EXCEPTION_BIT,
    MAX_READ_COUNT,
    READ,
    UNIT_ID,
    Frame,
    ModbusFrame,
    ModbusKind,
    build_aa55_frame,
    describe_frame,
)
from renac_protocol.groups import GROUPS, UNDOCUMENTED_GROUP  # noqa: E402
from renac_protocol.handshake import INVERTER_SERIAL, INVERTER_SERIAL_FIELD, MODULE_INFO, MODULE_INFO_FIELDS, UNKNOWN_15_0B  # noqa: E402
from renac_protocol.register_table import REGISTERS_BY_KEY  # noqa: E402

LOGGER = logging.getLogger("mock_hf_lpt270")


def full_uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


SERVICE = full_uuid("fee7")
FED5 = full_uuid("fed5")
FED6 = full_uuid("fed6")

DEFAULT_NAME = "MOCK-HF-LPT270"


DIGITS = "0123456789"
HEX_DIGITS = "0123456789ABCDEF"
# The N3-HV models a random identity takes one of.
MODELS = ("N3-HV-5.0", "N3-HV-6.0", "N3-HV-8.0", "N3-HV-10.0")


def random_text(alphabet: str, length: int) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


@dataclass(frozen=True)
class Identity:
    """Who the fake says it is: the inverter's model and serial; the module's serial, MAC, IP and firmware. The IP is
    the module's own address once socket B has connected (`FakeModule.take_ip()`)."""

    model: str
    inverter_serial: str
    module_serial: str
    mac: str
    ip: str = "127.0.0.1"
    module_firmware: str = "LFWIFI202406120000"


def mac_of(module_serial: str) -> str:
    """A made-up, locally administered MAC: 02 and the module serial's last ten hex digits, so two modules differ."""
    tail = module_serial[-10:]
    return ":".join(["02", *(tail[at : at + 2] for at in range(0, 10, 2))])


def demo_identity() -> Identity:
    """Renac's demo plant, whose inverter serial the cloud runs a whole session for, or its serials with random tails of
    the same length."""

    # True: the serials get random tails, a new identity for every run; False: Renac's demo plant's serials.
    USE_RANDOM_SN_INV = False
    USE_RANDOM_SN_MOD = True

    module_serial = "2200000" + (random_text(HEX_DIGITS, 9) if USE_RANDOM_SN_MOD else "C123C4121")
    return Identity(
        model="N3-HV-10.0",
        inverter_serial="830103323" + (random_text(DIGITS, 7) if USE_RANDOM_SN_INV else "1001064"),
        module_serial=module_serial,
        mac=mac_of(module_serial),
    )


def random_identity() -> Identity:
    """Made up whole, for the tests: serials of the real ones' lengths."""
    module_serial = random_text(HEX_DIGITS, 16)
    return Identity(model=random.choice(MODELS), inverter_serial=random_text(DIGITS, 16), module_serial=module_serial, mac=mac_of(module_serial))


def rated_power_w(model: str) -> float:
    """The model's rated power: N3-HV-6.0 is 6 kW."""
    return float(model.rsplit("-", 1)[1]) * 1000


# The command line's identity.
IDENTITY = demo_identity()
# The firmware versions times 100, made up: master 1.23, slave 1.02, HMI 2.04, the BMS's 0.00 (no battery), protocol 0.39.
FIRMWARE_VERSIONS = {10176: 123, 10177: 102, 10178: 204, 10179: 0, 10180: 0, 10181: 39}
# Made up, no real Renac BMS serial is known: the master's, then slaves 1-8, each in 16 registers from 10032.
BMS_SERIALS = ("MOCK-BMS-MASTER", *(f"MOCK-BMS-SLAVE{slave}" for slave in range(1, 9)))
BMS_SERIAL_ADDRESS = 10032
BMS_SERIAL_WORDS = 16

# The 10 kW model's PV peaks and energy; another model's are in proportion to its rated power.
REFERENCE_POWER_W = 10000
PV1_PEAK_W = 2900
PV2_PEAK_W = 2300
DAY_S = 3600.0 * 24
TICK_S = 1.0
# Between two pieces of one reply, about one connection interval of the module.
PIECE_GAP_S = 0.03
# One piece when the stack does not say the subscriber's MTU.
DEFAULT_PIECE_BYTES = 20
# GattServiceProviderAdvertisementStatus: 2 started, 4 started without all the advertisement data.
ADVERTISING_STARTED = (2, 4)
# Server mode: seconds before the module dials the server again after a drop or a failed dial.
RECONNECT_S = 5.0
# The AT side (PLAN §2.8), on the core's AT_PORT: the greeting that opens a session, the end of every reply, and the
# longest host socket B takes.
AT_GREETING = b"HF-A11ASSISTHREAD"
AT_REPLY_END = "\r\n\r\n"
SOCKB_HOST_MAX = 100

ILLEGAL_DATA_ADDRESS = 2
ILLEGAL_DATA_VALUE = 3

INVERTER_STATE_WAIT = 0
INVERTER_STATE_NORMAL = 2

# The settings the inverter takes writes into.
WRITABLE = range(20000, 21022)
# The clock: year since 2000, month, day, hour, minute, second.
CLOCK_ADDRESSES = range(20001, 20007)
# The demo plant's time zone, UTC+8: the cloud writes its clock in it. The clock starts there and runs on from the
# cloud's last write.
DEMO_UTC_OFFSET_S = 8 * 3600
# The vendor document's defaults, where it gives one: language English, Meter 2 and 3 at addresses 2 and 3.
VENDOR_DEFAULTS = {20007: 0, 20010: 2, 20011: 3}
# The settings and protection blocks the poll groups read, by their first address, made up inside the vendor
# document's scopes, its defaults included. Words past a block's documented end (20110-20113, 23109, 23224-23225,
# 23316) are 0.
MADE_UP_BLOCKS: dict[int, tuple[int, ...]] = {
    # System on; the clock, which the simulation sets; language, grid code, Meter 1-3 and Modbus addresses, password.
    20000: (1,),
    20001: (26, 1, 1, 0, 0, 0),
    20007: (0, 0, 1, 2, 3, 1, 0),
    # DRM0 off, Meter 1 on, remote power control off, ISO, GFCI and anti-islanding on, the other checks off.
    20100: (0, 1, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    # Self use; charge and discharge 25.0 A, minimum SoC 10 % and on grid 10 %, no export limit, power limit 100 %.
    21000: (0,),
    21016: (250, 250, 10, 10, 0, 100),
    # Connect: soft start, 60 s at 10.0 %/min, again 60 s at 10.0 %/min, 195.0-250.0 V, 47.50-50.50 Hz.
    22000: (1, 60, 100, 60, 100, 2500, 1950, 5050, 4750),
    # Voltage protection: levels 1 and 2, 260.0 V and 270.0 V high, 196.0 V and 115.0 V low, 253.0 V over 10 min.
    22100: (3, 2600, 20, 2700, 10, 1960, 150, 1150, 20, 2530, 31),
    # Frequency protection: levels 1 and 2, 51.50 Hz and 52.00 Hz high, 47.50 Hz and 47.00 Hz low.
    22200: (3, 5150, 20, 5200, 10, 4750, 20, 4700, 10, 15),
    # P(f) on; P(u) off; no reactive power control, PF 1.00; ride-through off; ROCOF off, at 1.0 Hz/s.
    23000: (1, 0, 0, 5020, 5, 5010, 4980, 5, 4990, 100, 30),
    23100: (0, 2070, 2200, 2480, 2530, 100, 20, 3, 10, 0),
    23200: (0, 100, 0, 2350, 2200, 100, 20, 95, 100, 0, 0, 0, 0, 10, 20, 5, 92, 0, 98, 0, 102, 0, 108, 0, 0, 0),
    23300: (0, 0, 50, 150, 70, 300, 85, 600, 120, 100, 115, 300, 110, 600, 50, 120, 0),
    23400: (0, 10),
}
# The live registers the simulation leaves alone or starts from: Meter 1 connected; the temperatures at 25.0 degrees.
LIVE_START = {11059: 1, 11016: 250, 11017: 250, 11018: 250, 11019: 250}
# The payload lengths of the module info reply and the inverter serial reply.
MODULE_INFO_BYTES = 150
INVERTER_SERIAL_BYTES = 34

# Energy counters: key -> (total U32 address, today U16 address), kWh / 10.
ENERGY_ADDRESSES = {
    "pv": (14000, 14002),
    "charge": (14003, 14005),
    "discharge": (14006, 14008),
    "grid_export": (14009, 14011),
    "grid_import": (14012, 14014),
    "output": (14015, 14017),
    "input": (14018, 14020),
    "consumption": (14021, 14023),
    "eps": (14024, 14026),
}


def initial_words(identity: Identity = IDENTITY) -> dict[int, int]:
    """Every register the poll groups read, the undocumented group left out, made up: the identity's model and serial,
    the BMS serials, the firmware versions, the settings and protection of MADE_UP_BLOCKS, the live block at rest,
    random energy counters."""
    words: dict[int, int] = {}
    for group in GROUPS:
        if group is UNDOCUMENTED_GROUP:
            continue
        for block_range in group.ranges:
            words.update(dict.fromkeys(range(block_range.address, block_range.address + block_range.count), 0))
    for first, block in MADE_UP_BLOCKS.items():
        words.update({first + offset: word for offset, word in enumerate(block)})
    words.update(VENDOR_DEFAULTS)
    words.update(LIVE_START)
    words.update(FIRMWARE_VERSIONS)
    write_register_text(words, "model", identity.model)
    write_register_text(words, "serial", identity.inverter_serial)
    for index, serial in enumerate(BMS_SERIALS):
        write_text(words, BMS_SERIAL_ADDRESS + index * BMS_SERIAL_WORDS, BMS_SERIAL_WORDS, serial)
    totals = random_energy_totals(rated_power_w(identity.model) / REFERENCE_POWER_W)
    for key, (total, today) in ENERGY_ADDRESSES.items():
        tenths = round(totals[key] * 10)
        words[total] = tenths >> 16
        words[total + 1] = tenths & 0xFFFF
        words[today] = 0
    return words


def random_energy_totals(scale: float) -> dict[str, float]:
    """Lifetime totals in kWh of a plant without a battery that has run for a random number of days, its PV in
    proportion to `scale`, the model's rated power over 10 kW."""
    days = random.uniform(100, 1500)
    pv = days * random.uniform(25, 45) * scale
    output = pv * random.uniform(0.95, 0.98)
    consumption = days * random.uniform(8, 25)
    from_pv = min(consumption * random.uniform(0.35, 0.6), output)
    return {
        "pv": pv,
        "charge": 0.0,
        "discharge": 0.0,
        "grid_export": output - from_pv,
        "grid_import": consumption - from_pv,
        "output": output,
        "input": 0.0,
        "consumption": consumption,
        "eps": 0.0,
    }


def write_register_text(words: dict[int, int], key: str, text: str) -> None:
    """The text into the registers of a text register, two ASCII bytes a register, NUL padded."""
    register = REGISTERS_BY_KEY[key]
    write_text(words, register.address, register.words, text)


def read_register_text(words: Mapping[int, int], key: str) -> str:
    """The text in the registers of a text register, the NUL padding left out."""
    register = REGISTERS_BY_KEY[key]
    data = b"".join(words[register.address + offset].to_bytes(2, "big") for offset in range(register.words))
    return data.rstrip(b"\x00").decode("ascii")


def write_text(words: dict[int, int], address: int, count: int, text: str) -> None:
    """The text into `count` registers from `address`, two ASCII bytes a register, NUL padded."""
    data = text.encode("ascii")
    if len(data) > 2 * count:
        raise ValueError(f"{address}: {text!r} longer than {2 * count} bytes")
    data = data.ljust(2 * count, b"\x00")
    for offset in range(count):
        words[address + offset] = int.from_bytes(data[2 * offset : 2 * offset + 2], "big")


def write_field_text(payload: bytearray, field: tuple[int, int], text: str) -> None:
    """The text into a text field of an AA55 payload, NUL padded to the field's end."""
    start, end = field
    data = text.encode("ascii")
    if len(data) >= end - start:
        raise ValueError(f"{text!r} does not fit {end - start} bytes")
    payload[start:end] = data.ljust(end - start, b"\x00")


def handshake_replies(identity: Identity) -> dict[tuple[int, int], bytes]:
    """The module's replies to the handshake requests, by (command, sub), built from the identity. The keepalive has
    none. The bytes whose meaning is unknown are made up."""
    module_info = bytearray(MODULE_INFO_BYTES)
    module_info[0] = 0x01
    write_field_text(module_info, MODULE_INFO_FIELDS["module_serial"], identity.module_serial)
    write_field_text(module_info, MODULE_INFO_FIELDS["mac"], identity.mac)
    write_field_text(module_info, MODULE_INFO_FIELDS["firmware"], identity.module_firmware)
    write_field_text(module_info, MODULE_INFO_FIELDS["ip"], identity.ip)
    inverter_serial = bytearray(INVERTER_SERIAL_BYTES)
    write_field_text(inverter_serial, INVERTER_SERIAL_FIELD, identity.inverter_serial)
    inverter_serial[INVERTER_SERIAL_FIELD[1] :] = bytes(random.randrange(256) for _ in range(INVERTER_SERIAL_BYTES - INVERTER_SERIAL_FIELD[1]))
    # A guess at its layout: 01, then the module serial's first seven characters with the third replaced by a byte that
    # changes between sessions, 0x25 to 0x28 seen; a random one of them.
    unknown = bytearray(b"\x01" + identity.module_serial[:7].encode("ascii"))
    unknown[3] = random.randint(0x25, 0x28)
    payloads = {MODULE_INFO: module_info, UNKNOWN_15_0B: unknown, INVERTER_SERIAL: inverter_serial}
    return {key: build_aa55_frame(key[0], key[1], bytes(payload), reply=True) for key, payload in payloads.items()}


class SimulatedInverter:
    """The registers of an N3-HV and the life behind them; `advance()` moves them on. Its rated power is its model's."""

    def __init__(self, words: Mapping[int, int], *, day_s: float = DAY_S) -> None:
        self.words = dict(words)
        self.day_s = day_s
        self.rated_power_w = rated_power_w(read_register_text(self.words, "model"))
        self.random = random.Random()
        self.elapsed = 0.08 * day_s
        self.day = int(self.elapsed // day_s)
        self.consumption_w = 600.0
        # The clock shows this computer's time plus this, in UTC fields.
        self.clock_offset_s = float(DEMO_UTC_OFFSET_S)
        self.temperatures = {address: self.i16(address) / 10 for address in (11016, 11017, 11018, 11019)}
        self.energy_total = {key: self.u32(total) / 10 for key, (total, _) in ENERGY_ADDRESSES.items()}
        self.energy_today = {key: self.u16(today) / 10 for key, (_, today) in ENERGY_ADDRESSES.items()}

    @property
    def phase(self) -> float:
        """Where in the day it is, 0 to 1; the sun is up in the first half."""
        return (self.elapsed % self.day_s) / self.day_s

    @property
    def sun(self) -> float:
        return max(0.0, math.sin(2 * math.pi * self.phase))

    def u16(self, address: int) -> int:
        return self.words[address]

    def i16(self, address: int) -> int:
        value = self.words[address]
        return value - 0x10000 if value & 0x8000 else value

    def u32(self, address: int) -> int:
        return (self.words[address] << 16) | self.words[address + 1]

    def i32(self, address: int) -> int:
        value = self.u32(address)
        return value - 0x100000000 if value & 0x80000000 else value

    def set_u16(self, address: int, value: float) -> None:
        self.words[address] = max(0, min(0xFFFF, round(value)))

    def set_i16(self, address: int, value: float) -> None:
        self.words[address] = max(-0x8000, min(0x7FFF, round(value))) & 0xFFFF

    def set_u32(self, address: int, value: float) -> None:
        clipped = max(0, min(0xFFFFFFFF, round(value)))
        self.words[address] = clipped >> 16
        self.words[address + 1] = clipped & 0xFFFF

    def set_i32(self, address: int, value: float) -> None:
        clipped = max(-0x80000000, min(0x7FFFFFFF, round(value))) & 0xFFFFFFFF
        self.words[address] = clipped >> 16
        self.words[address + 1] = clipped & 0xFFFF

    def advance(self, dt_s: float) -> None:
        """Moves the inverter on by `dt_s` seconds."""
        self.elapsed += dt_s
        day = int(self.elapsed // self.day_s)
        if day != self.day:
            self.day = day
            for key in self.energy_today:
                self.energy_today[key] = 0.0

        pv1_w, pv2_w = self._pv()
        pv_w = pv1_w + pv2_w
        output_w = min(pv_w, self.rated_power_w) if pv_w > 30 else 0.0
        self._consumption(dt_s)
        consumption_phases = self._split(self.consumption_w, (0.55, 0.25, 0.20))
        output_phases = self._split(output_w, (1 / 3, 1 / 3, 1 / 3))
        meter_phases = [consumption - output for consumption, output in zip(consumption_phases, output_phases, strict=True)]

        self._grid(output_phases)
        self._powers(consumption_phases, output_phases, meter_phases)
        self._battery(output_w > 0)
        self._temperatures(output_w, dt_s)
        self.set_u16(11057, INVERTER_STATE_NORMAL if output_w > 0 else INVERTER_STATE_WAIT)
        self._energy(dt_s, pv_w, output_w, sum(consumption_phases), sum(meter_phases))
        self._clock()

    def _pv(self) -> tuple[int, int]:
        sun = self.sun
        scale = self.rated_power_w / REFERENCE_POWER_W
        powers = []
        for base, peak, dark_voltage in ((11000, PV1_PEAK_W, 3.4), (11003, PV2_PEAK_W, 2.7)):
            if sun <= 0:
                voltage, current, power = dark_voltage, 0.0, 0.0
            else:
                power = peak * scale * sun * (1 + self.random.gauss(0, 0.02))
                voltage = 190 + 60 * sun + self.random.gauss(0, 2)
                current = power / voltage
            self.set_i16(base, voltage * 10)
            self.set_i16(base + 1, current * 10)
            self.set_i16(base + 2, power)
            powers.append(round(power))
        return powers[0], powers[1]

    def _consumption(self, dt_s: float) -> None:
        self.consumption_w += self.random.gauss(0, 30) * math.sqrt(dt_s)
        if self.random.random() < 0.01 * dt_s:
            self.consumption_w += self.random.choice((-800, 800))
        self.consumption_w = max(150.0, min(2500.0, self.consumption_w))

    def _split(self, total: float, shares: tuple[float, float, float]) -> list[int]:
        """The total over the three phases with a little imbalance; the phases add up to the total exactly."""
        phases = [round(total * share * (1 + self.random.gauss(0, 0.02))) for share in shares[:2]]
        phases.append(round(total) - phases[0] - phases[1])
        return phases

    def _grid(self, output_phases: list[int]) -> None:
        for i in range(3):
            voltage = 230 + self.random.gauss(0, 1.5)
            self.set_u16(11076 + i, voltage * 10)
            self.set_u16(11085 + i, (50 + self.random.gauss(0, 0.01)) * 100)
            self.set_i16(11079 + i, output_phases[i] / voltage * 10)

    def _powers(self, consumption_phases: list[int], output_phases: list[int], meter_phases: list[int]) -> None:
        for i in range(3):
            self.set_i16(11082 + i, output_phases[i])
            self.set_i16(11098 + i, meter_phases[i])
            self.set_i32(11126 + 2 * i, meter_phases[i])
            self.set_i16(11110 + i, consumption_phases[i])
            self.set_i32(11150 + 2 * i, consumption_phases[i])
        self.set_i16(11101, sum(meter_phases))
        self.set_i32(11132, sum(meter_phases))
        self.set_i16(11113, sum(consumption_phases))
        self.set_i32(11156, sum(consumption_phases))

    def _battery(self, running: bool) -> None:
        self.set_i16(11020, 120 if running else 6)
        self.set_i16(11021, 0)
        power = -3 if running else 0
        self.set_i16(11022, power)
        self.set_i32(11124, power)

    def _temperatures(self, output_w: float, dt_s: float) -> None:
        share = output_w / self.rated_power_w
        targets = {11016: 30 + 22 * share, 11017: 24 + 14 * share, 11018: 24 + 12 * share, 11019: 24 + 13 * share}
        step = min(1.0, dt_s / 120)
        for address, target in targets.items():
            self.temperatures[address] += (target - self.temperatures[address]) * step
            self.set_i16(address, self.temperatures[address] * 10)

    def _energy(self, dt_s: float, pv_w: float, output_w: float, consumption_w: float, meter_w: float) -> None:
        # A shortened day counts as a whole one, so the counters move as they would over a real day.
        hours = dt_s * (86400 / self.day_s) / 3600
        added = {
            "pv": pv_w / 1000 * hours,
            "output": output_w / 1000 * hours,
            "consumption": consumption_w / 1000 * hours,
            "grid_import": max(meter_w, 0) / 1000 * hours,
            "grid_export": max(-meter_w, 0) / 1000 * hours,
        }
        for key, kwh in added.items():
            self.energy_total[key] += kwh
            self.energy_today[key] += kwh
        for key, (total, today) in ENERGY_ADDRESSES.items():
            self.set_u32(total, self.energy_total[key] * 10)
            self.set_u16(today, self.energy_today[key] * 10)

    def take_clock(self) -> None:
        """The clock registers were written: the clock runs on from what was written. A date that is not one changes
        nothing."""
        year, month, day, hour, minute, second = (self.words[address] for address in CLOCK_ADDRESSES)
        try:
            written = calendar.timegm((2000 + year, month, day, hour, minute, second, 0, 0, 0))
        except (ValueError, OverflowError):
            return
        self.clock_offset_s = written - time.time()

    def _clock(self) -> None:
        now = time.gmtime(time.time() + self.clock_offset_s)
        for address, value in zip(CLOCK_ADDRESSES, (now.tm_year - 2000, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec), strict=True):
            self.set_u16(address, value)


def exception_reply(function: int, code: int) -> bytes:
    return append_crc(bytes([UNIT_ID, function | EXCEPTION_BIT, code]))


class FakeModule:
    """The module: the bytes it gets (FED5, or socket B in server mode) are requests to the inverter, the replies go back.

    With `answers_handshake` (server mode) the module also answers the AA55 handshake itself, as `identity`; on Bluetooth AA55
    frames are left out.
    """

    def __init__(self, inverter: SimulatedInverter, *, identity: Identity = IDENTITY, answers_handshake: bool = False) -> None:
        self.inverter = inverter
        self.identity = identity
        self.answers_handshake = answers_handshake
        self.handshake_replies = handshake_replies(identity) if answers_handshake else {}
        self.framer = Framer(from_master=True)

    def take_ip(self, ip: str) -> None:
        """The module's own address, as socket B connected from it: in the module info from now on."""
        if ip == self.identity.ip:
            return
        self.identity = replace(self.identity, ip=ip)
        if self.answers_handshake:
            self.handshake_replies = handshake_replies(self.identity)

    def handle(self, data: bytes, now: float, framer: Framer | None = None) -> list[tuple[Frame, bytes | None]]:
        """The requests in the bytes with their replies; None for a request that gets none. `framer` is the path's own
        when the module serves more than one; the module's by default."""
        answered: list[tuple[Frame, bytes | None]] = []
        for frame in (framer or self.framer).feed(data, now):
            if isinstance(frame, ModbusFrame):
                answered.append((frame, self.reply_to(frame)))
            elif self.answers_handshake and not frame.is_reply:
                answered.append((frame, self.handshake_replies.get((frame.command, frame.sub))))
        return answered

    def reply_to(self, request: ModbusFrame) -> bytes | None:
        """The inverter's reply to the request, or None when it stays silent."""
        address = request.address or 0
        count = request.count or 0
        if request.kind is ModbusKind.READ_REQUEST:
            return self._read(address, count)
        if request.kind is ModbusKind.WRITE_SINGLE:
            return self._write(request, [address])
        if request.kind is ModbusKind.WRITE_MULTIPLE_REQUEST:
            if count != len(request.registers()):
                return exception_reply(request.function, ILLEGAL_DATA_VALUE)
            return self._write(request, list(range(address, address + count)))
        return None

    def _read(self, address: int, count: int) -> bytes:
        if not 1 <= count <= MAX_READ_COUNT:
            return exception_reply(READ, ILLEGAL_DATA_VALUE)
        words = [self.inverter.words.get(register) for register in range(address, address + count)]
        if any(word is None for word in words):
            return exception_reply(READ, ILLEGAL_DATA_ADDRESS)
        data = b"".join((word or 0).to_bytes(2, "big") for word in words)
        return append_crc(bytes([UNIT_ID, READ, 2 * count]) + data)

    def _write(self, request: ModbusFrame, addresses: list[int]) -> bytes:
        if not all(address in WRITABLE and address in self.inverter.words for address in addresses):
            return exception_reply(request.function, ILLEGAL_DATA_ADDRESS)
        for address, value in zip(addresses, request.registers(), strict=True):
            self.inverter.words[address] = value
        if any(address in CLOCK_ADDRESSES for address in addresses):
            self.inverter.take_clock()
        if request.kind is ModbusKind.WRITE_SINGLE:
            return request.raw
        return append_crc(request.raw[:6])

    @staticmethod
    def pieces(reply: bytes, piece_bytes: int) -> list[bytes]:
        """The reply cut into pieces of `piece_bytes`; 0 keeps it whole."""
        size = piece_bytes if piece_bytes > 0 else len(reply)
        return [reply[at : at + size] for at in range(0, len(reply), size)]


class BleServer:
    """The GATT server on this computer: writes to FED5 go to the module, its replies out on FED6 as indications."""

    def __init__(self, module: FakeModule, *, fragment: int, name: str) -> None:
        self.module = module
        self.fragment = fragment
        self.name = name
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.data_writer = None
        self.sending: asyncio.Lock | None = None
        self.piece_bytes_unknown_logged = False

    async def start(self) -> None:
        try:
            from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties
        except ImportError:
            raise SystemExit("bless is missing: install requirements_test.txt into the venv") from None
        try:
            from bleak_winrt.windows.storage.streams import DataWriter
        except ImportError:
            try:
                from winrt.windows.storage.streams import DataWriter
            except ImportError:
                DataWriter = None

        self.loop = asyncio.get_running_loop()
        self.data_writer = DataWriter
        self.sending = asyncio.Lock()
        # bless turns its own logger to DEBUG when imported; only -v wants that.
        logging.getLogger("bless").setLevel(logging.DEBUG if LOGGER.isEnabledFor(logging.DEBUG) else logging.WARNING)
        # name_overwrite stays off: on Windows bless would rename this computer's Bluetooth adapter.
        self.server = BlessServer(name=self.name, loop=self.loop)
        self.server.on_read = self.on_read
        self.server.on_write = self.on_write
        self.server.on_subscribe = self.on_subscribe
        self.server.on_unsubscribe = self.on_unsubscribe
        await self.server.add_new_service(SERVICE)
        permissions = GATTAttributePermissions.readable | GATTAttributePermissions.writable
        await self.server.add_new_characteristic(SERVICE, FED5, GATTCharacteristicProperties.write, None, permissions)
        await self.server.add_new_characteristic(SERVICE, FED6, GATTCharacteristicProperties.indicate, None, permissions)
        # bless master indexes its table of subscribed clients before it has filled it, a KeyError on the first change; seeded here.
        tracked = getattr(self.server, "_subscribed_clients", None)
        if isinstance(tracked, dict):
            tracked.setdefault(FED5, set())
            tracked.setdefault(FED6, set())

        # Not bless's start(): it calls the one-argument start_advertising(), which the WinRT projection splits into
        # start_advertising() and start_advertising_with_parameters(); and it blocks the loop until Windows answers.
        providers = self.start_advertising()
        deadline = time.monotonic() + 10
        while not all(provider.advertisement_status in ADVERTISING_STARTED for provider in providers):
            if time.monotonic() > deadline:
                statuses = ", ".join(str(provider.advertisement_status) for provider in providers)
                raise SystemExit(f"advertising did not start within 10 s (status {statuses}); is Bluetooth on here?")
            await asyncio.sleep(0.2)

    def start_advertising(self) -> list:  # noqa: ANN401
        """Starts the advertisement of every service, connectable and discoverable; returns the WinRT service providers."""
        try:
            from winrt.windows.devices.bluetooth.genericattributeprofile import GattServiceProviderAdvertisingParameters
        except ImportError:
            from bleak_winrt.windows.devices.bluetooth.genericattributeprofile import GattServiceProviderAdvertisingParameters

        parameters = GattServiceProviderAdvertisingParameters()
        parameters.is_discoverable = True
        parameters.is_connectable = True
        providers = []
        for service in self.server.services.values():
            provider = service.service_provider
            start = getattr(provider, "start_advertising_with_parameters", None)
            if start is None:
                start = provider.start_advertising
            start(parameters)
            providers.append(provider)
        return providers

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()

    def on_subscribe(self, characteristic, session) -> None:  # noqa: ANN001
        """bless's thread: a central subscribed; its MTU says how much one indication carries."""
        LOGGER.info("%s subscribed to %s, MTU %s", session.central_id, str(characteristic.uuid).lower()[4:8], session.mtu)

    def on_unsubscribe(self, characteristic, session) -> None:  # noqa: ANN001
        LOGGER.info("%s left %s", session.central_id, str(characteristic.uuid).lower()[4:8])

    def on_read(self, characteristic, request):  # noqa: ANN001, ANN201
        return characteristic.value or bytearray()

    def on_write(self, characteristic, value, request) -> None:  # noqa: ANN001
        """bless's thread: the bytes go to the event loop."""
        uuid = str(characteristic.uuid).lower()
        if uuid != FED5:
            LOGGER.warning("write to %s ignored; only FED5 is the inverter's input", uuid[4:8])
            return
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.take, bytes(value))

    def take(self, data: bytes) -> None:
        LOGGER.debug("write FED5 %d B: %s", len(data), data.hex(" "))
        for request, reply in self.module.handle(data, time.monotonic()):
            if reply is None:
                LOGGER.info("%s: no reply", describe_frame(request))
                continue
            LOGGER.info("%s -> %d B", describe_frame(request), len(reply))
            asyncio.ensure_future(self.send(reply))

    async def send(self, reply: bytes) -> None:
        """Sends the reply in the pieces one indication carries, one reply at a time as the UART does."""
        assert self.sending is not None
        async with self.sending:
            size = self.fragment or await self.piece_bytes()
            for index, piece in enumerate(self.module.pieces(reply, size)):
                if index:
                    await asyncio.sleep(PIECE_GAP_S)
                await self.indicate(piece)

    async def piece_bytes(self) -> int:
        """The most bytes one indication carries to every subscriber, MTU - 3; a default when the stack does not say."""
        characteristic = self.server.get_characteristic(FED6)
        deadline = time.monotonic() + 3
        while True:
            clients = list(getattr(characteristic.obj, "subscribed_clients", None) or [])
            if clients:
                return min(client.max_notification_size for client in clients)
            if time.monotonic() > deadline:
                if not self.piece_bytes_unknown_logged:
                    self.piece_bytes_unknown_logged = True
                    LOGGER.warning("no subscriber size known; sending %d B pieces", DEFAULT_PIECE_BYTES)
                return DEFAULT_PIECE_BYTES
            await asyncio.sleep(0.05)

    async def indicate(self, piece: bytes) -> None:
        characteristic = self.server.get_characteristic(FED6)
        LOGGER.debug("indicate FED6 %d B: %s", len(piece), piece.hex(" "))
        notify = getattr(characteristic.obj, "notify_value_async", None)
        if self.data_writer is not None and notify is not None:
            writer = self.data_writer()
            writer.write_bytes(bytearray(piece))
            results = await notify(writer.detach_buffer())
            for result in results:
                if int(result.status) != 0:
                    LOGGER.error("indication of %d B failed: status %d, ATT error %s", len(piece), int(result.status), result.protocol_error)
            return
        characteristic.value = bytearray(piece)
        self.server.update_value(SERVICE, FED6)


class TcpModule:
    """The module's socket B in server mode: dials the server, answers the handshake and Modbus, dials again after a drop.

    It sends nothing until asked. `dial()` moves it to another address at once, as an `AT+SOCKB` set does. The inverter's
    replies also go out on `link` when there is one, as the module's one UART feeds Bluetooth and socket B. The tests
    change `padding`, `split_at` and `drop` (requests left without a reply) and send their own bytes with `send()`.
    """

    def __init__(self, module: FakeModule, host: str, port: int, *, reconnect_s: float = RECONNECT_S) -> None:
        self.module = module
        self.host = host
        self.port = port
        self.reconnect_s = reconnect_s
        self.link: LinkServer | None = None
        self.padding = b""
        self.split_at: int | None = None
        self.drop: set[bytes] = set()
        self.requests: list[Frame] = []
        self.connects = 0
        self.connected_at: float | None = None
        self.first_request_at: float | None = None
        self.local_address: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sending = asyncio.Lock()
        self._dial_now = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def run(self) -> None:
        """Dials the server and answers it; after a drop or a failed dial waits `reconnect_s` and dials again, at once
        when `dial()` gave a new address. Runs until cancelled."""
        while True:
            self._dial_now.clear()
            host, port = self.host, self.port
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError as error:
                LOGGER.info("cannot reach %s:%d: %s; dialling again in %g s", host, port, error, self.reconnect_s)
            else:
                if self._dial_now.is_set():
                    # Moved while this dial was under way.
                    writer.close()
                    continue
                reason = await self._serve(reader, writer)
                if not self._dial_now.is_set():
                    LOGGER.info("connection to %s:%d lost: %s; dialling again in %g s", host, port, reason, self.reconnect_s)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._dial_now.wait(), self.reconnect_s)

    def dial(self, host: str, port: int) -> None:
        """Socket B dials `host:port` from now on, at once, dropping the connection it has: an `AT+SOCKB` set."""
        LOGGER.info("socket B moves from %s:%d to %s:%d", self.host, self.port, host, port)
        self.host = host
        self.port = port
        self._dial_now.set()
        self.drop_connection()

    async def send_inverter_reply(self, reply: bytes) -> None:
        """An inverter reply another path asked for, out on socket B too; nothing when it is not connected."""
        writer = self._writer
        if writer is None or writer.is_closing():
            return
        with contextlib.suppress(OSError, ConnectionError):
            await self._send_reply(writer, reply)

    async def send(self, data: bytes) -> None:
        """Sends the bytes as they are: frames nobody asked for, garbage."""
        writer = self._writer
        if writer is None:
            raise ConnectionError("not connected")
        writer.write(data)
        await writer.drain()

    def drop_connection(self) -> None:
        """Closes the connection as a lost link would; `run()` dials again after `reconnect_s`."""
        if self._writer is not None:
            self._writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
        """Answers the server's requests until the connection ends; the reason it ended."""
        self._writer = writer
        self.connects += 1
        self.connected_at = time.monotonic()
        self.first_request_at = None
        address = writer.get_extra_info("sockname")
        self.local_address = f"{address[0]}:{address[1]}"
        self.module.take_ip(address[0])
        self.module.framer.drop_partial()
        LOGGER.info("connected to %s:%d from %s", self.host, self.port, self.local_address)
        reason = "closed by the server"
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                LOGGER.debug("received %d B: %s", len(data), data.hex(" "))
                for request, reply in self.module.handle(data, time.monotonic()):
                    self.requests.append(request)
                    if self.first_request_at is None:
                        self.first_request_at = time.monotonic()
                    if reply is None or request.raw in self.drop:
                        LOGGER.info("%s: no reply", describe_frame(request))
                        continue
                    LOGGER.info("%s -> %d B", describe_frame(request), len(reply))
                    await self._send_reply(writer, reply)
                    if self.link is not None and isinstance(request, ModbusFrame):
                        await self.link.send_inverter_reply(reply)
        except (OSError, ConnectionError) as error:
            reason = str(error)
        finally:
            self._writer = None
            writer.close()
        return reason

    async def _send_reply(self, writer: asyncio.StreamWriter, reply: bytes) -> None:
        """One reply at a time, as the UART gives them."""
        async with self._sending:
            data = self.padding + reply
            LOGGER.debug("send %d B: %s", len(data), data.hex(" "))
            if self.split_at is not None and len(data) > self.split_at:
                writer.write(data[: self.split_at])
                await writer.drain()
                await asyncio.sleep(0.005)
                data = data[self.split_at :]
            writer.write(data)
            await writer.drain()


class LinkServer:
    """The module's Bluetooth side as a TCP port (--link), for the mock relay: what a connection sends is what FED5
    writes would carry; the inverter's replies go to every connection as FED6 indications would, in pieces of
    `fragment` bytes (0: whole). AA55 frames get nothing, as on Bluetooth. The replies also go out on `socket_b` when
    there is one, as the module's one UART feeds both."""

    def __init__(self, module: FakeModule, *, fragment: int = 0) -> None:
        self.module = module
        self.fragment = fragment
        self.socket_b: TcpModule | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self._sending = asyncio.Lock()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Listens; returns the port."""
        self._server = await asyncio.start_server(self._serve, host, port)
        return self.port

    async def stop(self) -> None:
        for writer in list(self.writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def send_inverter_reply(self, reply: bytes) -> None:
        """The reply to every connection, in pieces, one reply at a time as the UART gives them."""
        async with self._sending:
            for index, piece in enumerate(self.module.pieces(reply, self.fragment)):
                if index:
                    await asyncio.sleep(PIECE_GAP_S)
                LOGGER.debug("link send %d B: %s", len(piece), piece.hex(" "))
                for writer in list(self.writers):
                    with contextlib.suppress(OSError, ConnectionError):
                        writer.write(piece)
                        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        address = writer.get_extra_info("peername")
        peer = f"{address[0]}:{address[1]}"
        framer = Framer(from_master=True)
        self.writers.append(writer)
        LOGGER.info("link: %s connected", peer)
        try:
            while data := await reader.read(4096):
                LOGGER.debug("link received %d B: %s", len(data), data.hex(" "))
                for request, reply in self.module.handle(data, time.monotonic(), framer):
                    if not isinstance(request, ModbusFrame):
                        continue
                    if reply is None:
                        LOGGER.info("link: %s: no reply", describe_frame(request))
                        continue
                    LOGGER.info("link: %s -> %d B", describe_frame(request), len(reply))
                    await self.send_inverter_reply(reply)
                    if self.socket_b is not None:
                        await self.socket_b.send_inverter_reply(reply)
        except (OSError, ConnectionError):
            pass
        finally:
            self.writers.remove(writer)
            writer.close()
            LOGGER.info("link: %s gone", peer)


def at_reply(text: str) -> bytes:
    return (text + AT_REPLY_END).encode("ascii")


def sockb_target(parameters: str) -> tuple[str, int] | None:
    """The host and port of `AT+SOCKB=TCP,<port>,<host>`, from its parameters; None when they are not that."""
    protocol, _, rest = parameters.partition(",")
    port, _, host = rest.partition(",")
    if protocol != "TCP" or not port.isdigit() or not 0 < int(port) < 65536 or not 0 < len(host) <= SOCKB_HOST_MAX or "," in host:
        return None
    return host, int(port)


def own_address_toward(peer: str, port: int) -> str:
    """This host's address on the way to `peer`: a UDP socket connected to it, which sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((peer, port))
        return probe.getsockname()[0]


class AtServer(asyncio.DatagramProtocol):
    """The module's AT side on UDP (PLAN §2.8), a broadcast included. `HF-A11ASSISTHREAD` gets `ip,mac,hostname`, the
    IP its own address toward the sender; `+ok` after it opens AT mode for that sender. In AT mode, commands end with
    CR: `AT+SOCKB` gets `+ok=TCP,<port>,<host>`, where socket B dials; `AT+SOCKB=TCP,<port>,<host>` gets `+ok` and moves
    socket B there at once (`+ERR=-4` for other parameters); `AT+Q` ends AT mode and gets nothing; any other `AT+`
    command gets `+ERR=-2`, anything else `+ERR=-1`. Replies end with CR LF CR LF. A sender not in AT mode, and a
    command without CR, get nothing."""

    def __init__(self, socket_b: TcpModule, identity: Identity) -> None:
        self.socket_b = socket_b
        self.identity = identity
        self.greeted: set[tuple[str, int]] = set()
        self.in_at_mode: set[tuple[str, int]] = set()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        reply = self.answer(data, address)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, address)

    def answer(self, data: bytes, address: tuple[str, int]) -> bytes | None:
        """The reply to one datagram from `address`; None when it gets none."""
        sender = f"{address[0]}:{address[1]}"
        if data == AT_GREETING:
            self.greeted.add(address)
            LOGGER.info("AT: greeting from %s", sender)
            ip = own_address_toward(*address)
            return f"{ip},{self.identity.mac.replace(':', '')},{DEFAULT_NAME}".encode("ascii")
        if data == b"+ok":
            if address in self.greeted:
                self.greeted.discard(address)
                self.in_at_mode.add(address)
                LOGGER.info("AT: %s in AT mode", sender)
            return None
        if address not in self.in_at_mode or not data.endswith(b"\r"):
            LOGGER.info("AT: %r from %s ignored: not in AT mode, or no CR", data, sender)
            return None
        command = data[:-1].decode("ascii", "replace")
        LOGGER.info("AT: %s from %s", command, sender)
        if command == "AT+Q":
            self.in_at_mode.discard(address)
            return None
        if command == "AT+SOCKB":
            return at_reply(f"+ok=TCP,{self.socket_b.port},{self.socket_b.host}")
        if command.startswith("AT+SOCKB="):
            target = sockb_target(command.removeprefix("AT+SOCKB="))
            if target is None:
                return at_reply("+ERR=-4")
            self.socket_b.dial(*target)
            return at_reply("+ok")
        return at_reply("+ERR=-2" if command.startswith("AT+") else "+ERR=-1")


async def tick(inverter: SimulatedInverter) -> None:
    while True:
        await asyncio.sleep(TICK_S)
        inverter.advance(TICK_S)


def host_and_port(text: str) -> tuple[str, int]:
    """HOST:PORT as a pair, for argparse."""
    host, separator, port = text.rpartition(":")
    if not separator or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"{text!r} is not HOST:PORT")
    return host, int(port)


def inverter_serial(text: str) -> str:
    """An inverter serial as the real ones are, 16 ASCII letters or digits, for argparse."""
    if len(text) != 16 or not (text.isascii() and text.isalnum()):
        raise argparse.ArgumentTypeError(f"{text!r} is not 16 letters or digits")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A fake HF-LPT270 on this computer's Bluetooth with a simulated N3-HV behind it. Ctrl+C stops it.")
    parser.add_argument(
        "--tcp",
        type=host_and_port,
        metavar="HOST:PORT",
        help="the module of server mode instead: socket B dials the server at HOST:PORT, the AT side answers on UDP; no Bluetooth",
    )
    parser.add_argument("--link", type=int, metavar="PORT", help="listen on TCP PORT in place of Bluetooth, for the mock relay's --module; no Bluetooth")
    parser.add_argument("--at-port", type=int, default=AT_PORT, metavar="PORT", help=f"with --tcp: the AT side's UDP port (default {AT_PORT})")
    parser.add_argument("--bind", default="127.0.0.1", help="the address the link and the AT side listen on (default 127.0.0.1)")
    parser.add_argument(
        "--fragment",
        type=int,
        default=0,
        metavar="BYTES",
        help="bytes per indication, or per piece on the link; 0 uses the subscriber's MTU - 3, whole on the link (default)",
    )
    parser.add_argument("--model", choices=MODELS, help=f"the inverter's model, its rated power with it (default {IDENTITY.model})")
    parser.add_argument("--serial", type=inverter_serial, help="the inverter's serial, 16 letters or digits (default the demo plant's)")
    parser.add_argument("--day", type=float, default=DAY_S, help=f"seconds of one simulated day (default {DAY_S:g})")
    parser.add_argument(
        "--name", default=DEFAULT_NAME, help=f"the advertised name on Linux and macOS (default {DEFAULT_NAME}); Windows advertises the computer's own"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="hexdump every write and indication")
    return parser.parse_args(argv)


def command_line_identity(args: argparse.Namespace) -> Identity:
    """IDENTITY with the inverter's model and serial the command line gives."""
    return replace(IDENTITY, model=args.model or IDENTITY.model, inverter_serial=args.serial or IDENTITY.inverter_serial)


async def run(args: argparse.Namespace) -> None:
    identity = command_line_identity(args)
    inverter = SimulatedInverter(initial_words(identity), day_s=args.day)
    inverter.advance(0)
    if args.tcp is not None or args.link is not None:
        await run_without_bluetooth(inverter, identity, args)
        return
    server = BleServer(FakeModule(inverter, identity=identity), fragment=args.fragment, name=args.name)
    await server.start()
    LOGGER.info(
        "advertising service FEE7 with FED5 write and FED6 indicate; inverter %s %s; %d registers simulated, a day of %g s",
        identity.model,
        identity.inverter_serial,
        len(inverter.words),
        args.day,
    )
    ticking = asyncio.ensure_future(tick(inverter))
    try:
        await asyncio.Event().wait()
    finally:
        ticking.cancel()
        await asyncio.gather(ticking, return_exceptions=True)
        await server.stop()


async def run_without_bluetooth(inverter: SimulatedInverter, identity: Identity, args: argparse.Namespace) -> None:
    """One module on the paths asked for: socket B dialling --tcp with the AT side, the link on --link, or both."""
    module = FakeModule(inverter, identity=identity, answers_handshake=args.tcp is not None)
    tasks = [asyncio.ensure_future(tick(inverter))]
    link: LinkServer | None = None
    at_transport: asyncio.BaseTransport | None = None
    try:
        if args.link is not None:
            link = LinkServer(module, fragment=args.fragment)
            await link.start(args.bind, args.link)
            LOGGER.info("the link listens on %s:%d in place of Bluetooth", args.bind, args.link)
        if args.tcp is not None:
            socket_b = TcpModule(module, *args.tcp)
            socket_b.link = link
            if link is not None:
                link.socket_b = socket_b
            loop = asyncio.get_running_loop()
            at_transport, _ = await loop.create_datagram_endpoint(lambda: AtServer(socket_b, identity), local_addr=(args.bind, args.at_port))
            LOGGER.info("socket B dials %s:%d; the AT side answers on UDP %s:%d", *args.tcp, args.bind, args.at_port)
            tasks.append(asyncio.ensure_future(socket_b.run()))
        LOGGER.info(
            "inverter %s %s, module %s, MAC %s; %d registers simulated, a day of %g s",
            identity.model,
            identity.inverter_serial,
            identity.module_serial,
            identity.mac,
            len(inverter.words),
            inverter.day_s,
        )
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if at_transport is not None:
            at_transport.close()
        if link is not None:
            await link.stop()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
