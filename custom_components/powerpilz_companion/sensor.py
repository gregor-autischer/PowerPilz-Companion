"""Sensor platform for Smart Curve helper.

Exposes the currently computed curve value as a numeric sensor so it
can be charted, used as a trigger source or referenced in templates
without reading the parent select's attribute dict.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from datetime import timedelta

from .const import (
    ATTR_CURRENT_VALUE,
    ATTR_TODAY_POINTS,
    ATTR_UNIT,
    ATTR_UPDATE_INTERVAL,
    CONF_ENTRY_TYPE,
    CONF_NAME,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UNIT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_CURVE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.options.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_CURVE:
        return
    async_add_entities([SmartCurveSetpointSensor(entry)])


class SmartCurveSetpointSensor(SensorEntity):
    """Live setpoint value of a Smart Curve helper."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        config = {**entry.data, **entry.options}
        base_name = str(config.get(CONF_NAME) or "Smart Curve").strip()
        self._attr_name = f"{base_name} setpoint"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_setpoint"
        self._attr_icon = "mdi:thermometer-lines"
        unit = str(config.get(CONF_UNIT, DEFAULT_UNIT))
        self._attr_native_unit_of_measurement = unit
        # If the user picked a non-temperature unit, drop the temperature
        # device class so HA doesn't complain about a unit mismatch.
        if unit not in ("°C", "°F", "K"):
            self._attr_device_class = None

        self._update_interval: int = max(
            1, int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )
        self._parent_entity_id: str | None = None
        self._unsub_tick: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._wire_parent_listener()
        # Tick on the same cadence as the parent so the curve sample
        # advances visibly even between mode changes.
        self._unsub_tick = async_track_time_interval(
            self.hass,
            self._on_tick,
            timedelta(minutes=self._update_interval),
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        await super().async_will_remove_from_hass()

    @callback
    def _on_tick(self, _now: Any) -> None:
        self.async_write_ha_state()

    @callback
    def _wire_parent_listener(self, _now: Any = None) -> None:
        if self._parent_entity_id is not None:
            return
        parent = self._resolve_parent()
        if parent is None:
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
        self.async_write_ha_state()

    def _resolve_parent(self) -> Any | None:
        bucket = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if not isinstance(bucket, dict):
            return None
        return bucket.get("entity")

    @callback
    def _parent_changed(self, _event: Event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        parent = self._resolve_parent()
        if parent is None:
            return None
        try:
            return parent._compute_current_curve_value()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parent = self._resolve_parent()
        if parent is None:
            return {}
        try:
            src = parent.extra_state_attributes
        except Exception:  # noqa: BLE001
            return {}
        return {
            ATTR_CURRENT_VALUE: src.get(ATTR_CURRENT_VALUE),
            ATTR_TODAY_POINTS: src.get(ATTR_TODAY_POINTS),
            ATTR_UNIT: src.get(ATTR_UNIT),
            ATTR_UPDATE_INTERVAL: src.get(ATTR_UPDATE_INTERVAL),
            "companion_entity": parent.entity_id,
        }
