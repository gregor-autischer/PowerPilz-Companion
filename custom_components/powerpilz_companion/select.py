"""Select entity for PowerPilz Smart Schedule helper.

v0.4+: weekly schedule blocks are stored by the integration itself in a
dedicated Store (`.storage/powerpilz_companion.schedules`). This entity:

- Exposes three modes: Off / On / Auto (renameable, with custom icons)
- In Auto mode: computes the current schedule active state from the
  stored blocks and drives the configured target device accordingly
- In Off / On mode: forces the target to the corresponding state,
  overriding the schedule
- Optionally, at every schedule boundary (on/off transition), an active
  Off/On override can be automatically released, returning the helper to
  Auto mode
- Publishes rich attributes (`schedule_active`, `next_event`,
  `current_window`, `today_blocks`, `week_blocks`) usable in templates
  and as trigger sources for standard state-change automations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COMPANION_ENTITY,
    ATTR_CURRENT_WINDOW,
    ATTR_LOGICAL_MODE,
    ATTR_MODE_ICONS,
    ATTR_MODE_NAMES,
    ATTR_NEXT_END,
    ATTR_NEXT_EVENT,
    ATTR_NEXT_START,
    ATTR_SCHEDULE_ACTIVE,
    ATTR_TARGET_ENTITY,
    ATTR_TARGET_STATE,
    ATTR_TODAY_BLOCKS,
    ATTR_WEEK_BLOCKS,
    CONF_ENTRY_TYPE,
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
    ENTRY_TYPE_TIMER,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    WEEKDAY_KEYS,
)
from .storage import async_load_blocks

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Smart Schedule select entity from a config entry."""
    if entry.options.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TIMER:
        return
    blocks = await async_load_blocks(hass, entry.entry_id)
    async_add_entities([SmartScheduleSelect(entry, blocks)])


class SmartScheduleSelect(SelectEntity, RestoreEntity):
    """Smart Schedule helper — select with 3 modes + inline schedule."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        blocks: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Initialize the helper entity."""
        self._entry = entry
        config = {**entry.data, **entry.options}

        name = str(config.get(CONF_NAME) or "Smart Schedule").strip()
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}"
        self._attr_icon = DEFAULT_MODE_AUTO_ICON

        self._target_entity: str = config.get(CONF_TARGET_ENTITY, "")

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
        self._blocks: dict[str, list[dict[str, Any]]] = blocks
        self._last_active: bool = False
        self._unsub_boundary: CALLBACK_TYPE | None = None

        self._attr_options = [
            self._mode_names[MODE_OFF],
            self._mode_names[MODE_ON],
            self._mode_names[MODE_AUTO],
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore state, register listeners, apply current mode."""
        await super().async_added_to_hass()

        # Restore previous logical mode.
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self._attr_options:
            logical = self._display_name_to_logical(last_state.state)
            if logical:
                self._logical_mode = logical

        # Expose ourselves in hass.data for services + cross-platform
        # (binary_sensor) access.
        hass_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = hass_data.setdefault(self._entry.entry_id, {})
        entry_data["entity"] = self

        # Target device tracking (only for attribute refresh).
        if self._target_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._target_entity], self._target_changed
                )
            )

        # Seed initial schedule-active state + schedule the next
        # boundary callback.
        self._last_active = self._is_active_now()
        self._schedule_next_boundary()

        # Apply current mode on startup.
        await self._apply_current_mode(reason="entity added")
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up timers + hass.data reference."""
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
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
        target_state = self.hass.states.get(self._target_entity)
        active = self._is_active_now()
        next_start, next_end = self._next_transitions()
        # Prefer end-of-window if we're currently in one; otherwise the
        # nearest upcoming start. Matches HA's `next_event` semantics on
        # the native schedule helper.
        next_event = next_end if active else next_start
        current_window = self._current_window()
        today_blocks = self._blocks_for_today()

        return {
            ATTR_LOGICAL_MODE: self._logical_mode,
            ATTR_TARGET_ENTITY: self._target_entity,
            ATTR_TARGET_STATE: target_state.state if target_state else None,
            ATTR_MODE_NAMES: dict(self._mode_names),
            ATTR_MODE_ICONS: dict(self._mode_icons),
            ATTR_SCHEDULE_ACTIVE: active,
            ATTR_NEXT_EVENT: next_event.isoformat() if next_event else None,
            ATTR_NEXT_START: next_start.isoformat() if next_start else None,
            ATTR_NEXT_END: next_end.isoformat() if next_end else None,
            ATTR_CURRENT_WINDOW: current_window,
            ATTR_TODAY_BLOCKS: today_blocks,
            ATTR_WEEK_BLOCKS: self._blocks,
            # Self-reference — lets cards that only know the companion
            # entity_id discover themselves via templates.
            ATTR_COMPANION_ENTITY: self.entity_id,
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
    # Public API used by the set_schedule_blocks service
    # ------------------------------------------------------------------

    async def async_update_blocks(
        self, blocks: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Install a new set of weekly blocks (already persisted)."""
        self._blocks = blocks
        prev_active = self._last_active
        now_active = self._is_active_now()

        # Reschedule boundary callback because transitions may have
        # moved.
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        self._schedule_next_boundary()

        if now_active != prev_active:
            await self._handle_boundary(now_active)
        else:
            # Even without a state flip, refresh target (e.g. in On mode
            # the schedule change is irrelevant but we write attrs).
            await self._apply_current_mode(reason="blocks updated")

        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Schedule evaluation
    # ------------------------------------------------------------------

    def _weekday_key_for(self, dt_obj: datetime) -> str:
        # Python's datetime.weekday(): 0=Monday … 6=Sunday — same order
        # as WEEKDAY_KEYS.
        return WEEKDAY_KEYS[dt_obj.weekday()]

    def _blocks_for_day(
        self, dt_obj: datetime
    ) -> list[dict[str, Any]]:
        return list(self._blocks.get(self._weekday_key_for(dt_obj), []))

    def _blocks_for_today(self) -> list[dict[str, Any]]:
        return self._blocks_for_day(dt_util.now())

    @staticmethod
    def _parse_hms_to_seconds(value: str) -> int | None:
        if not isinstance(value, str):
            return None
        parts = value.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            s = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            return None
        # HA schedule semantics allow "24:00:00" as end-of-day sentinel.
        total = h * 3600 + m * 60 + s
        if total < 0 or total > 24 * 3600:
            return None
        return total

    def _day_start(self, dt_obj: datetime) -> datetime:
        return dt_obj.replace(hour=0, minute=0, second=0, microsecond=0)

    def _is_active_now(self) -> bool:
        now = dt_util.now()
        day_start = self._day_start(now)
        now_s = (now - day_start).total_seconds()
        for blk in self._blocks_for_today():
            frm = self._parse_hms_to_seconds(blk.get("from", ""))
            to = self._parse_hms_to_seconds(blk.get("to", ""))
            if frm is None or to is None or to <= frm:
                continue
            if frm <= now_s < to:
                return True
        return False

    def _current_window(self) -> dict[str, Any] | None:
        now = dt_util.now()
        day_start = self._day_start(now)
        now_s = (now - day_start).total_seconds()
        for blk in self._blocks_for_today():
            frm = self._parse_hms_to_seconds(blk.get("from", ""))
            to = self._parse_hms_to_seconds(blk.get("to", ""))
            if frm is None or to is None or to <= frm:
                continue
            if frm <= now_s < to:
                payload: dict[str, Any] = {
                    "from": blk.get("from"),
                    "to": blk.get("to"),
                    "start": (day_start + timedelta(seconds=frm)).isoformat(),
                    "end": (day_start + timedelta(seconds=to)).isoformat(),
                }
                if isinstance(blk.get("data"), dict):
                    payload["data"] = blk["data"]
                return payload
        return None

    def _next_transitions(
        self,
    ) -> tuple[datetime | None, datetime | None]:
        """Return (next_start, next_end) datetimes looking 7 days ahead.

        - `next_start`: wall-clock time of the next block's `from`
        - `next_end`:  wall-clock time of the next block's `to`

        If we're currently inside a window, `next_end` is that window's
        end and `next_start` is the next different window's start.
        """
        now = dt_util.now()
        day_start = self._day_start(now)

        next_start: datetime | None = None
        next_end: datetime | None = None

        for offset in range(8):  # today + next 7 days
            probe = day_start + timedelta(days=offset)
            day_blocks = self._blocks.get(self._weekday_key_for(probe), [])
            for blk in day_blocks:
                frm = self._parse_hms_to_seconds(blk.get("from", ""))
                to = self._parse_hms_to_seconds(blk.get("to", ""))
                if frm is None or to is None or to <= frm:
                    continue
                start_dt = probe + timedelta(seconds=frm)
                end_dt = probe + timedelta(seconds=to)
                if next_start is None and start_dt > now:
                    next_start = start_dt
                if next_end is None and end_dt > now:
                    next_end = end_dt
                if next_start is not None and next_end is not None:
                    return next_start, next_end
        return next_start, next_end

    def _schedule_next_boundary(self) -> None:
        """Arm a one-shot callback for the next transition."""
        next_start, next_end = self._next_transitions()
        candidates = [t for t in (next_start, next_end) if t is not None]
        if not candidates:
            return
        fire_at = min(candidates)
        self._unsub_boundary = async_track_point_in_time(
            self.hass, self._on_boundary, fire_at
        )

    @callback
    def _on_boundary(self, _now: datetime) -> None:
        """Fired exactly at the next boundary time."""
        self._unsub_boundary = None
        new_active = self._is_active_now()
        self.hass.async_create_task(self._handle_boundary(new_active))

    async def _handle_boundary(self, new_active: bool) -> None:
        prev_active = self._last_active
        self._last_active = new_active

        # If user had a manual override and opted in to auto-release,
        # drop it at each boundary crossing.
        if (
            new_active != prev_active
            and self._restore_auto_on_boundary
            and self._logical_mode in (MODE_OFF, MODE_ON)
        ):
            _LOGGER.debug(
                "Smart Schedule %s crossed boundary → auto-restoring Auto mode",
                self.entity_id,
            )
            self._logical_mode = MODE_AUTO

        await self._apply_current_mode(reason="schedule boundary")
        self._schedule_next_boundary()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @callback
    def _target_changed(self, _event: Event) -> None:
        """Refresh attributes when the target device state changes."""
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
        # Auto — follow our own computed schedule.
        return STATE_ON if self._is_active_now() else STATE_OFF

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
