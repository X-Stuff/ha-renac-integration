"""What the core's tests share: waiting for a condition, a free port, the fake module dialling a port, what a fake
module or the mock relay was asked, a poller built from settings with the mocks around it."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from typing import Any

from renac_protocol.cloud_session import CloudAddress, CloudSettings
from renac_protocol.frames import ModbusFrame, ModbusKind
from renac_protocol.inverter import Inverter
from renac_protocol.poller import ClientSettings, Poller, ServerSettings
from tests.mocks.mock_cloud import MockCloud
from tests.mocks.mock_hf_lpt270 import FakeModule, Identity, SimulatedInverter, TcpModule, initial_words, random_identity
from tests.mocks.mock_relay import MockRelay

# The poller's settle time in the tests.
SETTLE_S = 0.05


async def wait_until(condition: Callable[[], bool], timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Modules:
    """Fake modules dialling ports on 127.0.0.1; every one stops at `stop()`."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def dial(self, port: int, identity: Identity | None = None, *, reconnect_s: float = 0.1) -> TcpModule:
        """A fake module, with a random identity unless given, dialling 127.0.0.1:`port`."""
        identity = identity or random_identity()
        fake = FakeModule(SimulatedInverter(initial_words(identity)), identity=identity, answers_handshake=True)
        module = TcpModule(fake, "127.0.0.1", port, reconnect_s=reconnect_s)
        self.tasks.append(asyncio.create_task(module.run()))
        return module

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def modbus_reads(module: TcpModule) -> list[tuple[int | None, int | None]]:
    """The (address, count) of every read the fake module was sent, in order."""
    return [(frame.address, frame.count) for frame in module.requests if isinstance(frame, ModbusFrame) and frame.kind is ModbusKind.READ_REQUEST]


def relay_reads(relay: MockRelay) -> list[tuple[int | None, int | None]]:
    """The (address, count) of every read the mock relay was sent, in order."""
    return [(frame.address, frame.count) for frame in relay.requests]


class PollerSetup:
    """A started poller built from settings, the fake modules dialling it, the mock clouds and relays; everything stops
    at `stop()`."""

    def __init__(self) -> None:
        self.poller: Poller | None = None
        self.modules = Modules()
        self.clouds: list[MockCloud] = []
        self.relays: list[MockRelay] = []

    async def start_cloud(self, port: int = 0, **options: Any) -> MockCloud:
        cloud = MockCloud(**options)
        await cloud.start(port)
        self.clouds.append(cloud)
        return cloud

    async def start_relay(self, identity: Identity, **options: Any) -> MockRelay:
        """The mock relay answering as an inverter of `identity`."""
        relay = MockRelay(initial_words(identity), latency_s=options.pop("latency_s", 0.005), **options)
        await relay.start()
        self.relays.append(relay)
        return relay

    async def start_poller(
        self,
        inverter: Inverter | None = None,
        *,
        server: bool = True,
        client: int | None = None,
        cloud: CloudAddress | None = None,
        request_gap_s: float = 0.0,
        cloud_options: dict[str, Any] | None = None,
        **options: Any,
    ) -> Poller:
        """A poller with server mode on a free port unless `server` is False, client mode to 127.0.0.1:`client`, the
        cloud session with `cloud_options` when `cloud` is given; `options` go to the poller."""
        server_settings = ServerSettings("127.0.0.1", 0, reply_timeout_s=0.3, request_gap_s=request_gap_s) if server else None
        client_settings = None
        if client is not None:
            client_settings = ClientSettings("127.0.0.1", client, reply_timeout_s=0.3, request_gap_s=request_gap_s, quiet_time_s=0.0)
        cloud_settings = CloudSettings(cloud, **(cloud_options or {})) if cloud is not None else None
        settings = {"settle_s": SETTLE_S} | options
        self.poller = Poller(inverter if inverter is not None else Inverter(), server=server_settings, client=client_settings, cloud=cloud_settings, **settings)
        await self.poller.start()
        return self.poller

    def start_module(self, identity: Identity | None = None, *, reconnect_s: float = 0.1) -> TcpModule:
        """A fake module, with a random identity unless given, dialling the poller's port."""
        assert self.poller is not None and self.poller.listener is not None
        return self.modules.dial(self.poller.listener.port, identity, reconnect_s=reconnect_s)

    async def stop(self) -> None:
        await self.modules.stop()
        if self.poller is not None:
            await self.poller.stop()
        for cloud in self.clouds:
            await cloud.stop()
        for relay in self.relays:
            await relay.stop()
