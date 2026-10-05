"""The mocks of the compose test instance (tests/ha/compose.yaml) as processes on 127.0.0.1, wired the same way: the fake
module with socket B, its AT side and its link; the mock relay bridging to the link; the mock cloud."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

from tests.mocks.mock_hf_lpt270 import AT_GREETING, random_identity

REPO = Path(__file__).resolve().parents[1]
RELAY_CLIENT = REPO / "tools" / "relay_client.py"


def free_port(kind: socket.SocketKind = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Process:
    """A mock run as `python -m`, its output kept."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.output = ""
        self._reading = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, module: str, *arguments: str) -> Process:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=REPO,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        return cls(process)

    async def _read(self) -> None:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            self.output += line.decode("utf-8", "replace")

    async def wait_for(self, text: str, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while text not in self.output:
            if time.monotonic() > deadline:
                raise AssertionError(f"{text!r} not in the output:\n{self.output}")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
        await self.process.wait()
        self._reading.cancel()
        await asyncio.gather(self._reading, return_exceptions=True)


class AtClient(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        self.replies.put_nowait(data)


async def test_the_compose_mocks_work_together() -> None:
    link_port, relay_port, cloud_port, nobody_port = free_port(), free_port(), free_port(), free_port()
    at_port = free_port(socket.SOCK_DGRAM)
    processes: list[Process] = []
    at_transport = None
    try:
        # Socket B starts on a port nobody listens on; the AT set below moves it to the cloud. The inverter as the
        # compose file gives its second one.
        serial = random_identity().inverter_serial
        module = await Process.start(
            "tests.mocks.mock_hf_lpt270",
            *("--tcp", f"127.0.0.1:{nobody_port}", "--link", str(link_port), "--at-port", str(at_port), "--model", "N3-HV-6.0", "--serial", serial),
        )
        processes.append(module)
        relay = await Process.start("tests.mocks.mock_relay", "--port", str(relay_port), "--module", f"127.0.0.1:{link_port}", "-v")
        processes.append(relay)
        await relay.wait_for(f"module link 127.0.0.1:{link_port} up")

        # Client mode: the relay's client reads the module through the link.
        client = await asyncio.create_subprocess_exec(
            sys.executable, str(RELAY_CLIENT), "127.0.0.1", str(relay_port), "--groups", "identity,live", stdout=asyncio.subprocess.PIPE, cwd=REPO
        )
        out, _ = await asyncio.wait_for(client.communicate(), 30)
        assert client.returncode == 0, out.decode("utf-8", "replace")
        assert "N3-HV-6.0" in out.decode("utf-8", "replace") and serial in out.decode("utf-8", "replace")
        await module.wait_for("link: fn3 read 10000 x32 -> 69 B")

        cloud = await Process.start("tests.mocks.mock_cloud", "--port", str(cloud_port))
        processes.append(cloud)
        await cloud.wait_for(f"mock cloud on 127.0.0.1:{cloud_port}")

        # The AT side moves socket B to the cloud at once; the cloud's handshake is answered.
        loop = asyncio.get_running_loop()
        at_transport, at = await loop.create_datagram_endpoint(AtClient, remote_addr=("127.0.0.1", at_port))
        at_transport.sendto(AT_GREETING)
        assert (await asyncio.wait_for(at.replies.get(), 2)).startswith(b"127.0.0.1,")
        at_transport.sendto(b"+ok")
        at_transport.sendto(f"AT+SOCKB=TCP,{cloud_port},127.0.0.1\r".encode())
        assert await asyncio.wait_for(at.replies.get(), 2) == b"+ok\r\n\r\n"
        await cloud.wait_for("AA55 0x15/0x00 request, 0 B payload -> AA55 0x15/0x00 reply", timeout_s=3.0)
        await module.wait_for(f"socket B moves from 127.0.0.1:{nobody_port} to 127.0.0.1:{cloud_port}")
    finally:
        if at_transport is not None:
            at_transport.close()
        for process in processes:
            await process.stop()
