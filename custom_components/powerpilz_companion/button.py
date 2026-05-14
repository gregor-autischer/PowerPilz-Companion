"""Button entity for the Smart Event Schedule helper.

Each Smart Event Schedule (events-mode) helper exposes a companion
`button.*_trigger` entity. The button has two purposes:

1. **User interaction**: pressing it (from a dashboard tile, an
   automation, etc.) fires the configured event action on the target,
   exactly like the card's "Trigger now" button. Press is rejected
   silently during the pulse cool-down.

2. **Unified history**: every event fire — scheduled or manual —
   writes a state change on this button. The HA history view of the
   button therefore shows a precise log of when the target was
   activated, regardless of who initiated it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    CONF_NAME,
    DOMAIN,
    ENTRY_TYPE_EVENT_SCHEDULE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the trigger button — only for event_schedule entries."""
    entry_type = entry.options.get(CONF_ENTRY_TYPE)
    if entry_type != ENTRY_TYPE_EVENT_SCHEDULE:
        return
    async_add_entities([SmartEventScheduleButton(entry)])


class SmartEventScheduleButton(ButtonEntity):
    """Trigger button for a Smart Event Schedule helper."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:play"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}
        base_name = str(config.get(CONF_NAME) or "Smart Event Schedule").strip()
        self._attr_name = f"{base_name} trigger"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_trigger"
        self._last_fire_utc: datetime | None = None

    @property
    def state(self) -> str | None:
        """Expose our own last-fire timestamp.

        Overrides ButtonEntity's built-in `state` (which tracks
        last_pressed from `button.press` service calls only). We need
        the state to also flip when the schedule fires automatically,
        so the HA history shows a unified timeline of every fire.
        """
        return self._last_fire_utc.isoformat() if self._last_fire_utc else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bucket = self.hass.data.setdefault(DOMAIN, {})
        entry_data = bucket.setdefault(self._entry.entry_id, {})
        entry_data["trigger_button"] = self

    async def async_will_remove_from_hass(self) -> None:
        bucket = self.hass.data.get(DOMAIN, {})
        entry_data = bucket.get(self._entry.entry_id, {})
        if isinstance(entry_data, dict) and entry_data.get("trigger_button") is self:
            entry_data.pop("trigger_button", None)
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface the companion select entity for templating."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        entity = entry_data.get("entity") if isinstance(entry_data, dict) else None
        return {
            "schedule_entity": getattr(entity, "entity_id", None),
        }

    async def async_press(self) -> None:
        """User-initiated press → ask the select entity to fire."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        entity = entry_data.get("entity") if isinstance(entry_data, dict) else None
        if entity is None or not hasattr(entity, "async_trigger_event_now"):
            _LOGGER.warning(
                "Trigger button pressed but no live event-schedule entity for %s",
                self._entry.entry_id,
            )
            return
        # The select runs the action AND, on success, calls
        # `async_internal_fire` back on us to record the press in history.
        await entity.async_trigger_event_now()

    def async_internal_fire(self, reason: str) -> None:
        """Record a fire without invoking the action.

        Called by the companion select entity after a successful event
        dispatch (scheduled or manual) so the button's state change is
        archived in HA history as a single timeline of all fires.
        """
        self._last_fire_utc = dt_util.utcnow()
        _LOGGER.debug(
            "Smart Event Schedule button %s: recorded fire (%s)",
            self.entity_id,
            reason,
        )
        self.async_write_ha_state()
