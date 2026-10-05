"""The Renac N3-HV protocol core, in small blocks: the frames and the framer, the channel, the connection and the
listener, the poll groups, the register table and the decoders, the inverter, the handshake, the DNS client, the cloud
session, and the poller that runs them (PLAN D39, §6.5); apart from them, the module's AT session (§6.11).

Standalone: standard library only, no Home Assistant import, no third-party package.
The tests and the command-line tools import it by this name; the integration imports it relatively.
"""

import logging

from .at_session import AtError, AtSession, FoundModule, SocketB, discover
from .channel import Channel, ChannelClosed, ChannelError, Opener, open_stream
from .cloud_session import CloudAddress, CloudCounters, CloudSession, CloudSettings, CloudStatus, CloudWrite
from .connection import Connection, Priority, ReplyTimeout, dial
from .dns import DnsError, query_a, resolve_host
from .framer import Framer
from .frames import AA55Frame, Frame, FrameError, ModbusFrame, ModbusKind, build_read_request, describe_frame, parse_frame
from .group_reader import GroupResult, read_group
from .groups import GROUPS, GROUPS_BY_KEY, PollGroup, RegisterRange
from .handshake import ModuleInfo, send_handshake
from .inverter import Inverter
from .listener import Listener
from .poller import ClientSettings, Poller, ServerSettings, Session, Source, read_identity
from .register_table import ALL_REGISTERS, REGISTERS_BY_KEY
from .registers import Block, Kind, Reading, Register, decode_block

# A library's logs go where its user sends them; nothing on stderr when the user sets up no logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ALL_REGISTERS",
    "GROUPS",
    "GROUPS_BY_KEY",
    "REGISTERS_BY_KEY",
    "AA55Frame",
    "AtError",
    "AtSession",
    "Block",
    "Channel",
    "ChannelClosed",
    "ChannelError",
    "ClientSettings",
    "CloudAddress",
    "CloudCounters",
    "CloudSession",
    "CloudSettings",
    "CloudStatus",
    "CloudWrite",
    "Connection",
    "DnsError",
    "FoundModule",
    "Frame",
    "FrameError",
    "Framer",
    "GroupResult",
    "Inverter",
    "Kind",
    "Listener",
    "ModbusFrame",
    "ModbusKind",
    "ModuleInfo",
    "Opener",
    "PollGroup",
    "Poller",
    "Priority",
    "Reading",
    "Register",
    "RegisterRange",
    "ReplyTimeout",
    "ServerSettings",
    "Session",
    "SocketB",
    "Source",
    "build_read_request",
    "decode_block",
    "describe_frame",
    "dial",
    "discover",
    "open_stream",
    "parse_frame",
    "query_a",
    "read_group",
    "read_identity",
    "resolve_host",
    "send_handshake",
]
