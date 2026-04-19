"""Persistent store for Smart Schedule weekly blocks.

Blocks live in `.storage/powerpilz_companion.schedules` — a dedicated
JSON file keyed by config entry id. This is separate from
`core.config_entries` so the common case (user edits a block in the
card) doesn't churn the config-entry registry and trigger entry
reloads.

Schema:
    {
        "entries": {
            "<config_entry_id>": {
                "blocks": {
                    "monday":    [{"from": "HH:MM:SS", "to": "HH:MM:SS", "data": {...}?}, ...],
                    ...
                    "sunday":    [...],
                },
            },
            ...
        }
    }

Block shape exactly mirrors what HA's native `schedule.*` helper
used to return via `schedule/list`, so old card code keeps working
with minimal changes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION, WEEKDAY_KEYS

_LOGGER = logging.getLogger(__name__)

_STORE_CACHE_KEY = "_schedule_store"


def _empty_week() -> dict[str, list[dict[str, Any]]]:
    return {day: [] for day in WEEKDAY_KEYS}


def _normalize_blocks(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Coerce arbitrary input into the canonical week dict shape."""
    out = _empty_week()
    if not isinstance(raw, dict):
        return out
    for day in WEEKDAY_KEYS:
        day_blocks = raw.get(day)
        if not isinstance(day_blocks, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for block in day_blocks:
            if not isinstance(block, dict):
                continue
            frm = block.get("from")
            to = block.get("to")
            if not isinstance(frm, str) or not isinstance(to, str):
                continue
            entry: dict[str, Any] = {"from": frm, "to": to}
            data = block.get("data")
            if isinstance(data, dict) and data:
                entry["data"] = data
            cleaned.append(entry)
        out[day] = cleaned
    return out


def _get_store(hass: HomeAssistant) -> Store:
    """Return (and cache) the shared Store instance."""
    bucket = hass.data.setdefault(DOMAIN, {})
    store = bucket.get(_STORE_CACHE_KEY)
    if store is None:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        bucket[_STORE_CACHE_KEY] = store
        # Serialize concurrent writes behind a single lock so two
        # near-simultaneous edits don't race.
        bucket.setdefault("_schedule_store_lock", asyncio.Lock())
    return store


async def _load_raw(hass: HomeAssistant) -> dict[str, Any]:
    store = _get_store(hass)
    data = await store.async_load()
    if not isinstance(data, dict):
        return {"entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        data["entries"] = {}
    return data


async def async_load_blocks(
    hass: HomeAssistant, entry_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Return the weekly blocks for a Smart Schedule entry."""
    data = await _load_raw(hass)
    entry_blob = data.get("entries", {}).get(entry_id) or {}
    return _normalize_blocks(entry_blob.get("blocks"))


async def async_save_blocks(
    hass: HomeAssistant,
    entry_id: str,
    blocks: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Persist a new set of weekly blocks for an entry.

    Returns the normalized payload that was actually saved so callers
    can hand it straight back to their entity without re-reading.
    """
    normalized = _normalize_blocks(blocks)
    bucket = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = bucket.setdefault(
        "_schedule_store_lock", asyncio.Lock()
    )
    async with lock:
        data = await _load_raw(hass)
        entries = data.setdefault("entries", {})
        entries[entry_id] = {"blocks": normalized}
        store = _get_store(hass)
        await store.async_save(data)
    return normalized


async def async_delete_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the blocks bucket for an entry (called from async_remove_entry)."""
    bucket = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = bucket.setdefault(
        "_schedule_store_lock", asyncio.Lock()
    )
    async with lock:
        data = await _load_raw(hass)
        entries = data.get("entries")
        if isinstance(entries, dict) and entry_id in entries:
            entries.pop(entry_id, None)
            store = _get_store(hass)
            await store.async_save(data)


# --- Migration from legacy linked `schedule.*` helpers --------------


async def async_migrate_from_schedule_entity(
    hass: HomeAssistant, entry_id: str, schedule_entity_id: str
) -> bool:
    """One-shot import of blocks from a legacy native schedule helper.

    Reads the legacy schedule entry, imports its weekly plan into our
    store, and removes the orphaned schedule from HA's native storage
    so it doesn't linger in Settings → Helpers. Safe to call multiple
    times — a second invocation is a no-op.
    """
    from homeassistant.helpers.storage import Store as _Store
    from homeassistant.util import slugify

    # Skip if we already have blocks for this entry.
    existing = await async_load_blocks(hass, entry_id)
    if any(existing[day] for day in WEEKDAY_KEYS):
        return False

    # Native `schedule` uses storage key "schedule" with item entries.
    native_store = _Store(hass, 1, "schedule")
    raw = await native_store.async_load()
    if not isinstance(raw, dict):
        return False
    items = raw.get("items")
    if not isinstance(items, list):
        return False

    slug = schedule_entity_id.split(".", 1)[1] if "." in schedule_entity_id else ""

    match: dict[str, Any] | None = None
    match_idx: int | None = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        if slugify(name) == slug:
            match = item
            match_idx = idx
            break

    if match is None:
        return False

    week_payload: dict[str, list[dict[str, Any]]] = {}
    for day in WEEKDAY_KEYS:
        raw_blocks = match.get(day)
        if isinstance(raw_blocks, list):
            week_payload[day] = raw_blocks

    await async_save_blocks(hass, entry_id, week_payload)

    # Remove the orphaned native schedule from HA's storage so users
    # don't see it in Settings → Helpers anymore. We delete from the
    # live storage collection if reachable (runtime removal, no
    # restart needed); otherwise fall back to rewriting the store
    # file directly (takes effect after restart).
    removed_live = await _remove_native_schedule_live(hass, match)
    if not removed_live and match_idx is not None:
        try:
            new_items = items[:match_idx] + items[match_idx + 1 :]
            raw["items"] = new_items
            await native_store.async_save(raw)
            _LOGGER.info(
                "Removed orphaned native schedule %s from storage (takes "
                "effect after next HA restart)",
                schedule_entity_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Migration of %s succeeded but couldn't clean up the "
                "orphaned native schedule helper: %s",
                schedule_entity_id,
                err,
            )

    _LOGGER.info(
        "Imported weekly blocks from legacy schedule.%s into entry %s",
        slug,
        entry_id,
    )
    return True


async def _remove_native_schedule_live(
    hass: HomeAssistant, item: dict[str, Any]
) -> bool:
    """Try to remove the schedule item via HA's live storage collection.

    Returns True if removed at runtime; False if the collection wasn't
    reachable (the caller then falls back to a direct file rewrite).
    """
    ws_handlers = hass.data.get("websocket_api")
    if not isinstance(ws_handlers, dict):
        return False
    entry = ws_handlers.get("schedule/create")
    if not entry:
        return False
    handler = entry[0] if isinstance(entry, tuple) else entry
    # Unwrap `functools.wraps` layers (require_admin, async_response)
    # to reach the bound DictStorageCollectionWebsocket method, whose
    # `__self__` carries the live `storage_collection`.
    seen: set[int] = set()
    while callable(handler) and hasattr(handler, "__wrapped__"):
        if id(handler) in seen:
            break
        seen.add(id(handler))
        handler = handler.__wrapped__
    ws_instance = getattr(handler, "__self__", None)
    collection = getattr(ws_instance, "storage_collection", None)
    if collection is None:
        return False
    target_id = None
    for item_id, candidate in collection.data.items():
        if candidate is item or (
            isinstance(candidate, dict)
            and candidate.get("id") == item.get("id")
        ):
            target_id = item_id
            break
    if target_id is None:
        return False
    try:
        await collection.async_delete_item(target_id)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Live-delete of schedule item failed: %s", err)
        return False
