"""The module's AA55 handshake: the requests the cloud opens a session with, what the module's replies say, and
sending it in the cloud's place (`send_handshake()`).

The module answers these itself, not the inverter. The module info reply gives the module's serial, MAC, firmware
and IP; the inverter serial reply the inverter's serial. Each text field is ASCII up to its first NUL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .channel import ChannelError
from .connection import Connection, ReplyTimeout, aa55_reply_matcher
from .frames import AA55Frame, build_aa55_frame

LOGGER = logging.getLogger(__name__)

# (command, sub) of each request.
MODULE_INFO = (0x15, 0x00)
# Purpose unknown; the cloud sends it twice. Its reply's fourth byte varies between sessions.
UNKNOWN_15_0B = (0x15, 0x0B)
INVERTER_SERIAL = (0x0E, 0x00)
# The cloud's keepalive; the module does not reply.
KEEPALIVE = (0x15, 0x02)

# The cloud's handshake in its order.
HANDSHAKE = (MODULE_INFO, UNKNOWN_15_0B, UNKNOWN_15_0B, INVERTER_SERIAL)
# The requests the module replies to; the keepalive, and any request not known, get no reply.
REPLIED = frozenset(HANDSHAKE)

# The text fields of the module info reply's payload: name -> (first byte, first byte past the field).
MODULE_INFO_FIELDS = {"module_serial": (1, 39), "mac": (39, 60), "firmware": (60, 111), "ip": (111, 150)}
# The text field of the inverter serial reply's payload.
INVERTER_SERIAL_FIELD = (0, 32)


def build_request(command: int, sub: int) -> bytes:
    """One handshake request: an AA55 frame without payload."""
    return build_aa55_frame(command, sub)


def read_text(payload: bytes, field: tuple[int, int]) -> str:
    """The ASCII text of a field, up to its first NUL; empty when the payload is shorter."""
    start, end = field
    return payload[start:end].split(b"\x00", 1)[0].decode("ascii", "replace").strip()


@dataclass
class ModuleInfo:
    """What the module's handshake replies said; empty texts until a reply came."""

    module_serial: str = ""
    mac: str = ""
    firmware: str = ""
    ip: str = ""
    inverter_serial: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((self.module_serial, self.mac, self.firmware, self.ip, self.inverter_serial))

    def update_from(self, frame: AA55Frame) -> bool:
        """Takes the fields of a module info or inverter serial reply; False for any other frame."""
        if not frame.is_reply:
            return False
        key = (frame.command, frame.sub)
        if key == MODULE_INFO:
            self.module_serial = read_text(frame.payload, MODULE_INFO_FIELDS["module_serial"])
            self.mac = read_text(frame.payload, MODULE_INFO_FIELDS["mac"])
            self.firmware = read_text(frame.payload, MODULE_INFO_FIELDS["firmware"])
            self.ip = read_text(frame.payload, MODULE_INFO_FIELDS["ip"])
            return True
        if key == INVERTER_SERIAL:
            self.inverter_serial = read_text(frame.payload, INVERTER_SERIAL_FIELD)
            return True
        return False

    def describe(self) -> str:
        """One line for logs and the console; `?` for a field no reply gave."""
        return (
            f"module serial {self.module_serial or '?'}, MAC {self.mac or '?'}, firmware {self.firmware or '?'}, IP {self.ip or '?'}; "
            f"inverter serial {self.inverter_serial or '?'}"
        )


async def send_handshake(connection: Connection, module: ModuleInfo) -> None:
    """Sends the cloud's handshake to the module, its replies into `module`. A request without a reply is logged and
    the next one sent; a lost connection ends it."""
    LOGGER.info("no cloud connected: sending the handshake")
    for command, sub in HANDSHAKE:
        try:
            reply = await connection.send(build_request(command, sub), aa55_reply_matcher(AA55Frame(command, sub, is_reply=False)))
        except ReplyTimeout as error:
            LOGGER.warning("handshake 0x%02x/0x%02x: %s", command, sub, error)
            continue
        except ChannelError as error:
            LOGGER.warning("handshake 0x%02x/0x%02x: %s", command, sub, error)
            return
        if isinstance(reply, AA55Frame):
            module.update_from(reply)
    LOGGER.info("handshake done: %s", module.describe())
