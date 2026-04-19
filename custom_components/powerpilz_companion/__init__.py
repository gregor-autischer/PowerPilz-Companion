"""PowerPilz Companion integration.

Hosts two helper kinds under one domain:

- **Smart Schedule** → a `select` entity with 3 modes linked to a native
  HA schedule helper (auto-created on setup).
- **Smart Timer** → a `switch` entity that autonomously drives a target
  device at configured on/off datetimes.

The `entry_type` field in the config entry options decides which
platform is used for a given entry.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    CONF_LINKED_SCHEDULE,
    DOMAIN,
    ENTRY_TYPE_SCHEDULE,
    ENTRY_TYPE_TIMER,
    SERVICE_SET_TIMER,
)
from .schedule_linker import async_remove_linked_schedule

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


def _platforms_for(entry: ConfigEntry) -> list[Platform]:
    if entry.options.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TIMER:
        return [Platform.SWITCH]
    return [Platform.SELECT]


SET_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("on"): cv.string,
        vol.Optional("off"): cv.string,
    }
)


def _parse_service_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Accept "YYYY-MM-DD HH:MM(:SS)" and ISO forms.
    candidates = [raw, raw.replace(" ", "T")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = dt_util.as_local(parsed)
            return dt_util.as_local(parsed)
        except ValueError:
            continue
    return None


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})

    async def handle_set_timer(call: ServiceCall) -> None:
        entity_id: str = call.data["entity_id"]
        on_raw = call.data.get("on")
        off_raw = call.data.get("off")

        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if not entry or entry.platform != DOMAIN:
            _LOGGER.warning(
                "set_timer called for unknown or non-PowerPilz entity: %s",
                entity_id,
            )
            return

        timer_entity = None
        for stored in hass.data.get(DOMAIN, {}).values():
            if not isinstance(stored, dict):
                continue
            candidate = stored.get("timer_entity")
            if candidate and candidate.unique_id == entry.unique_id:
                timer_entity = candidate
                break

        if timer_entity is None:
            _LOGGER.warning(
                "No live Smart Timer entity found for %s", entity_id
            )
            return

        on_dt = _parse_service_datetime(on_raw) if on_raw is not None else ...
        off_dt = _parse_service_datetime(off_raw) if off_raw is not None else ...

        # `...` sentinel means "field not provided, keep existing value".
        # `None` means "clear it".
        new_on = timer_entity._on_dt if on_dt is ... else on_dt  # noqa: SLF001
        new_off = timer_entity._off_dt if off_dt is ... else off_dt  # noqa: SLF001
        await timer_entity.async_set_timer(new_on, new_off)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_TIMER, handle_set_timer, schema=SET_TIMER_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(
        entry, _platforms_for(entry)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, _platforms_for(entry)
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Clean up side effects when a Smart helper is removed."""
    entry_type = entry.options.get(CONF_ENTRY_TYPE, ENTRY_TYPE_SCHEDULE)
    if entry_type != ENTRY_TYPE_SCHEDULE:
        return

    linked = entry.options.get(CONF_LINKED_SCHEDULE) or entry.data.get(
        CONF_LINKED_SCHEDULE
    )
    if not linked:
        return
    try:
        removed = await async_remove_linked_schedule(hass, linked)
        if removed:
            _LOGGER.info(
                "Removed auto-linked schedule helper %s after entry %s deletion",
                linked,
                entry.title,
            )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Failed to remove linked schedule %s: %s", linked, err)
