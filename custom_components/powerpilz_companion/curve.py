"""Select entity for the PowerPilz Smart Curve helper.

A Smart Curve helper bundles:
- A weekly heating curve (per-day list of {time, value} control points)
- A list of target entities (climate / number) the interpolated value is
  written to at a configurable cadence (default 15 min)
- Three override modes (Off / On / Auto) — same UX as Smart Schedule

In Auto mode the entity samples the curve at "now" using monotone-cubic
(PCHIP) interpolation and writes the result to every target. Off turns
climate targets off via `climate.turn_off` and skips number targets
(setpoint-numbers don't have a meaningful "off"). On writes a fixed
configurable boost value.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CURRENT_VALUE,
    ATTR_CURVE_POINTS,
    ATTR_LOGICAL_MODE,
    ATTR_MODE_ICONS,
    ATTR_MODE_NAMES,
    ATTR_MODE_ON_VALUE,
    ATTR_SAME_FOR_ALL_DAYS,
    ATTR_TARGET_ENTITIES,
    ATTR_TODAY_POINTS,
    ATTR_UNIT,
    ATTR_UPDATE_INTERVAL,
    ATTR_VALUE_MAX,
    ATTR_VALUE_MIN,
    CONF_ENTRY_TYPE,
    CONF_MODE_AUTO_ICON,
    CONF_MODE_AUTO_NAME,
    CONF_MODE_OFF_ICON,
    CONF_MODE_OFF_NAME,
    CONF_MODE_ON_ICON,
    CONF_MODE_ON_NAME,
    CONF_MODE_ON_VALUE,
    CONF_NAME,
    CONF_SAME_FOR_ALL_DAYS,
    CONF_TARGET_ENTITIES,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    CONF_VALUE_MAX,
    CONF_VALUE_MIN,
    DEFAULT_MODE_AUTO_ICON,
    DEFAULT_MODE_AUTO_NAME,
    DEFAULT_MODE_OFF_ICON,
    DEFAULT_MODE_OFF_NAME,
    DEFAULT_MODE_ON_ICON,
    DEFAULT_MODE_ON_NAME,
    DEFAULT_MODE_ON_VALUE,
    DEFAULT_UNIT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VALUE_MAX,
    DEFAULT_VALUE_MIN,
    DOMAIN,
    ENTRY_TYPE_CURVE,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    WEEKDAY_KEYS,
)
from .storage import async_load_curve

_LOGGER = logging.getLogger(__name__)

DAY_SECONDS = 24 * 3600


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Smart Curve select entity from a config entry."""
    if entry.options.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_CURVE:
        return
    points = await async_load_curve(hass, entry.entry_id)
    async_add_entities([SmartCurveSelect(entry, points)])


class SmartCurveSelect(SelectEntity, RestoreEntity):
    """Smart Curve helper — select with 3 modes + weekly point curve."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        points: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}

        name = str(config.get(CONF_NAME) or "Smart Curve").strip()
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}"
        self._attr_icon = "mdi:chart-bell-curve-cumulative"

        targets = config.get(CONF_TARGET_ENTITIES, [])
        if isinstance(targets, str):
            targets = [targets]
        self._target_entities: list[str] = [
            t for t in targets if isinstance(t, str) and t
        ]

        self._update_interval: int = max(
            1, int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )
        self._value_min: float = float(
            config.get(CONF_VALUE_MIN, DEFAULT_VALUE_MIN)
        )
        self._value_max: float = float(
            config.get(CONF_VALUE_MAX, DEFAULT_VALUE_MAX)
        )
        if self._value_max <= self._value_min:
            self._value_max = self._value_min + 1.0
        self._unit: str = str(config.get(CONF_UNIT, DEFAULT_UNIT))
        self._mode_on_value: float = float(
            config.get(CONF_MODE_ON_VALUE, DEFAULT_MODE_ON_VALUE)
        )
        self._same_for_all_days: bool = bool(
            config.get(CONF_SAME_FOR_ALL_DAYS, False)
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

        self._logical_mode: str = MODE_AUTO
        self._points: dict[str, list[dict[str, Any]]] = points
        self._last_written_value: float | None = None
        self._unsub_interval: CALLBACK_TYPE | None = None

        self._attr_options = [
            self._mode_names[MODE_OFF],
            self._mode_names[MODE_ON],
            self._mode_names[MODE_AUTO],
        ]

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

        hass_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = hass_data.setdefault(self._entry.entry_id, {})
        entry_data["entity"] = self

        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._on_interval,
            timedelta(minutes=self._update_interval),
        )

        await self._apply_current_mode(reason="entity added")
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
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
        return self._mode_icons.get(self._logical_mode, "mdi:chart-bell-curve-cumulative")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        current = self._compute_current_curve_value()
        today_points = self._points_for_today()
        return {
            ATTR_LOGICAL_MODE: self._logical_mode,
            ATTR_TARGET_ENTITIES: list(self._target_entities),
            ATTR_MODE_NAMES: dict(self._mode_names),
            ATTR_MODE_ICONS: dict(self._mode_icons),
            ATTR_CURVE_POINTS: self._points,
            ATTR_TODAY_POINTS: today_points,
            ATTR_CURRENT_VALUE: current,
            ATTR_VALUE_MIN: self._value_min,
            ATTR_VALUE_MAX: self._value_max,
            ATTR_UNIT: self._unit,
            ATTR_UPDATE_INTERVAL: self._update_interval,
            ATTR_SAME_FOR_ALL_DAYS: self._same_for_all_days,
            ATTR_MODE_ON_VALUE: self._mode_on_value,
            "companion_entity": self.entity_id,
        }

    async def async_select_option(self, option: str) -> None:
        logical = self._display_name_to_logical(option)
        if logical is None:
            _LOGGER.warning("Unknown mode selected: %s", option)
            return
        self._logical_mode = logical
        await self._apply_current_mode(reason=f"mode set to {logical}")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Public API used by the set_curve_points service
    # ------------------------------------------------------------------

    async def async_update_points(
        self, points: dict[str, list[dict[str, Any]]]
    ) -> None:
        self._points = points
        await self._apply_current_mode(reason="points updated")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Curve evaluation
    # ------------------------------------------------------------------

    def _weekday_key_for(self, dt_obj: datetime) -> str:
        return WEEKDAY_KEYS[dt_obj.weekday()]

    def _points_for_day(
        self, dt_obj: datetime
    ) -> list[dict[str, Any]]:
        key = (
            WEEKDAY_KEYS[0]
            if self._same_for_all_days
            else self._weekday_key_for(dt_obj)
        )
        return list(self._points.get(key, []))

    def _points_for_today(self) -> list[dict[str, Any]]:
        return self._points_for_day(dt_util.now())

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
        if total < 0 or total > DAY_SECONDS:
            return None
        return total

    def _today_seconds(self) -> int:
        now = dt_util.now()
        return now.hour * 3600 + now.minute * 60 + now.second

    def _compute_current_curve_value(self) -> float | None:
        """Sample the curve at "now" using monotone-cubic interpolation.

        - 0 points → None (no value)
        - 1 point  → constant
        - 2 points → linear (degenerate cubic)
        - n points → Fritsch-Carlson PCHIP, exact through every point
        """
        points = self._points_for_today()
        if not points:
            return None

        sampled = []
        for p in points:
            t = self._parse_hms_to_seconds(p.get("time"))
            v = p.get("value")
            if t is None or not isinstance(v, (int, float)):
                continue
            sampled.append((float(t), float(v)))
        if not sampled:
            return None
        sampled.sort(key=lambda pt: pt[0])

        if len(sampled) == 1:
            return self._clamp(sampled[0][1])

        x_now = float(self._today_seconds())
        if x_now <= sampled[0][0]:
            return self._clamp(sampled[0][1])
        if x_now >= sampled[-1][0]:
            return self._clamp(sampled[-1][1])

        return self._clamp(_pchip_interpolate(sampled, x_now))

    def _clamp(self, v: float) -> float:
        return max(self._value_min, min(self._value_max, float(v)))

    @callback
    def _on_interval(self, _now: datetime) -> None:
        self.hass.async_create_task(
            self._apply_current_mode(reason="interval tick")
        )

    # ------------------------------------------------------------------
    # Apply to targets
    # ------------------------------------------------------------------

    async def _apply_current_mode(self, reason: str) -> None:
        if self._logical_mode == MODE_OFF:
            await self._apply_off()
            self._last_written_value = None
            return

        if self._logical_mode == MODE_ON:
            value = self._clamp(self._mode_on_value)
        else:  # MODE_AUTO
            curve = self._compute_current_curve_value()
            if curve is None:
                _LOGGER.debug(
                    "Curve has no points for today — skipping write (%s)",
                    reason,
                )
                return
            value = curve

        await self._write_value(value, reason)
        self._last_written_value = value

    async def _apply_off(self) -> None:
        for target in self._target_entities:
            domain = target.split(".", 1)[0] if "." in target else ""
            if domain != "climate":
                # Setpoint-numbers don't have a sensible "off" — skip.
                continue
            state = self.hass.states.get(target)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            try:
                await self.hass.services.async_call(
                    "climate",
                    "turn_off",
                    {"entity_id": target},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("turn_off %s failed: %s", target, err)

    async def _write_value(self, value: float, reason: str) -> None:
        rounded = round(value, 2)
        for target in self._target_entities:
            if "." not in target:
                continue
            domain = target.split(".", 1)[0]
            state = self.hass.states.get(target)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                _LOGGER.debug(
                    "Target %s unavailable — skipping (%s)", target, reason
                )
                continue
            try:
                if domain == "climate":
                    await self.hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {"entity_id": target, "temperature": rounded},
                        blocking=False,
                    )
                elif domain in ("number", "input_number"):
                    await self.hass.services.async_call(
                        domain,
                        "set_value",
                        {"entity_id": target, "value": rounded},
                        blocking=False,
                    )
                else:
                    _LOGGER.debug(
                        "Unsupported curve target domain %s — skipped", domain
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Writing curve value to %s failed: %s", target, err
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


# ----------------------------------------------------------------------
# Monotone cubic (Fritsch-Carlson PCHIP) interpolation
# ----------------------------------------------------------------------


def _pchip_interpolate(
    points: list[tuple[float, float]], x: float
) -> float:
    """Evaluate a monotone cubic Hermite spline at x.

    points must be sorted by x and contain at least 2 entries. Caller
    guarantees points[0].x < x < points[-1].x.
    """
    n = len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    # Locate segment.
    i = 0
    while i < n - 2 and x >= xs[i + 1]:
        i += 1

    h = [xs[k + 1] - xs[k] for k in range(n - 1)]
    delta = [
        (ys[k + 1] - ys[k]) / h[k] if h[k] != 0 else 0.0
        for k in range(n - 1)
    ]

    m = [0.0] * n
    if n == 2:
        m[0] = delta[0]
        m[1] = delta[0]
    else:
        # Fritsch-Carlson tangents at interior points.
        for k in range(1, n - 1):
            if delta[k - 1] == 0 or delta[k] == 0 or (
                delta[k - 1] > 0
            ) != (delta[k] > 0):
                m[k] = 0.0
            else:
                w1 = 2 * h[k] + h[k - 1]
                w2 = h[k] + 2 * h[k - 1]
                m[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])
        # End tangents (one-sided three-point).
        m[0] = _end_tangent(h[0], h[1] if n > 2 else h[0], delta[0], delta[1] if n > 2 else delta[0])
        m[n - 1] = _end_tangent(
            h[n - 2],
            h[n - 3] if n > 2 else h[n - 2],
            delta[n - 2],
            delta[n - 3] if n > 2 else delta[n - 2],
        )

    # Hermite basis.
    hk = h[i]
    if hk == 0:
        return ys[i]
    t = (x - xs[i]) / hk
    t2 = t * t
    t3 = t2 * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return (
        h00 * ys[i]
        + h10 * hk * m[i]
        + h01 * ys[i + 1]
        + h11 * hk * m[i + 1]
    )


def _end_tangent(h0: float, h1: float, d0: float, d1: float) -> float:
    """One-sided non-overshooting end tangent (Fritsch-Carlson)."""
    if h0 + h1 == 0:
        return 0.0
    m = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if (m > 0) != (d0 > 0):
        return 0.0
    if (d0 > 0) != (d1 > 0) and abs(m) > abs(3 * d0):
        return 3 * d0
    return m
