"""The Fix of the issue issues.py raises (PLAN §6.7): the issue's text with Submit, then the entry's reconfigure in place
of this flow. The issue stays: it goes when the reconfigure reloads the entry, and comes back if the inverter still does
not answer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import FlowType, RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None) -> RepairsFlow:
    assert data is not None
    return ReconfigureFlow(str(data["entry_id"]))


class ReconfigureFlow(RepairsFlow):
    """The issue's text, then the reconfigure of the entry `entry_id`."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """The issue's text with its reasons; submitted, the reconfigure opens in place of this flow."""
        if user_input is not None:
            reconfigure = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": self.entry_id})
            return self.async_abort(reason="reconfigure", next_flow=(FlowType.CONFIG_FLOW, reconfigure["flow_id"]))
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        placeholders = dict(issue.translation_placeholders or {}) if issue is not None else None
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), last_step=False, description_placeholders=placeholders)
