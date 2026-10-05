"""The core's defaults, in one place: ports, addresses and timeouts its blocks and their users share."""

from __future__ import annotations

# The cloud's port, which the module dials; server mode listens on it too.
CLOUD_PORT = 20145
DEFAULT_SERVER_PORT = CLOUD_PORT
DEFAULT_BIND = "0.0.0.0"
# The BLE relay's port for the inverter: client mode's default.
DEFAULT_CLIENT_PORT = 8899
# The module's AT side, UDP (PLAN §2.8).
AT_PORT = 48899
# The DNS server the cloud's name is resolved through when the LAN's DNS is hijacked.
DEFAULT_DNS_SERVER = "8.8.8.8"

# Seconds a partial frame may wait for its rest before it is dropped as garbage.
IDLE_TIMEOUT_S = 0.3
CONNECT_TIMEOUT_S = 10.0
REPLY_TIMEOUT_S = 3.0
# The first reply after a dial may take this long: the relay still brings its BLE link up.
FIRST_REPLY_TIMEOUT_S = 15.0
RETRIES = 1
REQUEST_GAP_S = 0.1
# Client mode: seconds the next request waits after a frame nobody asked for, the cloud polling over the shared UART.
QUIET_TIME_S = 20.0
# Seconds a module session settles before its first request.
SETTLE_S = 1.0
# Seconds before a module session whose identity read failed is checked again.
CHECK_RETRY_S = 10.0
# Seconds before the next attempt to reach the cloud: after a drop the first, after each failed attempt the next; the
# last repeats.
CLOUD_RETRY_S = (5.0, 10.0, 30.0, 60.0)
# A word is valid for this many of its group's intervals, unless its register has a limit of its own.
STATE_AGE_INTERVALS = 3.0
# A group that failed is tried again after this, or after its interval when that is shorter.
FAILED_RETRY_S = 60.0
