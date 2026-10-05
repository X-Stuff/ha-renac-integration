"""The HA glue's tests: they import homeassistant and run in the HA test image (tests/ha/Dockerfile), with the test
harness made for that Home Assistant release. The mocks run in the test process on 127.0.0.1; a test that opens a
socket asks for `socket_enabled`, as the harness blocks every socket otherwise."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator

import pytest
from homeassistant.core import HomeAssistant

from custom_components.renac_local.const import DOMAIN
from tests.mocks.mock_hf_lpt270 import Identity, initial_words, random_identity
from tests.mocks.mock_relay import MockRelay


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    """Every test may load custom_components.renac_local."""
    yield


@pytest.fixture(autouse=True)
async def unload_entries(hass: HomeAssistant) -> AsyncIterator[None]:
    """Every entry unloaded after the test: no channel, server or retry left behind."""
    yield
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def identity() -> Identity:
    return random_identity()


@pytest.fixture
async def relay(socket_enabled: None, identity: Identity) -> AsyncIterator[MockRelay]:
    """The mock relay answering from the fake module's made-up registers, as `identity`."""
    mock = MockRelay(initial_words(identity), latency_s=0.005)
    await mock.start()
    yield mock
    await mock.stop()
