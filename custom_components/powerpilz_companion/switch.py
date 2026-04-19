"""Switch platform for PowerPilz Companion — Smart Timer helper.

A Smart Timer bundles a target device, an on-time, an optional off-time
and an active flag into a single `switch` entity. When turned on, the
switch autonomously drives the target device:

  - at `on_datetime`, the target is turned on
  - at `off_datetime`, the target is turned off AND the timer
    self-deactivates (one-shot semantics)

If the helper is re-activated while the current time is already inside
the [on, off] window, the target is brought on immediately.

State + on/off datetimes are persisted across restarts via RestoreEntity
— no extra config-entry writes are needed for the per-timer state, so
we don't trigger reload loops.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DIRECTION,
    ATTR_NEXT_EVENT,
    ATTR_OFF_DATETIME,
    ATTR_ON_DATETIME,
    ATTR_STATE_ICONS,
    ATTR_STATE_NAMES,
    ATTR_TARGET_ENTITY,
    ATTR_TARGET_STATE,
    CONF_ENTRY_TYPE,
    CONF_NAME,
    CONF_STATE_ACTIVE_ICON,
    CONF_STATE_ACTIVE_NAME,
    CONF_STATE_INACTIVE_ICON,
    CONF_STATE_INACTIVE_NAME,
    CONF_TARGET_ENTITY,
    CONF_TIMER_DIRECTION,
    CONF_TIMER_OFF_OPTION,
    CONF_TIMER_ON_OPTION,
    DEFAULT_STATE_ACTIVE_ICON,
    DEFAULT_STATE_ACTIVE_NAME,
    DEFAULT_STATE_INACTIVE_ICON,
    DEFAULT_STATE_INACTIVE_NAME,
    DEFAULT_TIMER_DIRECTION,
    DOMAIN,
    ENTRY_TYPE_TIMER,
    TIMER_DIRECTION_OFF_ONLY,
    TIMER_DIRECTION_ON_ONLY,
    TIMER_DIRECTIONS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a Smart Timer switch entity from a config entry."""
    if entry.options.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_TIMER:
        return
    async_add_entities([SmartTimerSwitch(entry)])


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (tolerant) into a local datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = dt_util.as_local(parsed)
    return dt_util.as_local(parsed)


class SmartTimerSwitch(SwitchEntity, RestoreEntity):
    """Autonomous Smart Timer."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}

        self._attr_name = str(config.get(CONF_NAME) or "Smart Timer").strip()
        self._attr_unique_id = f"{DOMAIN}_timer_{entry.entry_id}"
        self._attr_icon = "mdi:timer-outline"

        self._target_entity: str = config.get(CONF_TARGET_ENTITY, "")
        self._on_option: str | None = config.get(CONF_TIMER_ON_OPTION) or None
        self._off_option: str | None = config.get(CONF_TIMER_OFF_OPTION) or None

        direction = config.get(CONF_TIMER_DIRECTION, DEFAULT_TIMER_DIRECTION)
        self._direction: str = (
            direction if direction in TIMER_DIRECTIONS else DEFAULT_TIMER_DIRECTION
        )

        self._state_names = {
            "inactive": str(
                config.get(CONF_STATE_INACTIVE_NAME) or DEFAULT_STATE_INACTIVE_NAME
            ),
            "active": str(
                config.get(CONF_STATE_ACTIVE_NAME) or DEFAULT_STATE_ACTIVE_NAME
            ),
        }
        self._state_icons = {
            "inactive": str(
                config.get(CONF_STATE_INACTIVE_ICON) or DEFAULT_STATE_INACTIVE_ICON
            ),
            "active": str(
                config.get(CONF_STATE_ACTIVE_ICON) or DEFAULT_STATE_ACTIVE_ICON
            ),
        }

        # Runtime state — restored from last_state in async_added_to_hass.
        self._active: bool = False
        self._on_dt: datetime | None = None
        self._off_dt: datetime | None = None

        self._on_unsub: CALLBACK_TYPE | None = None
        self._off_unsub: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last = await self.async_get_last_state()
        if last and last.state in (STATE_ON, STATE_OFF):
            self._active = last.state == STATE_ON
            self._on_dt = _parse_dt(last.attributes.get(ATTR_ON_DATETIME))
            self._off_dt = _parse_dt(last.attributes.get(ATTR_OFF_DATETIME))

        # Register in hass.data so the set_timer service can find us.
        hass_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = hass_data.setdefault(self._entry.entry_id, {})
        entry_data["timer_entity"] = self

        # Resume timers if we were active before the restart.
        if self._active:
            self._schedule_timers()
            await self._apply_current_window(reason="startup resume")

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timers()
        hass_data = self.hass.data.get(DOMAIN, {})
        entry_data = hass_data.get(self._entry.entry_id, {})
        if entry_data.get("timer_entity") is self:
            entry_data.pop("timer_entity", None)
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Switch API
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._active

    @property
    def icon(self) -> str | None:
        key = "active" if self._active else "inactive"
        return self._state_icons.get(key, DEFAULT_STATE_ACTIVE_ICON)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        target = self.hass.states.get(self._target_entity)
        return {
            ATTR_TARGET_ENTITY: self._target_entity,
            ATTR_TARGET_STATE: target.state if target else None,
            ATTR_ON_DATETIME: self._on_dt.isoformat() if self._on_dt else None,
            ATTR_OFF_DATETIME: self._off_dt.isoformat() if self._off_dt else None,
            ATTR_NEXT_EVENT: (
                self._next_event().isoformat() if self._next_event() else None
            ),
            ATTR_DIRECTION: self._direction,
            ATTR_STATE_NAMES: dict(self._state_names),
            ATTR_STATE_ICONS: dict(self._state_icons),
        }

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Activate the timer."""
        self._active = True
        self._schedule_timers()
        await self._apply_current_window(reason="activated")
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Deactivate the timer. Does not touch the target device."""
        self._active = False
        self._cancel_timers()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Public: called by the set_timer service
    # ------------------------------------------------------------------

    async def async_set_timer(
        self, on_dt: datetime | None, off_dt: datetime | None
    ) -> None:
        """Replace on/off datetimes and reschedule if active."""
        self._on_dt = on_dt
        self._off_dt = off_dt
        if self._active:
            self._cancel_timers()
            self._schedule_timers()
            await self._apply_current_window(reason="set_timer update")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Internal: scheduling + target control
    # ------------------------------------------------------------------

    def _schedule_timers(self) -> None:
        """Register async_track_point_in_time callbacks for on/off times.

        Honors direction: off_only skips the on-callback, on_only skips
        the off-callback.
        """
        self._cancel_timers()
        now = dt_util.now()
        want_on = self._direction != TIMER_DIRECTION_OFF_ONLY
        want_off = self._direction != TIMER_DIRECTION_ON_ONLY

        if want_on and self._on_dt and self._on_dt > now:
            self._on_unsub = async_track_point_in_time(
                self.hass, self._handle_on_fire, self._on_dt
            )
        if want_off and self._off_dt and self._off_dt > now:
            self._off_unsub = async_track_point_in_time(
                self.hass, self._handle_off_fire, self._off_dt
            )

    def _cancel_timers(self) -> None:
        if self._on_unsub:
            self._on_unsub()
            self._on_unsub = None
        if self._off_unsub:
            self._off_unsub()
            self._off_unsub = None

    async def _apply_current_window(self, reason: str) -> None:
        """If now falls inside an active [on, off] window, turn target on.

        Skipped for off_only direction (no on-side action).
        """
        if not self._active or self._direction == TIMER_DIRECTION_OFF_ONLY:
            return
        if not self._on_dt:
            return
        now = dt_util.now()
        if now < self._on_dt:
            return
        if self._off_dt is not None and now >= self._off_dt:
            # Window already completely in the past → self-deactivate.
            self._active = False
            self._cancel_timers()
            return
        await self._turn_target_on(reason=f"in-window ({reason})")

    @callback
    def _handle_on_fire(self, _fire_time: datetime) -> None:
        self._on_unsub = None
        if not self._active:
            return

        async def _run() -> None:
            await self._turn_target_on(reason="on timer fired")
            # On-only direction is one-shot: deactivate after firing.
            if self._direction == TIMER_DIRECTION_ON_ONLY:
                self._active = False
                self._cancel_timers()
            self.async_write_ha_state()

        self.hass.async_create_task(_run())

    @callback
    def _handle_off_fire(self, _fire_time: datetime) -> None:
        self._off_unsub = None
        if not self._active:
            return

        async def _run() -> None:
            await self._turn_target_off(reason="off timer fired")
            # One-shot semantics: deactivate after the off-boundary passed.
            self._active = False
            self._cancel_timers()
            self.async_write_ha_state()

        self.hass.async_create_task(_run())

    # ----- Generic target action (select.select_option vs turn_on/off) -----

    def _is_select_target(self) -> bool:
        if not self._target_entity or "." not in self._target_entity:
            return False
        return self._target_entity.split(".", 1)[0] in ("select", "input_select")

    async def _apply_target_action(self, side: str, reason: str) -> None:
        """Drive the target device for an 'on' or 'off' boundary event.

        For switch/light/input_boolean/fan/climate: call
        `homeassistant.turn_on` / `turn_off`.
        For select/input_select: call `<domain>.select_option` with the
        configured option (skips the call if no option was configured).
        """
        if not self._target_entity:
            return

        if self._is_select_target():
            stored = self._on_option if side == "on" else self._off_option
            if not stored:
                _LOGGER.debug(
                    "Smart Timer '%s' has no %s-option configured for select "
                    "target %s — skipping (%s)",
                    self._attr_name, side, self._target_entity, reason,
                )
                return
            target = self.hass.states.get(self._target_entity)
            # Rename resilience for PowerPilz Smart Schedule targets: if
            # the stored value is a logical key (off/on/auto) and the
            # target exposes `mode_names`, resolve to the current display
            # name. Otherwise use `stored` verbatim (generic select).
            option = stored
            if target is not None:
                mode_names = target.attributes.get("mode_names")
                if isinstance(mode_names, dict) and stored in mode_names:
                    display = mode_names.get(stored)
                    if isinstance(display, str) and display:
                        option = display
            if target is not None and target.state == option:
                return
            domain = self._target_entity.split(".", 1)[0]
            _LOGGER.debug(
                "Smart Timer '%s' → %s.select_option %s=%s (%s)",
                self._attr_name, domain, self._target_entity, option, reason,
            )
            try:
                await self.hass.services.async_call(
                    domain,
                    "select_option",
                    {"entity_id": self._target_entity, "option": option},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to set %s to option %s: %s",
                    self._target_entity, option, err,
                )
            return

        # Generic switch-like target.
        desired_state = STATE_ON if side == "on" else STATE_OFF
        target = self.hass.states.get(self._target_entity)
        if target is not None and target.state == desired_state:
            return
        service = "turn_on" if side == "on" else "turn_off"
        _LOGGER.debug(
            "Smart Timer '%s' → homeassistant.%s %s (%s)",
            self._attr_name, service, self._target_entity, reason,
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
                "Failed to %s %s: %s", service, self._target_entity, err
            )

    # Back-compat shim methods used by the existing fire handlers.
    async def _turn_target_on(self, reason: str) -> None:
        await self._apply_target_action("on", reason)

    async def _turn_target_off(self, reason: str) -> None:
        await self._apply_target_action("off", reason)

    def _next_event(self) -> datetime | None:
        now = dt_util.now()
        candidates: list[datetime] = []
        if self._on_dt and self._on_dt > now:
            candidates.append(self._on_dt)
        if self._off_dt and self._off_dt > now:
            candidates.append(self._off_dt)
        return min(candidates) if candidates else None
