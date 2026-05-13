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

import asyncio
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
    ATTR_EVENT_ACTION,
    ATTR_LOGICAL_MODE,
    ATTR_MODE_ICONS,
    ATTR_MODE_NAMES,
    ATTR_NEXT_END,
    ATTR_NEXT_EVENT,
    ATTR_NEXT_START,
    ATTR_PULSE_DURATION,
    ATTR_PULSE_RUNNING,
    ATTR_SCHEDULE_ACTIVE,
    ATTR_SCHEDULE_KIND,
    ATTR_TARGET_ENTITY,
    ATTR_TARGET_STATE,
    ATTR_TODAY_BLOCKS,
    ATTR_TODAY_EVENTS,
    ATTR_WEEK_BLOCKS,
    ATTR_WEEK_EVENTS,
    CONF_ENTRY_TYPE,
    CONF_EVENT_ACTION,
    CONF_EVENT_SERVICE,
    CONF_EVENT_SERVICE_DATA,
    CONF_MODE_AUTO_ICON,
    CONF_MODE_AUTO_NAME,
    CONF_MODE_OFF_ICON,
    CONF_MODE_OFF_NAME,
    CONF_MODE_ON_ICON,
    CONF_MODE_ON_NAME,
    CONF_NAME,
    CONF_PULSE_DURATION,
    CONF_RESTORE_AUTO_ON_BOUNDARY,
    CONF_SCHEDULE_KIND,
    CONF_TARGET_ENTITY,
    DEFAULT_EVENT_ACTION,
    DEFAULT_MODE_AUTO_ICON,
    DEFAULT_MODE_AUTO_NAME,
    DEFAULT_MODE_OFF_ICON,
    DEFAULT_MODE_OFF_NAME,
    DEFAULT_MODE_ON_ICON,
    DEFAULT_MODE_ON_NAME,
    DEFAULT_PULSE_DURATION,
    DEFAULT_SCHEDULE_KIND,
    DOMAIN,
    ENTRY_TYPE_CURVE,
    ENTRY_TYPE_TIMER,
    EVENT_ACTION_CUSTOM,
    EVENT_ACTION_PULSE,
    EVENT_ACTION_TOGGLE,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    PULSE_COOL_DOWN_SECONDS,
    SCHEDULE_KIND_BLOCKS,
    SCHEDULE_KIND_EVENTS,
    WEEKDAY_KEYS,
)
from .curve import SmartCurveSelect
from .storage import async_load_blocks, async_load_curve, async_load_events

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Smart Schedule / Smart Curve select entity from a config entry."""
    entry_type = entry.options.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_TIMER:
        return
    if entry_type == ENTRY_TYPE_CURVE:
        points = await async_load_curve(hass, entry.entry_id)
        async_add_entities([SmartCurveSelect(entry, points)])
        return
    blocks = await async_load_blocks(hass, entry.entry_id)
    events = await async_load_events(hass, entry.entry_id)
    async_add_entities([SmartScheduleSelect(entry, blocks, events)])


class SmartScheduleSelect(SelectEntity, RestoreEntity):
    """Smart Schedule helper — select with 3 modes + inline schedule."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        blocks: dict[str, list[dict[str, Any]]],
        events: dict[str, list[dict[str, Any]]] | None = None,
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

        # Schedule kind + event-mode configuration.
        kind = config.get(CONF_SCHEDULE_KIND, DEFAULT_SCHEDULE_KIND)
        self._kind: str = kind if kind in (SCHEDULE_KIND_BLOCKS, SCHEDULE_KIND_EVENTS) else DEFAULT_SCHEDULE_KIND
        self._event_action: str = config.get(CONF_EVENT_ACTION, DEFAULT_EVENT_ACTION)
        self._pulse_duration: int = int(config.get(CONF_PULSE_DURATION, DEFAULT_PULSE_DURATION) or DEFAULT_PULSE_DURATION)
        self._event_service: str | None = config.get(CONF_EVENT_SERVICE) or None
        self._event_service_data: dict[str, Any] = dict(config.get(CONF_EVENT_SERVICE_DATA) or {})

        self._logical_mode: str = MODE_AUTO
        self._blocks: dict[str, list[dict[str, Any]]] = blocks
        self._events: dict[str, list[dict[str, Any]]] = events or {day: [] for day in WEEKDAY_KEYS}
        self._last_active: bool = False
        self._unsub_boundary: CALLBACK_TYPE | None = None

        # Pulse cool-down state: timestamp until which any new trigger
        # (Auto or manual) is silently dropped. Set when a pulse starts.
        self._pulse_blocked_until: datetime | None = None
        self._pulse_running: bool = False

        if self._kind == SCHEDULE_KIND_EVENTS:
            # Events mode: only Off / Auto are meaningful — "On" makes
            # no sense without an on/off state. Card matches this.
            self._attr_options = [
                self._mode_names[MODE_OFF],
                self._mode_names[MODE_AUTO],
            ]
        else:
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
        # Events mode has no "On" — coerce stale restores to Auto.
        if self._kind == SCHEDULE_KIND_EVENTS and self._logical_mode == MODE_ON:
            self._logical_mode = MODE_AUTO

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
        base: dict[str, Any] = {
            ATTR_LOGICAL_MODE: self._logical_mode,
            ATTR_TARGET_ENTITY: self._target_entity,
            ATTR_TARGET_STATE: target_state.state if target_state else None,
            ATTR_MODE_NAMES: dict(self._mode_names),
            ATTR_MODE_ICONS: dict(self._mode_icons),
            ATTR_SCHEDULE_KIND: self._kind,
            # Self-reference — lets cards that only know the companion
            # entity_id discover themselves via templates.
            ATTR_COMPANION_ENTITY: self.entity_id,
        }

        if self._kind == SCHEDULE_KIND_EVENTS:
            next_event_dt = self._next_event_dt()
            base.update({
                ATTR_EVENT_ACTION: self._event_action,
                ATTR_PULSE_DURATION: self._pulse_duration,
                ATTR_PULSE_RUNNING: self._pulse_running,
                ATTR_NEXT_EVENT: next_event_dt.isoformat() if next_event_dt else None,
                ATTR_TODAY_EVENTS: self._events_for_today(),
                ATTR_WEEK_EVENTS: self._events,
                # Mirror block-mode keys with empty/false defaults so
                # cards reading both shapes don't have to switch.
                ATTR_SCHEDULE_ACTIVE: self._pulse_running,
                ATTR_CURRENT_WINDOW: None,
                ATTR_TODAY_BLOCKS: [],
                ATTR_WEEK_BLOCKS: {day: [] for day in WEEKDAY_KEYS},
                ATTR_NEXT_START: None,
                ATTR_NEXT_END: None,
            })
            return base

        # Blocks mode (default / legacy).
        active = self._is_active_now()
        next_start, next_end = self._next_transitions()
        # Prefer end-of-window if we're currently in one; otherwise the
        # nearest upcoming start. Matches HA's `next_event` semantics on
        # the native schedule helper.
        next_event = next_end if active else next_start
        current_window = self._current_window()
        today_blocks = self._blocks_for_today()

        base.update({
            ATTR_SCHEDULE_ACTIVE: active,
            ATTR_NEXT_EVENT: next_event.isoformat() if next_event else None,
            ATTR_NEXT_START: next_start.isoformat() if next_start else None,
            ATTR_NEXT_END: next_end.isoformat() if next_end else None,
            ATTR_CURRENT_WINDOW: current_window,
            ATTR_TODAY_BLOCKS: today_blocks,
            ATTR_WEEK_BLOCKS: self._blocks,
            ATTR_TODAY_EVENTS: [],
            ATTR_WEEK_EVENTS: {day: [] for day in WEEKDAY_KEYS},
        })
        return base

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

    async def async_update_events(
        self, events: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Install a new set of weekly events (already persisted)."""
        self._events = events
        # Reschedule the next trigger because event times may have moved.
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        self._schedule_next_boundary()
        self.async_write_ha_state()

    async def async_trigger_event_now(self) -> bool:
        """Manually fire the configured event action.

        Returns True if the action was dispatched, False if the request
        was dropped due to an active pulse cool-down.
        """
        if self._kind != SCHEDULE_KIND_EVENTS:
            _LOGGER.debug(
                "trigger_event_now on %s ignored: not in events mode",
                self.entity_id,
            )
            return False
        return await self._trigger_event(reason="manual trigger", force=True)

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
        """Arm a one-shot callback for the next transition / event."""
        if self._kind == SCHEDULE_KIND_EVENTS:
            fire_at = self._next_event_dt()
            if fire_at is None:
                return
            self._unsub_boundary = async_track_point_in_time(
                self.hass, self._on_event_boundary, fire_at
            )
            return

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
        """Fired exactly at the next boundary time (blocks mode)."""
        self._unsub_boundary = None
        new_active = self._is_active_now()
        self.hass.async_create_task(self._handle_boundary(new_active))

    @callback
    def _on_event_boundary(self, _now: datetime) -> None:
        """Fired exactly at the next scheduled event time (events mode)."""
        self._unsub_boundary = None
        self.hass.async_create_task(self._handle_event_boundary())

    async def _handle_event_boundary(self) -> None:
        """Trigger the configured action (unless paused by Off mode)."""
        if self._logical_mode == MODE_OFF:
            _LOGGER.debug(
                "Smart Schedule %s: event suppressed (Off mode)",
                self.entity_id,
            )
        else:
            await self._trigger_event(reason="scheduled event", force=False)
        # Always rearm the next callback so the loop survives.
        self._schedule_next_boundary()
        self.async_write_ha_state()

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
        """Drive the target entity to the desired state if different.

        No-op in events mode — events only fire at scheduled times and
        don't maintain a continuous on/off state.
        """
        if self._kind == SCHEDULE_KIND_EVENTS:
            return
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
    # Events-mode helpers
    # ------------------------------------------------------------------

    def _events_for_day(self, dt_obj: datetime) -> list[dict[str, Any]]:
        return list(self._events.get(self._weekday_key_for(dt_obj), []))

    def _events_for_today(self) -> list[dict[str, Any]]:
        return self._events_for_day(dt_util.now())

    def _next_event_dt(self) -> datetime | None:
        """Return the wall-clock datetime of the next scheduled event."""
        now = dt_util.now()
        day_start = self._day_start(now)
        for offset in range(8):  # today + next 7 days
            probe = day_start + timedelta(days=offset)
            day_events = self._events.get(self._weekday_key_for(probe), [])
            for ev in day_events:
                seconds = self._parse_hms_to_seconds(ev.get("time", ""))
                if seconds is None:
                    continue
                fire_at = probe + timedelta(seconds=seconds)
                if fire_at > now:
                    return fire_at
        return None

    async def _trigger_event(self, reason: str, force: bool) -> bool:
        """Execute the configured event action with cool-down handling.

        Returns True if dispatched, False if dropped. `force=True` is set
        for manual triggers — they still respect the pulse cool-down
        (variant C, fixed 10s) but bypass any future-event timing check.
        """
        now = dt_util.now()
        if self._pulse_blocked_until is not None and now < self._pulse_blocked_until:
            _LOGGER.debug(
                "Smart Schedule %s: %s suppressed (pulse cool-down until %s)",
                self.entity_id,
                reason,
                self._pulse_blocked_until.isoformat(),
            )
            return False

        if not self._target_entity:
            _LOGGER.warning(
                "Smart Schedule %s: %s skipped — no target entity configured",
                self.entity_id,
                reason,
            )
            return False

        action = self._event_action
        try:
            if action == EVENT_ACTION_PULSE:
                self.hass.async_create_task(self._run_pulse(reason))
            elif action == EVENT_ACTION_CUSTOM:
                await self._call_custom_event_service(reason)
            else:  # toggle (default)
                await self.hass.services.async_call(
                    "homeassistant",
                    "toggle",
                    {"entity_id": self._target_entity},
                    blocking=False,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Smart Schedule %s: event dispatch failed (%s): %s",
                self.entity_id,
                reason,
                err,
            )
            return False
        return True

    async def _run_pulse(self, reason: str) -> None:
        """Turn the target on, wait pulse_duration seconds, turn it off.

        Sets a cool-down window (= pulse_duration + 10s) during which any
        other trigger is silently dropped. The pulse itself is best-effort
        — failures are logged but do not propagate.
        """
        duration = max(1, int(self._pulse_duration or DEFAULT_PULSE_DURATION))
        end_at = dt_util.now() + timedelta(seconds=duration + PULSE_COOL_DOWN_SECONDS)
        self._pulse_blocked_until = end_at
        self._pulse_running = True
        self.async_write_ha_state()

        _LOGGER.debug(
            "Smart Schedule %s: pulse start (%s) for %ds",
            self.entity_id,
            reason,
            duration,
        )
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "turn_on",
                {"entity_id": self._target_entity},
                blocking=False,
            )
            await asyncio.sleep(duration)
            await self.hass.services.async_call(
                "homeassistant",
                "turn_off",
                {"entity_id": self._target_entity},
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Smart Schedule %s: pulse failed: %s", self.entity_id, err
            )
        finally:
            self._pulse_running = False
            self.async_write_ha_state()

    async def _call_custom_event_service(self, reason: str) -> None:
        """Fire the user-configured custom service for this event."""
        service_ref = (self._event_service or "").strip()
        if "." not in service_ref:
            _LOGGER.warning(
                "Smart Schedule %s: custom event has no valid service (%s)",
                self.entity_id,
                reason,
            )
            return
        domain, service = service_ref.split(".", 1)
        data = dict(self._event_service_data)
        data.setdefault("entity_id", self._target_entity)
        await self.hass.services.async_call(domain, service, data, blocking=False)

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
