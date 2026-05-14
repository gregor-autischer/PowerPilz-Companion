"""Select entities for PowerPilz Smart Schedule helpers.

Two flavours of `select.*` entity live in this module, both exposing
a small set of mode options to the user:

- `SmartScheduleSelect` (entry_type=schedule) — blocks mode. Weekly
  time windows; Off/On/Auto modes drive the target on/off boundaries.
- `SmartEventScheduleSelect` (entry_type=event_schedule) — events
  mode. Weekly point-in-time triggers; Off/Auto modes. Each scheduled
  moment fires a configured action (toggle / pulse / custom service)
  on the target. A companion `button.*` entity records every fire in
  HA history.

The two classes share mode/target/restore-auto bookkeeping through
`_BaseSmartScheduleSelect`. Smart Curve is a separate entity class
that lives in `curve.py`.
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
    ATTR_PULSE_BLOCKED_UNTIL,
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
    CONF_TARGET_ENTITY,
    DEFAULT_EVENT_ACTION,
    DEFAULT_MODE_AUTO_ICON,
    DEFAULT_MODE_AUTO_NAME,
    DEFAULT_MODE_OFF_ICON,
    DEFAULT_MODE_OFF_NAME,
    DEFAULT_MODE_ON_ICON,
    DEFAULT_MODE_ON_NAME,
    DEFAULT_PULSE_DURATION,
    DOMAIN,
    ENTRY_TYPE_CURVE,
    ENTRY_TYPE_EVENT_SCHEDULE,
    ENTRY_TYPE_TIMER,
    EVENT_ACTION_CUSTOM,
    EVENT_ACTION_PULSE,
    EVENT_ACTION_TOGGLE,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    PULSE_COOL_DOWN_SECONDS,
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
    """Set up the appropriate select entity for the given config entry."""
    entry_type = entry.options.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_TIMER:
        return
    if entry_type == ENTRY_TYPE_CURVE:
        points = await async_load_curve(hass, entry.entry_id)
        async_add_entities([SmartCurveSelect(entry, points)])
        return
    if entry_type == ENTRY_TYPE_EVENT_SCHEDULE:
        events = await async_load_events(hass, entry.entry_id)
        async_add_entities([SmartEventScheduleSelect(entry, events)])
        return
    blocks = await async_load_blocks(hass, entry.entry_id)
    async_add_entities([SmartScheduleSelect(entry, blocks)])


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _BaseSmartScheduleSelect(SelectEntity, RestoreEntity):
    """Shared mode / target / lifecycle plumbing for both schedule kinds."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    # Subclasses set this so the base can hand a concrete option list to HA.
    _logical_modes: tuple[str, ...] = ()
    SCHEDULE_KIND: str = ""

    def __init__(self, entry: ConfigEntry, default_title: str) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}

        name = str(config.get(CONF_NAME) or default_title).strip()
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
        self._unsub_boundary: CALLBACK_TYPE | None = None

        # Subclasses may not have an "On" mode — they must declare their
        # supported logical modes via `_logical_modes`.
        self._attr_options = [self._mode_names[m] for m in self._logical_modes]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self._attr_options:
            logical = self._display_name_to_logical(last_state.state)
            if logical:
                self._logical_mode = logical
        # Coerce restores of modes the subclass no longer supports.
        if self._logical_mode not in self._logical_modes:
            self._logical_mode = MODE_AUTO

        hass_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = hass_data.setdefault(self._entry.entry_id, {})
        entry_data["entity"] = self

        if self._target_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._target_entity], self._target_changed
                )
            )

        self._on_added_seed_state()
        self._schedule_next_boundary()
        await self._apply_current_mode(reason="entity added")
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        hass_data = self.hass.data.get(DOMAIN, {})
        entry_data = hass_data.get(self._entry.entry_id, {})
        if entry_data.get("entity") is self:
            entry_data.pop("entity", None)
        await super().async_will_remove_from_hass()

    # Subclass hooks (no-op defaults).
    def _on_added_seed_state(self) -> None:
        """Compute any subclass-specific initial state at startup."""

    # ------------------------------------------------------------------
    # Select entity API
    # ------------------------------------------------------------------

    @property
    def current_option(self) -> str | None:
        return self._mode_names[self._logical_mode]

    @property
    def icon(self) -> str | None:
        return self._mode_icons.get(self._logical_mode, DEFAULT_MODE_AUTO_ICON)

    async def async_select_option(self, option: str) -> None:
        logical = self._display_name_to_logical(option)
        if logical is None or logical not in self._logical_modes:
            _LOGGER.warning("Unknown mode selected on %s: %s", self.entity_id, option)
            return
        self._logical_mode = logical
        await self._apply_current_mode(reason=f"mode set to {logical}")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _base_attributes(self) -> dict[str, Any]:
        target_state = self.hass.states.get(self._target_entity)
        return {
            ATTR_LOGICAL_MODE: self._logical_mode,
            ATTR_TARGET_ENTITY: self._target_entity,
            ATTR_TARGET_STATE: target_state.state if target_state else None,
            ATTR_MODE_NAMES: dict(self._mode_names),
            ATTR_MODE_ICONS: dict(self._mode_icons),
            ATTR_SCHEDULE_KIND: self.SCHEDULE_KIND,
            ATTR_COMPANION_ENTITY: self.entity_id,
        }

    def _weekday_key_for(self, dt_obj: datetime) -> str:
        return WEEKDAY_KEYS[dt_obj.weekday()]

    def _day_start(self, dt_obj: datetime) -> datetime:
        return dt_obj.replace(hour=0, minute=0, second=0, microsecond=0)

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
        total = h * 3600 + m * 60 + s
        if total < 0 or total > 24 * 3600:
            return None
        return total

    def _display_name_to_logical(self, display_name: str | None) -> str | None:
        if not display_name:
            return None
        for logical, name in self._mode_names.items():
            if name == display_name:
                return logical
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @callback
    def _target_changed(self, _event: Event) -> None:
        """Refresh attributes when the target device state changes."""
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    def _schedule_next_boundary(self) -> None:
        """Arm a one-shot callback for the next transition / event."""
        raise NotImplementedError

    async def _apply_current_mode(self, reason: str) -> None:
        """Drive the target based on the current logical mode."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Blocks-mode Smart Schedule
# ---------------------------------------------------------------------------


class SmartScheduleSelect(_BaseSmartScheduleSelect):
    """Smart Schedule helper in blocks mode — weekly time windows."""

    _logical_modes = (MODE_OFF, MODE_ON, MODE_AUTO)
    SCHEDULE_KIND = "blocks"

    def __init__(
        self,
        entry: ConfigEntry,
        blocks: dict[str, list[dict[str, Any]]],
    ) -> None:
        super().__init__(entry, default_title="Smart Schedule")
        self._blocks: dict[str, list[dict[str, Any]]] = blocks
        self._last_active: bool = False

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        active = self._is_active_now()
        next_start, next_end = self._next_transitions()
        next_event = next_end if active else next_start
        out = self._base_attributes()
        out.update({
            ATTR_SCHEDULE_ACTIVE: active,
            ATTR_NEXT_EVENT: next_event.isoformat() if next_event else None,
            ATTR_NEXT_START: next_start.isoformat() if next_start else None,
            ATTR_NEXT_END: next_end.isoformat() if next_end else None,
            ATTR_CURRENT_WINDOW: self._current_window(),
            ATTR_TODAY_BLOCKS: self._blocks_for_today(),
            ATTR_WEEK_BLOCKS: self._blocks,
        })
        return out

    # ------------------------------------------------------------------
    # Public API used by the set_schedule_blocks service
    # ------------------------------------------------------------------

    async def async_update_blocks(
        self, blocks: dict[str, list[dict[str, Any]]]
    ) -> None:
        self._blocks = blocks
        prev_active = self._last_active
        now_active = self._is_active_now()

        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        self._schedule_next_boundary()

        if now_active != prev_active:
            await self._handle_boundary(now_active)
        else:
            await self._apply_current_mode(reason="blocks updated")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Schedule evaluation
    # ------------------------------------------------------------------

    def _on_added_seed_state(self) -> None:
        self._last_active = self._is_active_now()

    def _blocks_for_day(self, dt_obj: datetime) -> list[dict[str, Any]]:
        return list(self._blocks.get(self._weekday_key_for(dt_obj), []))

    def _blocks_for_today(self) -> list[dict[str, Any]]:
        return self._blocks_for_day(dt_util.now())

    def _is_active_now(self) -> bool:
        now = dt_util.now()
        now_s = (now - self._day_start(now)).total_seconds()
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

    def _next_transitions(self) -> tuple[datetime | None, datetime | None]:
        now = dt_util.now()
        day_start = self._day_start(now)
        next_start: datetime | None = None
        next_end: datetime | None = None
        for offset in range(8):
            probe = day_start + timedelta(days=offset)
            for blk in self._blocks.get(self._weekday_key_for(probe), []):
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
        self._unsub_boundary = None
        new_active = self._is_active_now()
        self.hass.async_create_task(self._handle_boundary(new_active))

    async def _handle_boundary(self, new_active: bool) -> None:
        prev_active = self._last_active
        self._last_active = new_active

        if (
            new_active != prev_active
            and self._restore_auto_on_boundary
            and self._logical_mode in (MODE_OFF, MODE_ON)
        ):
            _LOGGER.debug(
                "Smart Schedule %s crossed boundary -> auto-restoring Auto",
                self.entity_id,
            )
            self._logical_mode = MODE_AUTO

        await self._apply_current_mode(reason="schedule boundary")
        self._schedule_next_boundary()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Control logic
    # ------------------------------------------------------------------

    def _desired_target_state(self) -> str | None:
        if self._logical_mode == MODE_OFF:
            return STATE_OFF
        if self._logical_mode == MODE_ON:
            return STATE_ON
        return STATE_ON if self._is_active_now() else STATE_OFF

    async def _apply_current_mode(self, reason: str) -> None:
        desired = self._desired_target_state()
        if desired is None:
            return

        target = self.hass.states.get(self._target_entity)
        if target is None or target.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "Target %s not available - skipping apply (%s)",
                self._target_entity,
                reason,
            )
            return

        if target.state == desired:
            return

        service = "turn_on" if desired == STATE_ON else "turn_off"
        _LOGGER.debug(
            "Applying %s -> homeassistant.%s (reason: %s)",
            self._target_entity,
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


# ---------------------------------------------------------------------------
# Events-mode Smart Schedule
# ---------------------------------------------------------------------------


class SmartEventScheduleSelect(_BaseSmartScheduleSelect):
    """Smart Schedule helper in events mode — point-in-time triggers."""

    _logical_modes = (MODE_OFF, MODE_AUTO)
    SCHEDULE_KIND = "events"

    def __init__(
        self,
        entry: ConfigEntry,
        events: dict[str, list[dict[str, Any]]],
    ) -> None:
        super().__init__(entry, default_title="Smart Event Schedule")
        config = {**entry.data, **entry.options}

        self._event_action: str = config.get(CONF_EVENT_ACTION, DEFAULT_EVENT_ACTION)
        self._pulse_duration: int = int(
            config.get(CONF_PULSE_DURATION, DEFAULT_PULSE_DURATION)
            or DEFAULT_PULSE_DURATION
        )
        self._event_service: str | None = config.get(CONF_EVENT_SERVICE) or None
        self._event_service_data: dict[str, Any] = dict(
            config.get(CONF_EVENT_SERVICE_DATA) or {}
        )

        self._events: dict[str, list[dict[str, Any]]] = events or {
            day: [] for day in WEEKDAY_KEYS
        }
        self._pulse_blocked_until: datetime | None = None
        self._pulse_running: bool = False

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        next_event_dt = self._next_event_dt()
        out = self._base_attributes()
        out.update({
            ATTR_EVENT_ACTION: self._event_action,
            ATTR_PULSE_DURATION: self._pulse_duration,
            ATTR_PULSE_RUNNING: self._pulse_running,
            ATTR_PULSE_BLOCKED_UNTIL: (
                self._pulse_blocked_until.isoformat()
                if self._pulse_blocked_until is not None
                else None
            ),
            ATTR_NEXT_EVENT: next_event_dt.isoformat() if next_event_dt else None,
            ATTR_TODAY_EVENTS: self._events_for_today(),
            ATTR_WEEK_EVENTS: self._events,
            # Mirror schedule_active = pulse_running so consumers that
            # only know the blocks-shape keep working in Auto-style.
            ATTR_SCHEDULE_ACTIVE: self._pulse_running,
        })
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_update_events(
        self, events: dict[str, list[dict[str, Any]]]
    ) -> None:
        self._events = events
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        self._schedule_next_boundary()
        self.async_write_ha_state()

    async def async_trigger_event_now(self) -> bool:
        """Manually fire the configured event action."""
        return await self._trigger_event(reason="manual trigger", force=True)

    # ------------------------------------------------------------------
    # Events evaluation
    # ------------------------------------------------------------------

    def _events_for_day(self, dt_obj: datetime) -> list[dict[str, Any]]:
        return list(self._events.get(self._weekday_key_for(dt_obj), []))

    def _events_for_today(self) -> list[dict[str, Any]]:
        return self._events_for_day(dt_util.now())

    def _next_event_dt(self) -> datetime | None:
        now = dt_util.now()
        day_start = self._day_start(now)
        for offset in range(8):
            probe = day_start + timedelta(days=offset)
            for ev in self._events.get(self._weekday_key_for(probe), []):
                seconds = self._parse_hms_to_seconds(ev.get("time", ""))
                if seconds is None:
                    continue
                fire_at = probe + timedelta(seconds=seconds)
                if fire_at > now:
                    return fire_at
        return None

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _schedule_next_boundary(self) -> None:
        fire_at = self._next_event_dt()
        if fire_at is None:
            return
        self._unsub_boundary = async_track_point_in_time(
            self.hass, self._on_event_boundary, fire_at
        )

    @callback
    def _on_event_boundary(self, _now: datetime) -> None:
        self._unsub_boundary = None
        self.hass.async_create_task(self._handle_event_boundary())

    async def _handle_event_boundary(self) -> None:
        if self._logical_mode == MODE_OFF:
            _LOGGER.debug(
                "Smart Event Schedule %s: scheduled event suppressed (Off mode)",
                self.entity_id,
            )
        else:
            await self._trigger_event(reason="scheduled event", force=False)
        # Always rearm the next callback so the scheduling loop survives.
        self._schedule_next_boundary()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Mode application — events mode has no continuous on/off state
    # ------------------------------------------------------------------

    async def _apply_current_mode(self, reason: str) -> None:
        """No-op: events mode only fires at scheduled times."""

    # ------------------------------------------------------------------
    # Event dispatch + pulse with cool-down (variant C, 10s fixed)
    # ------------------------------------------------------------------

    async def _trigger_event(self, reason: str, force: bool) -> bool:
        """Execute the configured event action with cool-down handling.

        Returns True if dispatched, False if dropped. `force=True` is set
        for manual triggers — they still respect the pulse cool-down but
        bypass any future-event timing check.
        """
        now = dt_util.now()
        if self._pulse_blocked_until is not None and now < self._pulse_blocked_until:
            _LOGGER.debug(
                "Smart Event Schedule %s: %s suppressed (cool-down until %s)",
                self.entity_id,
                reason,
                self._pulse_blocked_until.isoformat(),
            )
            return False

        if not self._target_entity:
            _LOGGER.warning(
                "Smart Event Schedule %s: %s skipped - no target entity configured",
                self.entity_id,
                reason,
            )
            return False

        action = self._event_action
        dispatched = False
        try:
            if action == EVENT_ACTION_PULSE:
                # Pulse runs asynchronously and updates state internally.
                self.hass.async_create_task(self._run_pulse(reason))
                dispatched = True
            elif action == EVENT_ACTION_CUSTOM:
                await self._call_custom_event_service(reason)
                dispatched = True
            else:  # toggle (default)
                await self.hass.services.async_call(
                    "homeassistant",
                    "toggle",
                    {"entity_id": self._target_entity},
                    blocking=False,
                )
                dispatched = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Smart Event Schedule %s: event dispatch failed (%s): %s",
                self.entity_id,
                reason,
                err,
            )
            return False

        if dispatched:
            self._notify_trigger_button(reason)
        return True

    async def _run_pulse(self, reason: str) -> None:
        """Turn target on, wait pulse_duration seconds, turn off; then
        keep the cool-down armed for PULSE_COOL_DOWN_SECONDS."""
        duration = max(1, int(self._pulse_duration or DEFAULT_PULSE_DURATION))
        end_at = dt_util.now() + timedelta(
            seconds=duration + PULSE_COOL_DOWN_SECONDS
        )
        self._pulse_blocked_until = end_at
        self._pulse_running = True
        self.async_write_ha_state()

        _LOGGER.debug(
            "Smart Event Schedule %s: pulse start (%s) for %ds",
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
                "Smart Event Schedule %s: pulse failed: %s", self.entity_id, err
            )

        self._pulse_running = False
        self.async_write_ha_state()

        try:
            await asyncio.sleep(PULSE_COOL_DOWN_SECONDS)
        except asyncio.CancelledError:
            return

        self._pulse_blocked_until = None
        self.async_write_ha_state()

    async def _call_custom_event_service(self, reason: str) -> None:
        service_ref = (self._event_service or "").strip()
        if "." not in service_ref:
            _LOGGER.warning(
                "Smart Event Schedule %s: custom event has no valid service (%s)",
                self.entity_id,
                reason,
            )
            return
        domain, service = service_ref.split(".", 1)
        data = dict(self._event_service_data)
        data.setdefault("entity_id", self._target_entity)
        await self.hass.services.async_call(domain, service, data, blocking=False)

    # ------------------------------------------------------------------
    # Cross-wire with the companion button entity (history record)
    # ------------------------------------------------------------------

    def _notify_trigger_button(self, reason: str) -> None:
        """Tell the companion button entity to record a press in history."""
        if self.hass is None:
            return
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        button = entry_data.get("trigger_button") if isinstance(entry_data, dict) else None
        if button is None:
            return
        try:
            button.async_internal_fire(reason)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Smart Event Schedule %s: button notify failed: %s",
                self.entity_id,
                err,
            )
