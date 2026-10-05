"""Reading one poll group through a connection: its ranges in order, each a fn 3 read, or where it failed.

The first range that fails ends the group; the blocks read before it stay in the result. The result carries words
only; the inverter (inverter.py) stores them and decodes on demand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .channel import ChannelError
from .connection import Connection, ReplyTimeout
from .frames import EXCEPTION_NAMES
from .groups import PollGroup
from .registers import Block


@dataclass
class GroupResult:
    """What one read of a group gave: its blocks, or where it failed, and when."""

    group: PollGroup
    blocks: list[Block] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    # Monotonic seconds, for the schedule.
    finished_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


async def read_group(connection: Connection, group: PollGroup) -> GroupResult:
    """Reads the group's ranges through the connection."""
    # perf_counter for the duration, as the connection's round trips; finished_at stays monotonic for the schedule.
    started = time.perf_counter()
    result = GroupResult(group)
    for block_range in group.ranges:
        where = f"{block_range.address} x{block_range.count}"
        try:
            reply = await connection.read(block_range.address, block_range.count)
        except (ReplyTimeout, ChannelError) as error:
            result.error = f"{where}: {error}"
            break
        if reply.is_exception:
            code = reply.exception_code or 0
            result.error = f"{where}: exception {code:02d} ({EXCEPTION_NAMES.get(code, 'unknown')})"
            break
        result.blocks.append(Block(block_range.address, reply.registers()))
    result.duration_s = time.perf_counter() - started
    result.finished_at = time.monotonic()
    return result
