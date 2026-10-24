"""The w2b relay's battery (PLAN §6.7): with TCP polling, when its address answers as a w2b relay, the battery voltage
from the relay's status page, `http://<address>/api/status`, read every 5 minutes apart from the inverter's polling,
and the percentage the relay's own page makes of it. An address that gives no such page, another bridge, has no
relay battery.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

READ_EVERY = timedelta(minutes=5)
REQUEST_TIMEOUT_S = 10.0
# A 3.7 V lithium-ion cell under the relay's load, (mV, %), the highest first. The load pulls the voltage down, so
# the data sheets' rest curve reads too low on the relay: here 3.55 V is about 20 % and 3.2 V is empty, a straight
# line between the points. The relay's own page uses this curve.
BATTERY_CURVE: tuple[tuple[int, int], ...] = (
    (4200, 100),
    (4150, 95),
    (4100, 90),
    (4050, 85),
    (4000, 80),
    (3950, 75),
    (3900, 70),
    (3850, 60),
    (3800, 50),
    (3750, 40),
    (3700, 30),
    (3620, 25),
    (3550, 20),
    (3480, 15),
    (3400, 10),
    (3300, 5),
    (3200, 0),
)


def battery_percent(millivolts: int) -> int:
    """An estimate from the voltage, as the relay's page shows it: a straight line between the curve's points. Charging
    raises the voltage, a load lowers it."""
    if millivolts >= BATTERY_CURVE[0][0]:
        return BATTERY_CURVE[0][1]
    for (upper_mv, upper_percent), (lower_mv, lower_percent) in itertools.pairwise(BATTERY_CURVE):
        if millivolts >= lower_mv:
            return round(lower_percent + (upper_percent - lower_percent) * (millivolts - lower_mv) / (upper_mv - lower_mv))
    return 0


@dataclass(frozen=True)
class RelayBatteryReading:
    """The relay's battery voltage in millivolts; 0 when the relay reports no battery."""

    millivolts: int

    @property
    def volts(self) -> float | None:
        return self.millivolts / 1000 if self.millivolts else None

    @property
    def percent(self) -> int | None:
        return battery_percent(self.millivolts) if self.millivolts else None


class RelayBattery(DataUpdateCoordinator[RelayBatteryReading]):
    """Reads the relay's status page every 5 minutes; its entities are unavailable while the page does not answer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: str) -> None:
        super().__init__(hass, LOGGER, config_entry=entry, name=f"{DOMAIN} relay battery {host}", update_interval=READ_EVERY)
        self.url = f"http://{host}/api/status"

    @classmethod
    async def find(cls, hass: HomeAssistant, entry: ConfigEntry, host: str) -> RelayBattery | None:
        """The relay's battery when the address answers as a w2b relay, with its first reading; None when it does not."""
        relay = cls(hass, entry, host)
        try:
            reading = await relay._read()
        except UpdateFailed as error:
            LOGGER.debug("no w2b relay at %s: %s", host, error)
            return None
        relay.async_set_updated_data(reading)
        LOGGER.info("w2b relay at %s, battery %s", host, f"{reading.millivolts} mV" if reading.millivolts else "none")
        return relay

    async def _async_update_data(self) -> RelayBatteryReading:
        return await self._read()

    async def _read(self) -> RelayBatteryReading:
        """One read of the status page. Raises UpdateFailed when it does not answer, or not as a w2b relay."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(self.url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)) as response:
                if response.status != 200:
                    raise UpdateFailed(f"{self.url}: HTTP {response.status}")
                status = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise UpdateFailed(f"{self.url}: {error or 'timeout'}") from error
        except ValueError as error:
            raise UpdateFailed(f"{self.url}: not JSON: {error}") from error
        device = status.get("device") if isinstance(status, dict) else None
        millivolts = device.get("battery_mv") if isinstance(device, dict) else None
        if not isinstance(millivolts, int) or isinstance(millivolts, bool):
            raise UpdateFailed(f"{self.url}: no battery_mv in the answer")
        return RelayBatteryReading(max(0, millivolts))
