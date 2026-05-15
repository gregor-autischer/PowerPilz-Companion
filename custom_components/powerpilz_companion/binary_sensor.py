"""Binary sensor platform for Smart Schedule: `schedule_active` flag.

Every Smart Schedule config entry spawns a companion `binary_sensor.*`
whose state reflects whether the weekly schedule is currently inside an
active block. This provides drop-in parity with HA's native
`schedule.*` helper for state-trigger-based automations:

    trigger:
      platform: state
      entity_id: binary_sensor.smart_schedule_heating_active
      to: "on"

The sensor mirrors the same rich attributes as the select entity
(`current_window`, `next_event`, `today_blocks`, `week_blocks`) so
templates can read everything from a single entity.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    ATTR_CURRENT_WINDOW,
    ATTR_NEXT_END,
    ATTR_NEXT_EVENT,
    ATTR_NEXT_START,
    ATTR_SCHEDULE_ACTIVE,
    ATTR_TODAY_BLOCKS,
    ATTR_WEEK_BLOCKS,
    CONF_ENTRY_TYPE,
    CONF_NAME,
    DOMAIN,
    ENTRY_TYPE_TIMER,
)

_LOGGER = logging.getLogger(__name__)

# Interesting subset of attributes copied from the parent select so
# consumers don't have to read two entities.
_MIRROR_ATTRS = (
    ATTR_CURRENT_WINDOW,
    ATTR_NEXT_EVENT,
    ATTR_NEXT_START,
    ATTR_NEXT_END,
    ATTR_TODAY_BLOCKS,
    ATTR_WEEK_BLOCKS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Smart Schedule active sensor from a config entry."""
    if entry.options.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TIMER:
        return
    async_add_entities([SmartScheduleActiveBinarySensor(entry)])


class SmartScheduleActiveBinarySensor(BinarySensorEntity):
    """Active-window indicator for a Smart Schedule helper."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}
        base_name = str(config.get(CONF_NAME) or "Smart Schedule").strip()
        self._attr_name = f"{base_name} active"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_active"
        self._attr_icon = "mdi:clock-check-outline"
        # Populated in `async_added_to_hass` once the parent select has
        # registered in hass.data.
        self._parent_entity_id: str | None = None

    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._wire_parent_listener()

    @callback
    def _wire_parent_listener(self, _now: Any = None) -> None:
        """Attach the state-change listener once the parent select is
        live. Both platforms set up in parallel, so we may need to
        retry briefly until the select registers itself in hass.data.
        """
        if self._parent_entity_id is not None:
            return
        parent = self._resolve_parent()
        if parent is None:
            # Retry shortly — platform setup runs in parallel.
            unsub = async_call_later(
                self.hass, 0.5, self._wire_parent_listener
            )
            self.async_on_remove(unsub)
            return
        self._parent_entity_id = parent.entity_id
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [parent.entity_id], self._parent_changed
            )
        )
        # Trigger an initial state push once we're wired up.
        self.async_write_ha_state()

    def _resolve_parent(self) -> Any | None:
        bucket = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if not isinstance(bucket, dict):
            return None
        return bucket.get("entity")

    @callback
    def _parent_changed(self, _event: Event) -> None:
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Entity state
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        parent = self._resolve_parent()
        if parent is None:
            return False
        try:
            return bool(parent._is_active_now())  # noqa: SLF001
        except AttributeError:
            return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parent = self._resolve_parent()
        if parent is None:
            return {ATTR_SCHEDULE_ACTIVE: False}
        try:
            src = parent.extra_state_attributes
        except Exception:  # noqa: BLE001
            return {ATTR_SCHEDULE_ACTIVE: False}
        out: dict[str, Any] = {ATTR_SCHEDULE_ACTIVE: src.get(ATTR_SCHEDULE_ACTIVE)}
        for key in _MIRROR_ATTRS:
            if key in src:
                out[key] = src[key]
        # Always expose a back-pointer to the parent select so
        # automations/templates can locate it from the sensor alone.
        out["companion_entity"] = parent.entity_id
        return out
