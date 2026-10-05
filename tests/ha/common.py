"""What the glue's tests share: entries with either way in or both, setting one up, an entity's state, a free port,
waiting for a condition."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.renac_local.const import CONF_BIND, CONF_CLIENT, CONF_ENABLED, CONF_SERVER, DOMAIN
from custom_components.renac_local.inverter_device import InverterDevice
from custom_components.renac_local.renac_protocol import GROUPS_BY_KEY

# The groups enabled by default.
DEFAULT_GROUPS = ("identity", "settings", "live", "energy")


def groups_shown(device: InverterDevice) -> bool:
    """True once the entities have a reading of every group enabled by default: each was read and the update pushed."""
    return all(any(register.key in device.data for register in GROUPS_BY_KEY[key].registers) for key in DEFAULT_GROUPS)


def entry(
    *,
    server: int | None = None,
    client: int | None = None,
    title: str = "inverter",
    cloud: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    **data: Any,
) -> MockConfigEntry:
    """An entry with server mode listening on 127.0.0.1:`server` and client mode polling 127.0.0.1:`client`, each when
    given; `cloud` adds the cloud relay's keys to server mode; `options` are the settings; `data` adds keys at the top,
    the serial for one."""
    server_data: dict[str, Any] = {CONF_ENABLED: server is not None}
    if server is not None:
        server_data |= {CONF_BIND: "127.0.0.1", CONF_PORT: server} | (cloud or {})
    client_data: dict[str, Any] = {CONF_ENABLED: client is not None}
    if client is not None:
        client_data |= {CONF_HOST: "127.0.0.1", CONF_PORT: client}
    return MockConfigEntry(domain=DOMAIN, title=title, data={CONF_SERVER: server_data, CONF_CLIENT: client_data, **data}, options=options or {})


async def set_up(hass: HomeAssistant, config: MockConfigEntry) -> InverterDevice:
    config.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    device = config.runtime_data
    assert isinstance(device, InverterDevice)
    return device


def state_of(hass: HomeAssistant, config: MockConfigEntry, platform: str, key: str) -> State:
    """The state of the entry's entity `key`."""
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{config.entry_id}_{key}")
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)
