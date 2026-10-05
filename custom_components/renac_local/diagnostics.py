"""The entry's diagnostics download: the entry's data and options, the poller's state, the last connection either way,
open or closed, and its counters, the cloud's, each group's last result and the readings; the inverter's serial
redacted, the one thing in it that names the unit."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL
from .inverter_device import RenacConfigEntry

# The inverter's serial: the entry's and the readings' `serial`, the module's handshake reply's `inverter_serial`.
TO_REDACT = {CONF_SERIAL, "inverter_serial"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RenacConfigEntry) -> dict[str, Any]:
    device = getattr(entry, "runtime_data", None)
    if device is None:
        # Neither way in yet: the entry's data and options alone.
        return async_redact_data({"entry": dict(entry.data), "options": dict(entry.options)}, TO_REDACT)
    poller = device.poller
    inverter = device.inverter
    session = poller.session
    connection = poller.last_connection
    cloud = poller.cloud
    source = poller.current_source
    now = time.monotonic()
    data = {
        "entry": dict(entry.data),
        "options": dict(entry.options),
        "poller": {
            "source": source.value if source is not None else None,
            "serial": poller.serial,
            "live_mode": poller.live_mode,
            "module_session": session.state if session is not None else None,
            "server_refused": poller.server_refused,
            "client_refused": poller.client_refused,
            "last_session_end": poller.last_session_end,
            "module": asdict(poller.module),
        },
        "connection": None
        if connection is None
        else {
            "name": connection.name,
            "open": connection.is_connected,
            "close_reason": connection.close_reason,
            "requests": asdict(connection.counters),
            "channel": asdict(connection.channel.counters),
            "framer": asdict(connection.channel.framer.counters),
        },
        "cloud": None
        if cloud is None
        else {
            "status": asdict(cloud.status),
            "counters": asdict(cloud.counters),
            "last_write": asdict(cloud.last_write) if cloud.last_write is not None else None,
        },
        "groups": {
            key: {"error": result.error, "duration_ms": round(result.duration_s * 1000), "age_s": round(now - result.finished_at)}
            for key, result in inverter.results.items()
        },
        "readings": {key: reading.text for key, reading in inverter.readings().items()},
        "relay_battery": asdict(device.relay_battery.data) if device.relay_battery is not None and device.relay_battery.data is not None else None,
    }
    return async_redact_data(data, TO_REDACT)
