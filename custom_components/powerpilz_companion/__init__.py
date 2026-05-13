"""PowerPilz Companion integration.

Hosts two helper kinds under one domain:

- **Smart Schedule** → a `select` entity with 3 modes + a companion
  `binary_sensor` exposing the currently-active state. Weekly schedule
  blocks are stored natively by this integration (no external
  `schedule.*` helper needed).
- **Smart Timer** → a `switch` entity that autonomously drives a target
  device at configured on/off datetimes.

The `entry_type` field in the config entry options decides which
platforms are loaded for a given entry.
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
    ENTRY_TYPE_CURVE,
    ENTRY_TYPE_SCHEDULE,
    ENTRY_TYPE_TIMER,
    SERVICE_SET_CURVE_POINTS,
    SERVICE_SET_SCHEDULE_BLOCKS,
    SERVICE_SET_TIMER,
    WEEKDAY_KEYS,
)
from .storage import (
    async_delete_curve_entry as async_delete_curve_storage_entry,
    async_delete_entry as async_delete_storage_entry,
    async_load_blocks,
    async_migrate_from_schedule_entity,
    async_save_blocks,
    async_save_curve,
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


def _platforms_for(entry: ConfigEntry) -> list[Platform]:
    entry_type = entry.options.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_TIMER:
        return [Platform.SWITCH]
    if entry_type == ENTRY_TYPE_CURVE:
        return [Platform.SELECT, Platform.SENSOR]
    return [Platform.SELECT, Platform.BINARY_SENSOR]


# ---------------------------------------------------------------------------
# Service schemas
# ---------------------------------------------------------------------------


SET_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("on"): cv.string,
        vol.Optional("off"): cv.string,
        # For select/input_select targets: which option to apply at
        # the on / off boundary. Overrides whatever was configured in
        # the helper's Options flow for this and future activations
        # (survives restarts via RestoreEntity). Omit to leave
        # unchanged; pass an empty string to clear.
        vol.Optional("on_option"): cv.string,
        vol.Optional("off_option"): cv.string,
    }
)


_DAY_SCHEMA = vol.Schema(
    [
        vol.Schema(
            {
                vol.Required("from"): cv.string,
                vol.Required("to"): cv.string,
                vol.Optional("data"): dict,
            },
            extra=vol.REMOVE_EXTRA,
        )
    ]
)


SET_SCHEDULE_BLOCKS_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("blocks"): vol.Schema(
            {vol.Optional(day): _DAY_SCHEMA for day in WEEKDAY_KEYS},
            extra=vol.REMOVE_EXTRA,
        ),
    }
)


_CURVE_DAY_SCHEMA = vol.Schema(
    [
        vol.Schema(
            {
                vol.Required("time"): cv.string,
                vol.Required("value"): vol.Coerce(float),
            },
            extra=vol.REMOVE_EXTRA,
        )
    ]
)


SET_CURVE_POINTS_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("points"): vol.Schema(
            {vol.Optional(day): _CURVE_DAY_SCHEMA for day in WEEKDAY_KEYS},
            extra=vol.REMOVE_EXTRA,
        ),
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


# ---------------------------------------------------------------------------
# Entry resolution helpers
# ---------------------------------------------------------------------------


def _find_schedule_entity_for(
    hass: HomeAssistant, entity_id: str
) -> tuple[ConfigEntry | None, Any | None]:
    """Given the entity_id of a Smart Schedule / Curve select, return the
    entry and live entity object, or (None, None) if it can't be resolved."""
    registry = er.async_get(hass)
    entry_reg = registry.async_get(entity_id)
    if not entry_reg or entry_reg.platform != DOMAIN or not entry_reg.config_entry_id:
        return None, None

    config_entry = hass.config_entries.async_get_entry(entry_reg.config_entry_id)
    bucket = hass.data.get(DOMAIN, {}).get(entry_reg.config_entry_id, {})
    return config_entry, bucket.get("entity") if isinstance(bucket, dict) else None


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


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

        # Same sentinel logic for on_option / off_option: "" clears,
        # omitted keeps existing.
        on_option_raw = call.data.get("on_option")
        off_option_raw = call.data.get("off_option")
        new_on_option = (
            timer_entity._on_option  # noqa: SLF001
            if on_option_raw is None
            else (on_option_raw or None)
        )
        new_off_option = (
            timer_entity._off_option  # noqa: SLF001
            if off_option_raw is None
            else (off_option_raw or None)
        )

        await timer_entity.async_set_timer(
            new_on, new_off, new_on_option, new_off_option
        )

    async def handle_set_schedule_blocks(call: ServiceCall) -> None:
        entity_id: str = call.data["entity_id"]
        blocks: dict[str, list[dict[str, Any]]] = call.data["blocks"]

        config_entry, entity = _find_schedule_entity_for(hass, entity_id)
        if config_entry is None:
            _LOGGER.warning(
                "set_schedule_blocks: %s is not a PowerPilz Smart Schedule entity",
                entity_id,
            )
            return

        saved = await async_save_blocks(hass, config_entry.entry_id, blocks)
        if entity is not None and hasattr(entity, "async_update_blocks"):
            await entity.async_update_blocks(saved)
        else:
            _LOGGER.debug(
                "set_schedule_blocks: entity for %s not live yet; blocks "
                "persisted and will load on next setup.",
                entity_id,
            )

    async def handle_set_curve_points(call: ServiceCall) -> None:
        entity_id: str = call.data["entity_id"]
        points: dict[str, list[dict[str, Any]]] = call.data["points"]

        config_entry, entity = _find_schedule_entity_for(hass, entity_id)
        if config_entry is None:
            _LOGGER.warning(
                "set_curve_points: %s is not a PowerPilz helper entity",
                entity_id,
            )
            return

        saved = await async_save_curve(hass, config_entry.entry_id, points)
        if entity is not None and hasattr(entity, "async_update_points"):
            await entity.async_update_points(saved)
        else:
            _LOGGER.debug(
                "set_curve_points: entity for %s not live yet; points "
                "persisted and will load on next setup.",
                entity_id,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_TIMER, handle_set_timer, schema=SET_TIMER_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE_BLOCKS,
        handle_set_schedule_blocks,
        schema=SET_SCHEDULE_BLOCKS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CURVE_POINTS,
        handle_set_curve_points,
        schema=SET_CURVE_POINTS_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # One-shot migration from the legacy v0.3 linked-schedule model: if
    # the entry still carries `linked_schedule` and our store doesn't
    # have blocks for it, read the native schedule helper and import
    # its weekly plan. Then clear the config reference.
    _entry_type = entry.options.get(CONF_ENTRY_TYPE, ENTRY_TYPE_SCHEDULE)
    if _entry_type == ENTRY_TYPE_SCHEDULE:
        legacy_link = entry.options.get(CONF_LINKED_SCHEDULE) or entry.data.get(
            CONF_LINKED_SCHEDULE
        )
        if isinstance(legacy_link, str) and legacy_link:
            try:
                imported = await async_migrate_from_schedule_entity(
                    hass, entry.entry_id, legacy_link
                )
                if imported:
                    _LOGGER.info(
                        "Migrated entry %s: imported blocks from %s",
                        entry.title,
                        legacy_link,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Migration from %s failed: %s", legacy_link, err
                )
            # Drop the legacy key from options so it doesn't mislead
            # future code paths.
            new_options = {
                k: v for k, v in entry.options.items() if k != CONF_LINKED_SCHEDULE
            }
            if new_options != entry.options:
                hass.config_entries.async_update_entry(entry, options=new_options)

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

    if entry_type == ENTRY_TYPE_SCHEDULE:
        try:
            await async_delete_storage_entry(hass, entry.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to clean up schedule blocks for %s: %s",
                entry.title,
                err,
            )
    elif entry_type == ENTRY_TYPE_CURVE:
        try:
            await async_delete_curve_storage_entry(hass, entry.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to clean up curve points for %s: %s",
                entry.title,
                err,
            )
