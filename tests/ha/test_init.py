"""Setting the entry up and unloading it: client mode, server mode, neither (its device only)."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.renac_local.const import DOMAIN
from tests.ha.common import entry, free_port
from tests.mocks.mock_relay import MockRelay


async def test_client_mode_loads_and_unloads(hass: HomeAssistant, relay: MockRelay) -> None:
    config = entry(client=relay.port)
    config.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.LOADED
    # The device is the entry's, there before anything is read.
    assert dr.async_get(hass).async_get_device_by_identifier((DOMAIN, config.entry_id), config.entry_id) is not None

    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.NOT_LOADED


async def test_server_mode_listens_while_loaded(hass: HomeAssistant, socket_enabled: None) -> None:
    port = free_port()
    config = entry(server=port)
    config.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.LOADED
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()

    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.NOT_LOADED
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_client_mode_without_its_endpoint_loads_and_reads_nothing(hass: HomeAssistant, socket_enabled: None) -> None:
    config = entry(client=free_port())
    config.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.LOADED
    device = config.runtime_data
    await asyncio.sleep(0.3)
    assert device.data == {}


async def test_server_mode_with_its_port_taken_tries_again_later(hass: HomeAssistant, socket_enabled: None) -> None:
    taken = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        config = entry(server=taken.sockets[0].getsockname()[1])
        config.add_to_hass(hass)
        await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        assert config.state is ConfigEntryState.SETUP_RETRY
    finally:
        taken.close()
        await taken.wait_closed()


async def test_an_entry_with_neither_way_has_its_device_only(hass: HomeAssistant) -> None:
    config = entry()
    config.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    assert config.state is ConfigEntryState.LOADED
    assert dr.async_get(hass).async_get_device_by_identifier((DOMAIN, config.entry_id), config.entry_id) is not None
    assert er.async_entries_for_config_entry(er.async_get(hass), config.entry_id) == []
    assert await hass.config_entries.async_unload(config.entry_id)
