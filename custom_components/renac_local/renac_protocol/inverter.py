"""One inverter as Home Assistant knows it: the words read from it and their times, the poll schedule, and each group's
last result. No I/O: the poller stores what it read; `readings()` decodes on demand.

The schedule: the groups, their intervals, the enabled set, live mode, each group's last read and the retry wait after
a failed one. The words: the raw word of every register read or written, by address, with the time and how long it stays
valid; the cloud session answers the cloud's reads from them while every register asked for is valid. The readings:
decoded from the words, by key, the registers of every group in group order, a later group's key replacing an earlier
one's; a group whose last read failed gives none, so its entities are unavailable. A failure is logged at warning level
when new and at debug level while it repeats.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .const import FAILED_RETRY_S, STATE_AGE_INTERVALS
from .group_reader import GroupResult
from .groups import GROUPS, PollGroup
from .registers import Block, Reading

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    """One register's word, when it was read or written (monotonic seconds), and how long it stays valid."""

    word: int
    at: float
    limit_s: float

    def is_valid(self, now: float) -> bool:
        return now - self.at <= self.limit_s


class Inverter:
    """Everything read from one inverter, and its poll schedule."""

    def __init__(
        self,
        groups: Iterable[PollGroup] = GROUPS,
        *,
        intervals: Mapping[str, float] | None = None,
        live_intervals: Mapping[str, float] | None = None,
        enabled: Mapping[str, bool] | None = None,
        age_intervals: float = STATE_AGE_INTERVALS,
    ) -> None:
        """`intervals`, `live_intervals` and `enabled` by group key replace the groups' own; a group without a live
        interval keeps its interval in live mode."""
        self.groups = tuple(groups)
        self.intervals = {group.key: group.interval_s for group in self.groups} | dict(intervals or {})
        self.live_intervals = {group.key: group.live_interval_s for group in self.groups if group.live_interval_s is not None} | dict(live_intervals or {})
        self.enabled = {group.key: group.enabled for group in self.groups} | dict(enabled or {})
        self.live_mode = False
        self.age_intervals = age_intervals
        # Each group's last result, by key, and when its last read was (monotonic seconds); a group without one is due.
        self.results: dict[str, GroupResult] = {}
        self._last_run: dict[str, float] = {}
        self._words: dict[int, Word] = {}
        # The registers with a limit of their own: the clock's.
        self._own_limits = {register.address: register.max_age_s for group in self.groups for register in group.registers if register.max_age_s is not None}

    # The schedule

    def interval_of(self, group: PollGroup) -> float:
        """The group's interval now: its live interval in live mode, when it has one, its configured one otherwise."""
        if self.live_mode and group.key in self.live_intervals:
            return self.live_intervals[group.key]
        return self.intervals[group.key]

    def is_due(self, group: PollGroup, now: float) -> bool:
        """True when the group is enabled and its wait since its last read is over, or it never ran."""
        return self.enabled.get(group.key, False) and self._due_in(group, now) <= 0

    def due(self, now: float) -> list[PollGroup]:
        """The due groups in cycle order."""
        return [group for group in self.groups if self.is_due(group, now)]

    def seconds_until_due(self, now: float) -> float | None:
        """Seconds until the earliest enabled group is due, 0 when one is due now, None when no group is enabled."""
        waits = [self._due_in(group, now) for group in self.groups if self.enabled.get(group.key, False)]
        return min(waits) if waits else None

    def _due_in(self, group: PollGroup, now: float) -> float:
        """Seconds until the group is due: its interval after its last read, or the retry wait after a failed one when
        that is shorter; 0 when it is due, or never ran."""
        last_run = self._last_run.get(group.key)
        if last_run is None:
            return 0.0
        wait = self.interval_of(group)
        result = self.results.get(group.key)
        if result is not None and not result.ok:
            wait = min(wait, FAILED_RETRY_S)
        return max(0.0, last_run + wait - now)

    def group_of(self, address: int) -> PollGroup | None:
        """The group whose ranges cover the register; None when no group reads it."""
        for group in self.groups:
            if any(block_range.covers(address) for block_range in group.ranges):
                return group
        return None

    # Storing what was read

    def store_result(self, result: GroupResult) -> None:
        """A group's read: its blocks' words kept with the time, the result kept, the schedule moved on."""
        group = result.group
        for block in result.blocks:
            self._store_words(group, block, result.finished_at)
        previous = self.results.get(group.key)
        self.results[group.key] = result
        self._last_run[group.key] = result.finished_at
        log_result(result, previous)

    def store_block(self, group: PollGroup, block: Block) -> None:
        """A block of the group read outside a group read, the identity read's: its words kept with the time."""
        self._store_words(group, block, time.monotonic())

    def fail(self, groups: Iterable[PollGroup], error: str) -> list[GroupResult]:
        """The groups could not be read, no connection or one not accepted: their results, each with the error."""
        now = time.monotonic()
        results = [GroupResult(group, error=error, finished_at=now) for group in groups]
        for result in results:
            self.store_result(result)
        return results

    def store_written(self, addresses: Iterable[int], values: Iterable[int]) -> None:
        """The inverter took a write: the written words from now, as if read. A register no group reads stays out."""
        now = time.monotonic()
        for address, word in zip(addresses, values, strict=False):
            limit = self._own_limits.get(address)
            if limit is None:
                group = self.group_of(address)
                limit = self.age_intervals * self.interval_of(group) if group is not None else None
            if limit is not None:
                self._words[address] = Word(word, now, limit)

    def forget(self, addresses: Iterable[int]) -> None:
        """Takes the registers' words out until they are read again."""
        for address in addresses:
            self._words.pop(address, None)

    def retry_failed(self) -> None:
        """The groups that failed are due again now, not after their retry wait: a way in has just come."""
        for key, result in self.results.items():
            if not result.ok:
                self._last_run.pop(key, None)

    def clear(self) -> None:
        """Forgets everything read; every enabled group is due again."""
        self.results.clear()
        self._last_run.clear()
        self._words.clear()

    # Reading what is held

    def valid_words(self, address: int, count: int) -> tuple[int, ...] | None:
        """The words of `count` registers from `address` when every one is held and valid now; None otherwise."""
        if count < 1:
            return None
        now = time.monotonic()
        words = []
        for register in range(address, address + count):
            entry = self._words.get(register)
            if entry is None or not entry.is_valid(now):
                return None
            words.append(entry.word)
        return tuple(words)

    def readings(self) -> dict[str, Reading]:
        """The readings of every group, by key, in group order: a later group's key replaces an earlier one's."""
        readings: dict[str, Reading] = {}
        for group in self.groups:
            readings.update(self.readings_of(group))
        return readings

    def readings_of(self, group: PollGroup) -> dict[str, Reading]:
        """The group's registers decoded from the words held, by key; none while the group's last read failed."""
        result = self.results.get(group.key)
        if result is not None and not result.ok:
            return {}
        readings: dict[str, Reading] = {}
        for register in group.registers:
            words = [self._words.get(address) for address in range(register.address, register.end)]
            if all(word is not None for word in words):
                readings[register.key] = register.decode(tuple(word.word for word in words if word is not None))
        return readings

    def _store_words(self, group: PollGroup, block: Block, at: float) -> None:
        group_limit = self.age_intervals * self.interval_of(group)
        for offset, word in enumerate(block.words):
            address = block.address + offset
            self._words[address] = Word(word, at, self._own_limits.get(address, group_limit))


def log_result(result: GroupResult, previous: GroupResult | None) -> None:
    """A failure at warning level when it is new, at debug level while it repeats; a success at debug level."""
    key = result.group.key
    if result.error is not None:
        repeated = previous is not None and previous.error == result.error
        LOGGER.log(logging.DEBUG if repeated else logging.WARNING, "group %s failed: %s", key, result.error)
    else:
        LOGGER.debug("group %s read in %.0f ms", key, result.duration_s * 1000)
