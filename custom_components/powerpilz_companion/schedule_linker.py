"""Programmatic CRUD of native HA Schedule helpers.

HA's `schedule` component does not expose its internal storage collection
via `hass.data`. To interact with it from another integration we have to
reach into HA's websocket command registry: when the schedule component
sets up, it registers `DictStorageCollectionWebsocket` handlers whose
bound `ws_create_item` / `ws_delete_item` methods close over the
`DictStorageCollection` instance. By unwrapping `functools.wraps` layers
(`require_admin`, `async_response`) we get back to the bound method whose
`__self__` gives us the websocket handler instance — and from there the
live storage collection.

Calling `async_create_item(...)` on that collection:
- validates via Voluptuous schema
- persists to `.storage/schedule`
- fires `CHANGE_ADDED` which HA's `sync_entity_lifecycle` listens to
- → a new `schedule.*` entity is registered at runtime, no restart needed

Falls back to direct Store write + service reload if the handler isn't
reachable (e.g. HA internals changed).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

SCHEDULE_DOMAIN = "schedule"
SCHEDULE_STORAGE_VERSION = 1
SCHEDULE_STORAGE_KEY = "schedule"
WEBSOCKET_API_DOMAIN = "websocket_api"

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _empty_days() -> dict[str, list]:
    return {day: [] for day in _WEEKDAYS}


def _unique_name(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    i = 2
    while True:
        candidate = f"{base} {i}"
        if candidate not in existing:
            return candidate
        i += 1


# --- Collection access via websocket command registry ---


def _unwrap(fn: Any) -> Any:
    """Strip `functools.wraps` decorators to reach the innermost function."""
    seen = set()
    while callable(fn) and hasattr(fn, "__wrapped__"):
        if id(fn) in seen:
            break
        seen.add(id(fn))
        fn = fn.__wrapped__
    return fn


def _get_schedule_storage_collection(hass: HomeAssistant) -> Any | None:
    """Walk HA's websocket-command registry to find the schedule storage
    collection. Returns None if the internal API has changed."""
    ws_handlers = hass.data.get(WEBSOCKET_API_DOMAIN)
    if not isinstance(ws_handlers, dict):
        return None
    entry = ws_handlers.get(f"{SCHEDULE_DOMAIN}/create")
    if not entry:
        return None
    handler = entry[0] if isinstance(entry, tuple) else entry
    inner = _unwrap(handler)
    # `ws_create_item` is a bound method on DictStorageCollectionWebsocket.
    ws_instance = getattr(inner, "__self__", None)
    if ws_instance is None:
        return None
    return getattr(ws_instance, "storage_collection", None)


# --- Direct Store fallback ---


async def _fallback_store_write(
    hass: HomeAssistant, new_item: dict[str, Any]
) -> None:
    """Write a schedule item directly to `.storage/schedule` and trigger
    a reload. The reload only touches YAML-defined schedules, so the new
    entity will only appear after the next full HA restart."""
    store = Store(hass, SCHEDULE_STORAGE_VERSION, SCHEDULE_STORAGE_KEY)
    data = await store.async_load() or {"items": []}
    if not isinstance(data, dict):
        data = {"items": []}
    items = data.setdefault("items", [])
    if not isinstance(items, list):
        items = []
        data["items"] = items
    items.append(new_item)
    await store.async_save(data)
    _LOGGER.warning(
        "Wrote schedule item via fallback Store (entity will only appear "
        "after the next HA restart)."
    )


async def _fallback_store_delete(
    hass: HomeAssistant, target_slug: str
) -> bool:
    """Delete a schedule item directly from `.storage/schedule`."""
    store = Store(hass, SCHEDULE_STORAGE_VERSION, SCHEDULE_STORAGE_KEY)
    data = await store.async_load() or {"items": []}
    if not isinstance(data, dict):
        return False
    items = data.get("items", [])
    if not isinstance(items, list):
        return False
    new_items: list = []
    removed = False
    for item in items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and slugify(item["name"]) == target_slug
        ):
            removed = True
            continue
        new_items.append(item)
    if not removed:
        return False
    data["items"] = new_items
    await store.async_save(data)
    return True


# --- Public API ---


async def async_create_linked_schedule(
    hass: HomeAssistant,
    preferred_name: str,
    icon: str = "mdi:calendar-clock",
) -> str:
    """Create an empty native schedule helper and return its entity_id.

    Uses HA's live DictStorageCollection when reachable so the new
    `schedule.*` entity appears at runtime. Falls back to a direct file
    write if HA internals have changed.
    """
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})

    collection = _get_schedule_storage_collection(hass)

    # Figure out the final (collision-free) name using the collection's
    # current state if available, otherwise read the Store directly.
    existing_names: set[str] = set()
    if collection is not None:
        # DictStorageCollection.data is a dict {id: item}
        for item in collection.data.values():
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                existing_names.add(item["name"])
    else:
        store = Store(hass, SCHEDULE_STORAGE_VERSION, SCHEDULE_STORAGE_KEY)
        data = await store.async_load() or {}
        for item in data.get("items", []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                existing_names.add(item["name"])

    base_name = (preferred_name or "").strip() or "Smart Schedule Plan"
    final_name = _unique_name(base_name, existing_names)

    payload: dict[str, Any] = {
        "name": final_name,
        "icon": icon,
        **_empty_days(),
    }

    if collection is not None:
        try:
            item = await collection.async_create_item(payload)
            # The collection assigns an "id" key. Entity_id is derived from
            # the slugified name — even when name collisions bump it.
            item_id = item.get("id") if isinstance(item, dict) else None
            _LOGGER.info(
                "Created linked schedule via storage collection: name=%s id=%s",
                final_name,
                item_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Storage collection create failed (%s); falling back to "
                "direct Store write.",
                err,
            )
            await _fallback_store_write(
                hass, {"id": uuid.uuid4().hex, **payload}
            )
    else:
        _LOGGER.warning(
            "Could not reach schedule storage collection; falling back to "
            "direct Store write. The new schedule entity will appear only "
            "after the next HA restart."
        )
        await _fallback_store_write(hass, {"id": uuid.uuid4().hex, **payload})

    entity_id = f"{SCHEDULE_DOMAIN}.{slugify(final_name)}"
    _LOGGER.info("Auto-linked schedule: %s", entity_id)
    return entity_id


async def async_remove_linked_schedule(
    hass: HomeAssistant, entity_id: str
) -> bool:
    """Remove a previously-created native schedule helper by entity_id."""
    if not entity_id or not entity_id.startswith(f"{SCHEDULE_DOMAIN}."):
        return False

    target_slug = entity_id.split(".", 1)[1]

    collection = _get_schedule_storage_collection(hass)

    if collection is not None:
        # Find the item whose slugified name matches.
        target_id: str | None = None
        for item_id, item in collection.data.items():
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and slugify(item["name"]) == target_slug
            ):
                target_id = item_id
                break
        if target_id is None:
            return False
        try:
            await collection.async_delete_item(target_id)
            _LOGGER.info(
                "Removed linked schedule via storage collection: %s (id=%s)",
                entity_id,
                target_id,
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Storage collection delete failed (%s); falling back to "
                "direct Store write.",
                err,
            )

    return await _fallback_store_delete(hass, target_slug)
