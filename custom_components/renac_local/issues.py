"""The Repairs issue of an entry whose inverter has never answered: no serial read yet, so no entities.

Raised once the grace time after setup has passed with no answer and there is a reason to show: server mode's module
not connected, or the last error, the client's dial or the identity read. Its reasons follow every update; it goes at
the first answer, when the serial is read, and with the entry. Its Fix opens the entry's reconfigure (repairs.py).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .renac_protocol import Inverter, Poller

# How long a new entry waits for the inverter's first answer before the issue is raised.
NO_ANSWER_GRACE_S = 60.0


class NoAnswerIssue:
    """The issue of one entry, driven by its device: `start()` at setup, `update()` after every change the poller
    reports, `stop()` at unload."""

    def __init__(self, hass: HomeAssistant, entry_id: str, title: str, poller: Poller, inverter: Inverter) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.issue_id = f"no_answer_{entry_id}"
        self.title = title
        self.poller = poller
        self.inverter = inverter
        self.grace_over = False
        # The reasons the issue shows; None while it is not raised.
        self.reasons: str | None = None
        self._cancel_grace: Callable[[], None] | None = None

    def start(self) -> None:
        self._cancel_grace = async_call_later(self.hass, NO_ANSWER_GRACE_S, self._on_grace_over)

    @callback
    def stop(self) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None
        self._delete()

    @callback
    def update(self) -> None:
        """Deleted once the serial is known; raised or updated after the grace time while there is a reason."""
        if self.poller.serial is not None:
            self._delete()
            return
        if not self.grace_over:
            return
        reasons = "; ".join(self._reasons())
        if not reasons or reasons == self.reasons:
            return
        self.reasons = reasons
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=True,
            data={"entry_id": self.entry_id},
            severity=ir.IssueSeverity.WARNING,
            translation_key="no_answer",
            translation_placeholders={"title": self.title, "reasons": reasons},
        )

    def _reasons(self) -> list[str]:
        """Server mode's module not connected; the latest error of a group read, the client's included."""
        reasons = []
        listener = self.poller.listener
        if listener is not None and self.poller.session is None:
            reasons.append(f"the module has not connected to Home Assistant (port {listener.port})")
        errors = [(result.finished_at, result.error) for result in self.inverter.results.values() if result.error is not None]
        if errors:
            reasons.append(max(errors)[1])
        return reasons

    @callback
    def _on_grace_over(self, now: datetime) -> None:
        self._cancel_grace = None
        self.grace_over = True
        self.update()

    @callback
    def _delete(self) -> None:
        if self.reasons is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            self.reasons = None
