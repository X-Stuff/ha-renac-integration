"""The cloud relay script as a subprocess, with the fake module of server mode dialling it and the mock cloud, both on 127.0.0.1.
The proxy is not tested."""

from __future__ import annotations

import asyncio
import re
import socket
import sys
import time
from pathlib import Path

import pytest

from renac_protocol.frames import AA55Frame, ModbusFrame
from tests.mocks.mock_cloud import MockCloud
from tests.mocks.mock_hf_lpt270 import FakeModule, SimulatedInverter, TcpModule, initial_words, random_identity

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "cloud_relay.py"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def read_until(stream: asyncio.StreamReader, text: str, timeout_s: float) -> str:
    """The stream's lines until one holds the text; everything read when the time is up."""
    lines: list[str] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(stream.readline(), deadline - time.monotonic())
        except TimeoutError:
            break
        if not line:
            break
        lines.append(line.decode("utf-8", "replace"))
        if text in lines[-1]:
            break
    return "".join(lines)


async def run_tool(*arguments: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(TOOL), *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=REPO
    )
    out, err = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def test_the_console_shows_the_session_the_identity_the_cloud_and_the_values(tmp_path: Path) -> None:
    port = free_port()
    log = tmp_path / "relay.log"
    cloud = MockCloud()
    cloud_port = await cloud.start()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(TOOL),
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--cloud-ip",
        f"127.0.0.1:{cloud_port}",
        "--log",
        str(log),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=REPO,
    )
    identity = random_identity()
    fake = FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True)
    module = TcpModule(fake, "127.0.0.1", port, reconnect_s=0.2)
    task = asyncio.create_task(module.run())
    try:
        assert process.stdout is not None
        out = await read_until(process.stdout, "Energy (energy)", 20)
        await asyncio.wait_for(cloud.session_done.wait(), 20)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        process.kill()
        await process.wait()
        await cloud.stop()

    assert f"cloud relay on 127.0.0.1:{port}" in out
    assert "waiting for the module on" in out
    assert "connected from 127.0.0.1:" in out
    described = f"module serial {identity.module_serial}, MAC {identity.mac}, firmware {identity.module_firmware}, IP 127.0.0.1"
    assert f"{described}; inverter serial {identity.inverter_serial}" in out
    assert identity.model in out and "reg 10016" in out
    assert f"127.0.0.1:{cloud_port} through an IP address, connected" in out
    assert re.search(r"connects 1, frames in [1-9]\d*, bytes in [1-9]\d*, out [1-9]\d*", out)
    assert re.search(r"reads answered from the state \d+, requests forwarded [1-9]\d*, writes blocked \d, bad-CRC frames dropped \d", out)
    assert re.search(r"Live \(live\)  \d+ ms, read at \d\d:\d\d:\d\d", out)
    assert "Energy (energy)" in out
    assert cloud.exchanges

    text = log.read_text(encoding="utf-8")
    assert "cloud_relay: started: --bind 127.0.0.1" in text
    assert "module connected from 127.0.0.1:" in text
    assert f"cloud connected at 127.0.0.1:{cloud_port}" in text
    assert "from cloud   11 B  aa 55 01 00 00 fe 15 00 00 02 13  AA55 0x15/0x00 request" in text
    assert "to module    11 B  aa 55 01 00 00 fe 15 00 00 02 13  AA55 0x15/0x00 request" in text
    assert "to cloud    161 B  aa 55 01 00 00 fe 15 80 96" in text
    assert "fn3 read 10000 x32" in text and "fn3 reply 32 regs, 69 B" in text
    assert "its writes outside the clock blocked, its frames with a bad CRC dropped" in text
    assert "cloud's clock write 20001 x6 = 26, 1, 2, 3, 4, 5: to the inverter" in text
    assert "WARNING renac_protocol.cloud_session: cloud's write 20007 x1 = 1 is not the clock's: blocked" in text
    assert "cloud's frame with a bad CRC dropped: 01 03 2b 71 00 03 9d f4" in text
    # The poller's identity read, the serial learned for this run.
    assert f"inverter serial {identity.inverter_serial} learned" in text


async def test_without_the_handshake_the_groups_come_first_and_a_lost_session_is_shown(tmp_path: Path) -> None:
    port = free_port()
    log = tmp_path / "relay.log"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(TOOL),
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-handshake",
        "--log",
        str(log),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=REPO,
    )
    identity = random_identity()
    fake = FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True)
    # Dials again every 2 s until the tool listens.
    module = TcpModule(fake, "127.0.0.1", port, reconnect_s=2.0)
    task = asyncio.create_task(module.run())
    try:
        assert process.stdout is not None
        out = await read_until(process.stdout, "Energy (energy)", 20)
        module.drop_connection()
        out += await read_until(process.stdout, "the last session ended", 10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        process.kill()
        await process.wait()

    assert "no handshake sent (--no-handshake)" in out
    assert not any(isinstance(frame, AA55Frame) for frame in module.requests)
    # The identity read, then the identity group.
    reads = [(frame.address, frame.count) for frame in module.requests if isinstance(frame, ModbusFrame)]
    assert reads[:3] == [(10000, 32), (10000, 32), (10176, 6)]
    assert "the last session ended" in out
    text = log.read_text(encoding="utf-8")
    assert "no cloud; handshake off" in text and "sending the handshake" not in text


async def test_a_busy_port_gives_exit_code_2(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, _, err = await run_tool("--bind", "127.0.0.1", "--port", str(port), "--log", str(tmp_path / "relay.log"))
    assert code == 2
    assert "cannot wait for the module" in err


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("--dns", "dns.example"), "neither an IP address"),
        (("--cloud-ip", "cloud.example"), "is not ADDRESS[:PORT]"),
        (("--cloud", "cloud.example:99999"), "is not NAME[:PORT]"),
        (("--port", "0"), "is not a port"),
    ],
)
async def test_bad_options_are_refused(tmp_path: Path, arguments: tuple[str, ...], error: str) -> None:
    code, _, err = await run_tool(*arguments, "--bind", "127.0.0.1", "--log", str(tmp_path / "relay.log"))
    assert code != 0
    assert error in err
