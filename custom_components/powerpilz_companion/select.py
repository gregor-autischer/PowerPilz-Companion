"""Select entity for PowerPilz Smart Schedule helper.

The actual weekly schedule is managed by a native Home Assistant Schedule
helper (a `schedule.*` entity) which the user creates separately. This
`select` entity:

- Exposes three modes: Off / On / Auto (renameable, with custom icons)
- In Auto mode: mirrors the linked schedule's state (on/off) onto the
  configured target device via `homeassistant.turn_on/turn_off`
- In Off / On mode: forces the target to the corresponding state,
  overriding the schedule
- Optionally, at every schedule boundary (on/off transition), an active
  Off/On override can be automatically released, returning the helper to
  Auto mode
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_TIMER,
    ATTR_LINKED_SCHEDULE,
    ATTR_LOGICAL_MODE,
    ATTR_MODE_ICONS,
    ATTR_MODE_NAMES,
    ATTR_NEXT_EVENT,
    ATTR_SCHEDULE_STATE,
    ATTR_TARGET_ENTITY,
    ATTR_TARGET_STATE,
    CONF_LINKED_SCHEDULE,
    CONF_MODE_AUTO_ICON,
    CONF_MODE_AUTO_NAME,
    CONF_MODE_OFF_ICON,
    CONF_MODE_OFF_NAME,
    CONF_MODE_ON_ICON,
    CONF_MODE_ON_NAME,
    CONF_NAME,
    CONF_RESTORE_AUTO_ON_BOUNDARY,
    CONF_TARGET_ENTITY,
    DEFAULT_MODE_AUTO_ICON,
    DEFAULT_MODE_AUTO_NAME,
    DEFAULT_MODE_OFF_ICON,
    DEFAULT_MODE_OFF_NAME,
    DEFAULT_MODE_ON_ICON,
    DEFAULT_MODE_ON_NAME,
    DOMAIN,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Smart Schedule select entity from a config entry."""
    if entry.options.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TIMER:
        return
    async_add_entities([SmartScheduleSelect(entry)])


class SmartScheduleSelect(SelectEntity, RestoreEntity):
    """Smart Schedule helper entity — references a native schedule helper."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the helper entity."""
        self._entry = entry
        config = {**entry.data, **entry.options}

        name = str(config.get(CONF_NAME) or "Smart Schedule").strip()
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}"
        self._attr_icon = DEFAULT_MODE_AUTO_ICON

        self._target_entity: str = config.get(CONF_TARGET_ENTITY, "")
        self._linked_schedule: str = config.get(CONF_LINKED_SCHEDULE, "")
        if not self._linked_schedule:
            _LOGGER.warning(
                "Smart Schedule '%s' has no linked schedule configured — "
                "please reconfigure this helper (open its Options).",
                name,
            )

        self._mode_names: dict[str, str] = {
            MODE_OFF: config.get(CONF_MODE_OFF_NAME, DEFAULT_MODE_OFF_NAME),
            MODE_ON: config.get(CONF_MODE_ON_NAME, DEFAULT_MODE_ON_NAME),
            MODE_AUTO: config.get(CONF_MODE_AUTO_NAME, DEFAULT_MODE_AUTO_NAME),
        }
        self._mode_icons: dict[str, str] = {
            MODE_OFF: config.get(CONF_MODE_OFF_ICON, DEFAULT_MODE_OFF_ICON),
            MODE_ON: config.get(CONF_MODE_ON_ICON, DEFAULT_MODE_ON_ICON),
            MODE_AUTO: config.get(CONF_MODE_AUTO_ICON, DEFAULT_MODE_AUTO_ICON),
        }
        self._restore_auto_on_boundary: bool = bool(
            config.get(CONF_RESTORE_AUTO_ON_BOUNDARY, True)
        )

        self._logical_mode: str = MODE_AUTO
        self._last_schedule_state: str | None = None

        # Track whether a pending target state change was issued by us, so we
        # can distinguish it from genuine manual overrides if we ever want to.
        self._expected_target_state: str | None = None

        self._attr_options = [
            self._mode_names[MODE_OFF],
            self._mode_names[MODE_ON],
            self._mode_names[MODE_AUTO],
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore state and register listeners."""
        await super().async_added_to_hass()

        # Restore previous logical mode.
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self._attr_options:
            logical = self._display_name_to_logical(last_state.state)
            if logical:
                self._logical_mode = logical

        # Register in hass.data for service + introspection access.
        hass_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = hass_data.setdefault(self._entry.entry_id, {})
        entry_data["entity"] = self

        # Track schedule + target state changes (if configured).
        if self._linked_schedule:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._linked_schedule], self._schedule_changed
                )
            )
        if self._target_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._target_entity], self._target_changed
                )
            )

        # Seed initial schedule state tracking.
        sched_state = (
            self.hass.states.get(self._linked_schedule)
            if self._linked_schedule
            else None
        )
        self._last_schedule_state = (
            sched_state.state if sched_state is not None else None
        )

        # Apply current mode on startup.
        await self._apply_current_mode(reason="entity added")
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up hass.data reference."""
        hass_data = self.hass.data.get(DOMAIN, {})
        entry_data = hass_data.get(self._entry.entry_id, {})
        if entry_data.get("entity") is self:
            entry_data.pop("entity", None)
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Select entity API
    # ------------------------------------------------------------------

    @property
    def current_option(self) -> str | None:
        return self._mode_names[self._logical_mode]

    @property
    def icon(self) -> str | None:
        return self._mode_icons.get(self._logical_mode, DEFAULT_MODE_AUTO_ICON)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched_state = self.hass.states.get(self._linked_schedule)
        target_state = self.hass.states.get(self._target_entity)
        next_event = None
        if sched_state is not None:
            next_event = sched_state.attributes.get("next_event")
        return {
            ATTR_LOGICAL_MODE: self._logical_mode,
            ATTR_TARGET_ENTITY: self._target_entity,
            ATTR_TARGET_STATE: target_state.state if target_state else None,
            ATTR_LINKED_SCHEDULE: self._linked_schedule,
            ATTR_SCHEDULE_STATE: sched_state.state if sched_state else None,
            ATTR_MODE_NAMES: dict(self._mode_names),
            ATTR_MODE_ICONS: dict(self._mode_icons),
            ATTR_NEXT_EVENT: next_event,
        }

    async def async_select_option(self, option: str) -> None:
        """Change the logical mode via user interaction."""
        logical = self._display_name_to_logical(option)
        if logical is None:
            _LOGGER.warning("Unknown mode selected: %s", option)
            return
        self._logical_mode = logical
        await self._apply_current_mode(reason=f"mode set to {logical}")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @callback
    def _schedule_changed(self, event: Event) -> None:
        """Handle state changes of the linked schedule entity."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        new_value = new_state.state if new_state else None
        old_value = old_state.state if old_state else None

        # Only react to on/off flips — ignore unavailable/unknown flaps.
        if new_value not in (STATE_ON, STATE_OFF):
            self._last_schedule_state = new_value
            self.async_write_ha_state()
            return

        prev = self._last_schedule_state
        self._last_schedule_state = new_value

        # If schedule crossed a boundary (on↔off) and user had an override
        # active, optionally restore Auto mode.
        boundary_crossed = (
            old_value in (STATE_ON, STATE_OFF)
            and new_value in (STATE_ON, STATE_OFF)
            and old_value != new_value
        ) or (prev in (STATE_ON, STATE_OFF) and prev != new_value)

        if (
            boundary_crossed
            and self._restore_auto_on_boundary
            and self._logical_mode in (MODE_OFF, MODE_ON)
        ):
            _LOGGER.debug(
                "Schedule %s crossed boundary → auto-restoring Auto mode",
                self._linked_schedule,
            )
            self._logical_mode = MODE_AUTO

        self.hass.async_create_task(
            self._apply_current_mode(reason="schedule state change")
        )
        self.async_write_ha_state()

    @callback
    def _target_changed(self, event: Event) -> None:
        """Handle state changes of the target device (for UI refresh)."""
        # We only use this to update displayed attributes; no control logic.
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Control logic
    # ------------------------------------------------------------------

    def _desired_target_state(self) -> str | None:
        """Return "on", "off", or None depending on current logical mode."""
        if self._logical_mode == MODE_OFF:
            return STATE_OFF
        if self._logical_mode == MODE_ON:
            return STATE_ON
        # Auto — mirror linked schedule.
        if not self._linked_schedule:
            return None
        sched_state = self.hass.states.get(self._linked_schedule)
        if sched_state is None or sched_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        ):
            return None
        if sched_state.state in (STATE_ON, STATE_OFF):
            return sched_state.state
        return None

    async def _apply_current_mode(self, reason: str) -> None:
        """Drive the target entity to the desired state if different."""
        desired = self._desired_target_state()
        if desired is None:
            return

        target = self.hass.states.get(self._target_entity)
        if target is None or target.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "Target %s not available — skipping apply (%s)",
                self._target_entity,
                reason,
            )
            return

        current = target.state
        if current == desired:
            return

        service = "turn_on" if desired == STATE_ON else "turn_off"
        _LOGGER.debug(
            "Applying %s → %s.%s (reason: %s)",
            self._target_entity,
            "homeassistant",
            service,
            reason,
        )
        self._expected_target_state = desired
        try:
            await self.hass.services.async_call(
                "homeassistant",
                service,
                {"entity_id": self._target_entity},
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to drive %s to %s: %s", self._target_entity, desired, err
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _display_name_to_logical(self, display_name: str | None) -> str | None:
        if not display_name:
            return None
        for logical, name in self._mode_names.items():
            if name == display_name:
                return logical
        return None
