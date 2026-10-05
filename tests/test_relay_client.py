"""The relay client script as a subprocess against the mock relay."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest

from renac_protocol.frames import build_read_request
from renac_protocol.register_table import ALL_REGISTERS, PHASES, POWER_32BIT
from renac_protocol.registers import Reading, Register
from tests.mocks.mock_relay import ILLEGAL_ADDRESS, MockRelay

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "relay_client.py"


def reading_of(words: Mapping[int, int], key: str, registers: tuple[Register, ...] = ALL_REGISTERS) -> Reading:
    """What the register under the key decodes to from the words."""
    register = next(register for register in registers if register.key == key)
    return register.decode(tuple(words[address] for address in range(register.address, register.end)))


def phase_row(words: Mapping[int, int], key: str, unit: str, registers: tuple[Register, ...] = ALL_REGISTERS) -> str:
    """The tool's row of a value's three phases from its numbers on: `L1/L2/L3 unit   reg ADDRESS  words / words / words`."""
    readings = [reading_of(words, f"{key}_{phase}", registers) for phase in PHASES]
    numbers = "/".join(reading.text.removesuffix(f" {unit}") for reading in readings)
    return f"{numbers} {unit}   reg {readings[0].register.address}  {' / '.join(reading.words_hex for reading in readings)}"


@pytest.fixture
async def relay(inverter_words: dict[int, int]) -> AsyncIterator[MockRelay]:
    mock = MockRelay(inverter_words, latency_s=0.005)
    await mock.start()
    yield mock
    await mock.stop()


async def run_tool(*arguments: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(TOOL), *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=REPO
    )
    out, err = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def test_tables_of_every_group(relay: MockRelay) -> None:
    code, out, err = await run_tool("127.0.0.1", str(relay.port), "--all", "--backoff", "0")
    assert code == 0, err
    words = relay.words
    for key in ("serial", "pv1_power", "pv_energy", "voltage_high_1", "register_50029"):
        reading = reading_of(words, key)
        assert f"{reading.text}   reg {reading.register.address}" in out, key
    assert phase_row(words, "grid_voltage", "V") in out
    assert "FAILED" not in out and "cycle 1" in out


async def test_full_shows_the_registers_that_are_off_by_default(relay: MockRelay) -> None:
    code, out, _ = await run_tool("127.0.0.1", str(relay.port), "--groups", "live", "--full", "--backoff", "0")
    assert code == 0
    assert "BMS max cell voltage" in out and re.search(r"Meter 2 power L1/L2/L3 +0/0/0 W   reg 11102", out)


async def test_use_32bit_adds_the_power32_group_whose_failure_leaves_the_16bit_values(relay: MockRelay) -> None:
    words = relay.words
    total_32bit, total_16bit = reading_of(words, "consumption_power", POWER_32BIT), reading_of(words, "consumption_power")
    code, out, _ = await run_tool("127.0.0.1", str(relay.port), "--groups", "live", "--use-32bit", "--backoff", "0")
    assert code == 0
    assert f"{total_32bit.text}   reg {total_32bit.register.address}" in out and "FAILED" not in out

    relay.replies[build_read_request(11124, 34)] = ILLEGAL_ADDRESS
    code, out, _ = await run_tool("127.0.0.1", str(relay.port), "--groups", "live", "--use-32bit", "--backoff", "0")
    assert code == 1
    assert "32-bit power (power32)  FAILED" in out
    assert f"{total_16bit.text}   reg {total_16bit.register.address}" in out


async def test_period_repeats_until_stopped_and_live_keeps_the_connection(relay: MockRelay) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(TOOL),
        "127.0.0.1",
        str(relay.port),
        "--groups",
        "energy",
        "--period",
        "0.2",
        "--live",
        "--backoff",
        "0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=REPO,
    )
    await asyncio.sleep(1.5)
    process.kill()
    out, _ = await process.communicate()
    text = out.decode("utf-8", "replace")
    assert "cycle 1" in text and "cycle 3" in text
    assert "every 0.2 s, Ctrl+C stops" in text
    assert relay.accepted == 1
    assert len(relay.requests) >= 3


async def test_json_output(relay: MockRelay) -> None:
    code, out, err = await run_tool("127.0.0.1", str(relay.port), "--groups", "live,energy", "--json", "--backoff", "0")
    assert code == 0, err
    data = json.loads(out)
    assert set(data["groups"]) == {"live", "energy"}
    assert data["groups"]["live"]["values"]["pv1_power"]["value"] == reading_of(relay.words, "pv1_power").value
    assert data["groups"]["energy"]["values"]["pv_energy"]["text"] == reading_of(relay.words, "pv_energy").text
    assert data["connection"]["requests"] == len(relay.requests)
    assert data["cycle"] == 1


async def test_raw_shows_the_bytes_and_the_frames(relay: MockRelay) -> None:
    code, out, _ = await run_tool("127.0.0.1", str(relay.port), "--groups", "identity", "--raw", "--backoff", "0")
    assert code == 0
    assert "->   8 B  01 03 27 10 00 20 4f 63" in out
    assert "fn3 reply 32 regs, 69 B" in out


async def test_a_refused_group_gives_exit_code_1(relay: MockRelay) -> None:
    relay.replies[build_read_request(14000, 27)] = ILLEGAL_ADDRESS
    code, out, _ = await run_tool("127.0.0.1", str(relay.port), "--groups", "energy", "--backoff", "0")
    assert code == 1
    assert "Energy (energy)  FAILED" in out


async def test_no_relay_gives_exit_code_2() -> None:
    code, out, _ = await run_tool("127.0.0.1", "1", "--groups", "identity", "--timeout", "0.5")
    assert code == 2
    assert "FAILED" in out


async def test_unknown_group_is_refused() -> None:
    code, _, err = await run_tool("127.0.0.1", "1", "--groups", "nothing")
    assert code != 0
    assert "unknown group" in err
