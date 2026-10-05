"""Shared by every test. tests/ha imports homeassistant; where it is not installed (Windows) that folder is not collected,
and it runs in the HA test image instead (tests/ha/Dockerfile)."""

from __future__ import annotations

import importlib.util
import random

import pytest

from tests.mocks.mock_hf_lpt270 import SimulatedInverter, initial_words, random_identity

collect_ignore = [] if importlib.util.find_spec("homeassistant") else ["ha"]

# The undocumented group's registers, which the fake inverter does not hold.
UNDOCUMENTED_ADDRESSES = (20114, 20130, 50028, 50029, 50030)


@pytest.fixture
def inverter_words() -> dict[int, int]:
    """The registers of a fake inverter with a random identity, the sun up, and random words for the undocumented group."""
    inverter = SimulatedInverter(initial_words(random_identity()))
    inverter.advance(0)
    return inverter.words | {address: random.randrange(0x10000) for address in UNDOCUMENTED_ADDRESSES}
