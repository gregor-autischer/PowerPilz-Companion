"""Config flow for PowerPilz Companion Smart Schedule helper.

- **Creation** (`async_step_user`): Name, target, optional linked schedule,
  modes. If linked_schedule is left empty, a native `schedule.*` helper is
  auto-created and linked; otherwise the supplied entity is used.
- **Options** (`async_step_init`): edit all of the above — linked_schedule
  is pre-filled with the currently linked schedule so the user can see
  and re-link if desired.

Both flows share a common schema builder and validator.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_LINKED_SCHEDULE,
    CONF_MODE_AUTO_ICON,
    CONF_MODE_AUTO_NAME,
    CONF_MODE_OFF_ICON,
    CONF_MODE_OFF_NAME,
    CONF_MODE_ON_ICON,
    CONF_MODE_ON_NAME,
    CONF_NAME,
    CONF_RESTORE_AUTO_ON_BOUNDARY,
    CONF_TARGET_ENTITY,
    DEFAULT_MODE_AUTO_ICON,
    DEFAULT_MODE_AUTO_NAME,
    DEFAULT_MODE_OFF_ICON,
    DEFAULT_MODE_OFF_NAME,
    DEFAULT_MODE_ON_ICON,
    DEFAULT_MODE_ON_NAME,
    DOMAIN,
)
from .schedule_linker import async_create_linked_schedule


def _build_schema(
    defaults: Mapping[str, Any] | None = None,
    *,
    linked_schedule_required: bool = False,
) -> vol.Schema:
    """Build the form schema.

    In **creation** the linked_schedule is OPTIONAL — leaving it empty
    triggers auto-creation of a new native Schedule helper with the same
    name; picking an existing `schedule.*` links to it instead.

    In **options** it's REQUIRED and pre-filled with the currently linked
    schedule so it can't be accidentally unlinked.
    """
    defaults = defaults or {}

    target_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["switch", "light", "input_boolean", "fan", "climate"],
        )
    )
    schedule_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="schedule")
    )
    icon_selector = selector.IconSelector(selector.IconSelectorConfig())
    text_selector = selector.TextSelector(selector.TextSelectorConfig())
    boolean_selector = selector.BooleanSelector()

    def _marker(key: str, required: bool, fallback: Any = None) -> Any:
        current = defaults.get(key, fallback)
        mk = vol.Required if required else vol.Optional
        if current in (None, "", []):
            return mk(key)
        return mk(key, default=current)

    fields: dict[Any, Any] = {}
    fields[_marker(CONF_NAME, True, "")] = text_selector
    fields[_marker(CONF_TARGET_ENTITY, True)] = target_selector
    fields[_marker(CONF_LINKED_SCHEDULE, linked_schedule_required)] = schedule_selector
    fields[_marker(CONF_MODE_OFF_NAME, False, DEFAULT_MODE_OFF_NAME)] = text_selector
    fields[_marker(CONF_MODE_OFF_ICON, False, DEFAULT_MODE_OFF_ICON)] = icon_selector
    fields[_marker(CONF_MODE_ON_NAME, False, DEFAULT_MODE_ON_NAME)] = text_selector
    fields[_marker(CONF_MODE_ON_ICON, False, DEFAULT_MODE_ON_ICON)] = icon_selector
    fields[_marker(CONF_MODE_AUTO_NAME, False, DEFAULT_MODE_AUTO_NAME)] = text_selector
    fields[_marker(CONF_MODE_AUTO_ICON, False, DEFAULT_MODE_AUTO_ICON)] = icon_selector
    fields[
        vol.Optional(
            CONF_RESTORE_AUTO_ON_BOUNDARY,
            default=defaults.get(CONF_RESTORE_AUTO_ON_BOUNDARY, True),
        )
    ] = boolean_selector
    return vol.Schema(fields)


def _normalize_and_validate(user_input: dict[str, Any]) -> None:
    """Normalize user_input in place; raise vol.Invalid on errors."""
    for key, default in (
        (CONF_MODE_OFF_NAME, DEFAULT_MODE_OFF_NAME),
        (CONF_MODE_ON_NAME, DEFAULT_MODE_ON_NAME),
        (CONF_MODE_AUTO_NAME, DEFAULT_MODE_AUTO_NAME),
    ):
        value = user_input.get(key)
        if not isinstance(value, str) or not value.strip():
            user_input[key] = default
        else:
            user_input[key] = value.strip()

    names = {
        user_input[CONF_MODE_OFF_NAME],
        user_input[CONF_MODE_ON_NAME],
        user_input[CONF_MODE_AUTO_NAME],
    }
    if len(names) != 3:
        raise vol.Invalid("duplicate_mode_names")


class PowerPilzCompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle creation of a PowerPilz Smart Schedule helper."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _normalize_and_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                # Resolve linked_schedule: supplied or auto-create.
                supplied = user_input.get(CONF_LINKED_SCHEDULE)
                if isinstance(supplied, str) and supplied.strip():
                    user_input[CONF_LINKED_SCHEDULE] = supplied.strip()
                else:
                    entity_id = await async_create_linked_schedule(
                        self.hass, preferred_name=user_input[CONF_NAME]
                    )
                    user_input[CONF_LINKED_SCHEDULE] = entity_id

                title = str(user_input.get(CONF_NAME) or "").strip() or "Smart Schedule"
                return self.async_create_entry(
                    title=title, data={}, options=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(user_input, linked_schedule_required=False),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PowerPilzCompanionOptionsFlow(entry)


class PowerPilzCompanionOptionsFlow(OptionsFlow):
    """Edit an existing PowerPilz Smart Schedule helper."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _normalize_and_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(title="", data=user_input)

        defaults = {**self._entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults, linked_schedule_required=True),
            errors=errors,
        )
