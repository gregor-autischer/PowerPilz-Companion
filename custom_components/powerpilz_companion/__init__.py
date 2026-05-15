"""PowerPilz Companion integration.

Hosts four helper kinds under one domain:

- **Smart Schedule** → `select` entity with 3 modes (Off/On/Auto) and
  a weekly blocks plan stored natively. The `schedule_active` flag is
  exposed as an attribute on the select — consumers either trigger on
  that attribute or build a template `binary_sensor` on top.
- **Smart Event Schedule** → `select` entity (2 modes) plus a companion
  `button.*_trigger` that records every event fire (scheduled or manual)
  in HA history.
- **Smart Timer** → `switch` entity that autonomously drives a target
  device at configured on/off datetimes.
- **Smart Curve** → `select` + `sensor` for weekly value curves applied
  to one or more climate / number targets.

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
    CONF_SCHEDULE_KIND,
    DOMAIN,
    ENTRY_TYPE_CURVE,
    ENTRY_TYPE_EVENT_SCHEDULE,
    ENTRY_TYPE_SCHEDULE,
    ENTRY_TYPE_TIMER,
    SCHEDULE_KIND_EVENTS,
    SERVICE_SET_CURVE_POINTS,
    SERVICE_SET_SCHEDULE_BLOCKS,
    SERVICE_SET_SCHEDULE_EVENTS,
    SERVICE_SET_TIMER,
    SERVICE_TRIGGER_EVENT_NOW,
    WEEKDAY_KEYS,
)
from .storage import (
    async_delete_curve_entry as async_delete_curve_storage_entry,
    async_delete_entry as async_delete_storage_entry,
    async_load_blocks,
    async_migrate_from_schedule_entity,
    async_save_blocks,
    async_save_curve,
    async_save_events,
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


def _platforms_for(entry: ConfigEntry) -> list[Platform]:
    entry_type = entry.options.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_TIMER:
        return [Platform.SWITCH]
    if entry_type == ENTRY_TYPE_CURVE:
        return [Platform.SELECT, Platform.SENSOR]
    if entry_type == ENTRY_TYPE_EVENT_SCHEDULE:
        return [Platform.SELECT, Platform.BUTTON]
    # Default: schedule (blocks). Just the select entity; consumers use
    # the `schedule_active` attribute trigger or build their own
    # template binary_sensor on top of it.
    return [Platform.SELECT]


def _migrate_legacy_event_schedule_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """One-shot migration for entries created before the schedule split.

    Before the split, events-mode helpers had `entry_type == "schedule"`
    plus `options.schedule_kind == "events"`. This routine flips them to
    the new `entry_type == "event_schedule"` and strips the legacy
    `schedule_kind` field. Idempotent: safe to call on every startup.

    Returns True if the entry was migrated.
    """
    options = dict(entry.options or {})
    is_legacy_events = (
        options.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SCHEDULE
        and options.get(CONF_SCHEDULE_KIND) == SCHEDULE_KIND_EVENTS
    )
    if not is_legacy_events:
        return False
    options[CONF_ENTRY_TYPE] = ENTRY_TYPE_EVENT_SCHEDULE
    options.pop(CONF_SCHEDULE_KIND, None)
    hass.config_entries.async_update_entry(entry, options=options)

    _LOGGER.info(
        "Migrated %s to event_schedule entry type",
        entry.title,
    )
    return True


def _purge_orphan_binary_sensors(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove any `binary_sensor.*_active` left behind by older versions.

    The blocks-mode schedule helper used to spawn a companion
    `binary_sensor.*_active` entity that mirrored the `schedule_active`
    flag. As of v0.7 we no longer load the binary_sensor platform —
    consumers read the flag from the select's attribute or build their
    own template binary_sensor. This sweep removes the orphaned
    entries from the entity registry so they don't stay "unavailable"
    forever. Idempotent; safe to call on every startup.
    """
    registry = er.async_get(hass)
    removed: list[str] = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.platform == DOMAIN and reg_entry.entity_id.startswith("binary_sensor."):
            registry.async_remove(reg_entry.entity_id)
            removed.append(reg_entry.entity_id)
    if removed:
        _LOGGER.warning(
            "Removed %d orphan binary_sensor(s) for %s: %s",
            len(removed),
            entry.title,
            ", ".join(removed),
        )


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


_EVENT_DAY_SCHEMA = vol.Schema(
    [
        vol.Schema(
            {
                vol.Required("time"): cv.string,
                vol.Optional("data"): dict,
            },
            extra=vol.REMOVE_EXTRA,
        )
    ]
)


SET_SCHEDULE_EVENTS_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("events"): vol.Schema(
            {vol.Optional(day): _EVENT_DAY_SCHEMA for day in WEEKDAY_KEYS},
            extra=vol.REMOVE_EXTRA,
        ),
    }
)


TRIGGER_EVENT_NOW_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
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

    async def handle_set_schedule_events(call: ServiceCall) -> None:
        entity_id: str = call.data["entity_id"]
        events: dict[str, list[dict[str, Any]]] = call.data["events"]

        config_entry, entity = _find_schedule_entity_for(hass, entity_id)
        if config_entry is None:
            _LOGGER.warning(
                "set_schedule_events: %s is not a PowerPilz Smart Schedule entity",
                entity_id,
            )
            return

        saved = await async_save_events(hass, config_entry.entry_id, events)
        if entity is not None and hasattr(entity, "async_update_events"):
            await entity.async_update_events(saved)
        else:
            _LOGGER.debug(
                "set_schedule_events: entity for %s not live yet; events "
                "persisted and will load on next setup.",
                entity_id,
            )

    async def handle_trigger_event_now(call: ServiceCall) -> None:
        entity_id: str = call.data["entity_id"]
        _, entity = _find_schedule_entity_for(hass, entity_id)
        if entity is None or not hasattr(entity, "async_trigger_event_now"):
            _LOGGER.warning(
                "trigger_event_now: %s is not a live PowerPilz Smart Schedule entity",
                entity_id,
            )
            return
        await entity.async_trigger_event_now()

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE_EVENTS,
        handle_set_schedule_events,
        schema=SET_SCHEDULE_EVENTS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRIGGER_EVENT_NOW,
        handle_trigger_event_now,
        schema=TRIGGER_EVENT_NOW_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # One-shot migration: legacy entries with schedule_kind=events get
    # promoted to the new ENTRY_TYPE_EVENT_SCHEDULE entry type.
    _migrate_legacy_event_schedule_entry(hass, entry)

    # Sweep orphan binary_sensor.*_active entities — the platform is
    # no longer loaded as of v0.7 (use the select's schedule_active
    # attribute trigger instead, or roll a template binary_sensor).
    _purge_orphan_binary_sensors(hass, entry)

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

    if entry_type in (ENTRY_TYPE_SCHEDULE, ENTRY_TYPE_EVENT_SCHEDULE):
        # Both blocks and events share the same Store; the same delete
        # API removes the whole entry bucket (blocks + events).
        try:
            await async_delete_storage_entry(hass, entry.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to clean up schedule storage for %s: %s",
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
