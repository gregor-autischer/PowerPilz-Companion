"""PowerPilz Companion integration.

Provides a "Smart Schedule" helper: a select entity that wraps three
override modes (Off / On / Auto) around a target switch/light and follows
a user-picked native Home Assistant Schedule helper for the weekly plan.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_LINKED_SCHEDULE, DOMAIN
from .schedule_linker import async_remove_linked_schedule

# Empty config schema so HA is happy if `powerpilz_companion:` appears in
# configuration.yaml (we have nothing to read there).
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize the integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a PowerPilz Smart Schedule from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when config entry options change (e.g. new mode names)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up the auto-created native schedule helper when our helper is
    deleted from HA's Helpers list."""
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
