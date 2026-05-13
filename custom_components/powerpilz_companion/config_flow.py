"""Config flow for PowerPilz Companion.

Two helper kinds are offered:

  - **Smart Schedule** — select entity with 3 modes + companion
    binary_sensor. Weekly blocks are edited in the PowerPilz Schedule
    Lovelace card (long-press on the card).
  - **Smart Timer** — autonomous switch entity driving a target device
    at configured on/off datetimes.

The top-level `async_step_user` shows a menu; each branch has its own
`async_show_form` step and creates an entry carrying a distinguishing
`entry_type` field in its options.
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
    CONF_ENTRY_TYPE,
    CONF_MODE_AUTO_ICON,
    CONF_MODE_AUTO_NAME,
    CONF_MODE_OFF_ICON,
    CONF_MODE_OFF_NAME,
    CONF_MODE_ON_ICON,
    CONF_MODE_ON_NAME,
    CONF_MODE_ON_VALUE,
    CONF_NAME,
    CONF_RESTORE_AUTO_ON_BOUNDARY,
    CONF_SAME_FOR_ALL_DAYS,
    CONF_STATE_ACTIVE_ICON,
    CONF_STATE_ACTIVE_NAME,
    CONF_STATE_INACTIVE_ICON,
    CONF_STATE_INACTIVE_NAME,
    CONF_TARGET_ENTITIES,
    CONF_TARGET_ENTITY,
    CONF_TIMER_DIRECTION,
    CONF_TIMER_OFF_OPTION,
    CONF_TIMER_ON_OPTION,
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
    DEFAULT_STATE_ACTIVE_ICON,
    DEFAULT_STATE_ACTIVE_NAME,
    DEFAULT_STATE_INACTIVE_ICON,
    DEFAULT_STATE_INACTIVE_NAME,
    DEFAULT_TIMER_DIRECTION,
    DEFAULT_UNIT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VALUE_MAX,
    DEFAULT_VALUE_MIN,
    DOMAIN,
    ENTRY_TYPE_CURVE,
    ENTRY_TYPE_SCHEDULE,
    ENTRY_TYPE_TIMER,
    TIMER_DIRECTION_BOTH,
    TIMER_DIRECTION_OFF_ONLY,
    TIMER_DIRECTION_ON_ONLY,
    TIMER_DIRECTIONS,
)


# ---------------------------------------------------------------------------
# Schedule schema + validation
# ---------------------------------------------------------------------------


def _schedule_schema(
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    defaults = defaults or {}

    target_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["switch", "light", "input_boolean", "fan", "climate"],
        )
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


def _schedule_validate(user_input: dict[str, Any]) -> None:
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


# ---------------------------------------------------------------------------
# Timer schema + validation
# ---------------------------------------------------------------------------


TIMER_TARGET_DOMAINS = [
    "switch",
    "light",
    "input_boolean",
    "fan",
    "climate",
    "select",
    "input_select",
]


def _is_select_domain(entity_id: str | None) -> bool:
    if not entity_id or "." not in entity_id:
        return False
    return entity_id.split(".", 1)[0] in ("select", "input_select")


def _timer_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    target_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(domain=TIMER_TARGET_DOMAINS)
    )
    text_selector = selector.TextSelector(selector.TextSelectorConfig())
    icon_selector = selector.IconSelector(selector.IconSelectorConfig())
    direction_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            mode=selector.SelectSelectorMode.DROPDOWN,
            options=[
                selector.SelectOptionDict(
                    value=TIMER_DIRECTION_BOTH,
                    label="Both on and off",
                ),
                selector.SelectOptionDict(
                    value=TIMER_DIRECTION_ON_ONLY,
                    label="On only",
                ),
                selector.SelectOptionDict(
                    value=TIMER_DIRECTION_OFF_ONLY,
                    label="Off only",
                ),
            ],
        )
    )

    def _marker(key: str, required: bool, fallback: Any = None) -> Any:
        current = defaults.get(key, fallback)
        mk = vol.Required if required else vol.Optional
        if current in (None, "", []):
            return mk(key)
        return mk(key, default=current)

    return vol.Schema(
        {
            _marker(CONF_NAME, True, ""): text_selector,
            _marker(CONF_TARGET_ENTITY, True): target_selector,
            vol.Optional(
                CONF_TIMER_DIRECTION,
                default=defaults.get(CONF_TIMER_DIRECTION, DEFAULT_TIMER_DIRECTION),
            ): direction_selector,
            _marker(
                CONF_STATE_INACTIVE_NAME, False, DEFAULT_STATE_INACTIVE_NAME
            ): text_selector,
            _marker(
                CONF_STATE_INACTIVE_ICON, False, DEFAULT_STATE_INACTIVE_ICON
            ): icon_selector,
            _marker(
                CONF_STATE_ACTIVE_NAME, False, DEFAULT_STATE_ACTIVE_NAME
            ): text_selector,
            _marker(
                CONF_STATE_ACTIVE_ICON, False, DEFAULT_STATE_ACTIVE_ICON
            ): icon_selector,
        }
    )


def _timer_options_schema(
    hass: Any,
    target_entity: str,
    direction: str,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[vol.Schema, list[str], dict[str, str | None]]:
    """Build the schema for the second timer step (select-target option pick).

    If the target is a PowerPilz Smart Schedule (detected via its
    `mode_names` attribute holding `{off, on, auto}` → display name), the
    dropdown entries are `{value: logical_key, label: current_display}`
    so the *stable logical key* is stored. At fire time `switch.py`
    resolves the logical key back to the current display name via
    `mode_names` — so renaming a mode in Smart Schedule doesn't break
    the timer binding.

    For all other select / input_select targets `value == label` (we
    store the display option as-is).
    """
    defaults = defaults or {}
    state = hass.states.get(target_entity) if hass else None
    options_attr = state.attributes.get("options") if state else None
    options_list: list[str] = (
        [str(v) for v in options_attr] if isinstance(options_attr, list) else []
    )

    # Smart Schedule detection via `mode_names` attribute.
    mode_names_attr = state.attributes.get("mode_names") if state else None
    mode_names: dict[str, str] = {}
    if isinstance(mode_names_attr, dict):
        for key, display in mode_names_attr.items():
            if isinstance(key, str) and isinstance(display, str):
                mode_names[key] = display

    is_smart_schedule = bool(mode_names)

    # Build selector options.
    if is_smart_schedule:
        options_for_selector = [
            selector.SelectOptionDict(value=logical, label=display)
            for logical, display in mode_names.items()
        ]
        # Defaults for Smart Schedule: on-event → logical "on", off-event
        # → logical "auto" (boost-until-resume pattern).
        smart_on: str | None = "on" if "on" in mode_names else None
        smart_off: str | None = "auto" if "auto" in mode_names else None
    else:
        options_for_selector = [
            selector.SelectOptionDict(value=opt, label=opt)
            for opt in options_list
        ]
        smart_on = None
        smart_off = None

    smart_defaults: dict[str, str | None] = {"on": smart_on, "off": smart_off}

    dropdown = selector.SelectSelector(
        selector.SelectSelectorConfig(
            mode=selector.SelectSelectorMode.DROPDOWN,
            options=options_for_selector,
        )
    )

    fields: dict[Any, Any] = {}

    if direction != "off_only":
        default_on = defaults.get(CONF_TIMER_ON_OPTION) or smart_defaults["on"]
        marker = (
            vol.Optional(CONF_TIMER_ON_OPTION, default=default_on)
            if default_on
            else vol.Optional(CONF_TIMER_ON_OPTION)
        )
        fields[marker] = dropdown

    if direction != "on_only":
        default_off = defaults.get(CONF_TIMER_OFF_OPTION) or smart_defaults["off"]
        marker = (
            vol.Optional(CONF_TIMER_OFF_OPTION, default=default_off)
            if default_off
            else vol.Optional(CONF_TIMER_OFF_OPTION)
        )
        fields[marker] = dropdown

    # The description still shows the raw display-name list for context.
    display_options = (
        list(mode_names.values()) if is_smart_schedule else options_list
    )
    return vol.Schema(fields), display_options, smart_defaults


# ---------------------------------------------------------------------------
# Curve schema + validation
# ---------------------------------------------------------------------------


def _curve_schema(
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    defaults = defaults or {}

    targets_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["climate", "number", "input_number"],
            multiple=True,
        )
    )
    text_selector = selector.TextSelector(selector.TextSelectorConfig())
    icon_selector = selector.IconSelector(selector.IconSelectorConfig())
    boolean_selector = selector.BooleanSelector()
    interval_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1, max=240, step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )
    value_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=-50, max=120, step=0.5,
            mode=selector.NumberSelectorMode.BOX,
        )
    )

    def _marker(key: str, required: bool, fallback: Any = None) -> Any:
        current = defaults.get(key, fallback)
        mk = vol.Required if required else vol.Optional
        if current in (None, "", []):
            return mk(key)
        return mk(key, default=current)

    fields: dict[Any, Any] = {}
    fields[_marker(CONF_NAME, True, "")] = text_selector
    fields[_marker(CONF_TARGET_ENTITIES, True, [])] = targets_selector
    fields[
        vol.Optional(
            CONF_UPDATE_INTERVAL,
            default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
    ] = interval_selector
    fields[
        vol.Optional(
            CONF_VALUE_MIN,
            default=defaults.get(CONF_VALUE_MIN, DEFAULT_VALUE_MIN),
        )
    ] = value_selector
    fields[
        vol.Optional(
            CONF_VALUE_MAX,
            default=defaults.get(CONF_VALUE_MAX, DEFAULT_VALUE_MAX),
        )
    ] = value_selector
    fields[_marker(CONF_UNIT, False, DEFAULT_UNIT)] = text_selector
    fields[
        vol.Optional(
            CONF_MODE_ON_VALUE,
            default=defaults.get(CONF_MODE_ON_VALUE, DEFAULT_MODE_ON_VALUE),
        )
    ] = value_selector
    fields[
        vol.Optional(
            CONF_SAME_FOR_ALL_DAYS,
            default=defaults.get(CONF_SAME_FOR_ALL_DAYS, False),
        )
    ] = boolean_selector
    fields[_marker(CONF_MODE_OFF_NAME, False, DEFAULT_MODE_OFF_NAME)] = text_selector
    fields[_marker(CONF_MODE_OFF_ICON, False, DEFAULT_MODE_OFF_ICON)] = icon_selector
    fields[_marker(CONF_MODE_ON_NAME, False, DEFAULT_MODE_ON_NAME)] = text_selector
    fields[_marker(CONF_MODE_ON_ICON, False, DEFAULT_MODE_ON_ICON)] = icon_selector
    fields[_marker(CONF_MODE_AUTO_NAME, False, DEFAULT_MODE_AUTO_NAME)] = text_selector
    fields[_marker(CONF_MODE_AUTO_ICON, False, DEFAULT_MODE_AUTO_ICON)] = icon_selector
    return vol.Schema(fields)


def _curve_validate(user_input: dict[str, Any]) -> None:
    targets = user_input.get(CONF_TARGET_ENTITIES)
    if isinstance(targets, str):
        user_input[CONF_TARGET_ENTITIES] = [targets]
    elif not isinstance(targets, list) or not targets:
        raise vol.Invalid("targets_required")

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

    try:
        v_min = float(user_input.get(CONF_VALUE_MIN, DEFAULT_VALUE_MIN))
        v_max = float(user_input.get(CONF_VALUE_MAX, DEFAULT_VALUE_MAX))
    except (TypeError, ValueError):
        raise vol.Invalid("invalid_range")
    if v_max <= v_min:
        raise vol.Invalid("invalid_range")
    user_input[CONF_VALUE_MIN] = v_min
    user_input[CONF_VALUE_MAX] = v_max

    try:
        interval = int(user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_UPDATE_INTERVAL
    user_input[CONF_UPDATE_INTERVAL] = max(1, interval)


def _timer_validate(user_input: dict[str, Any]) -> None:
    name = user_input.get(CONF_NAME)
    if isinstance(name, str):
        user_input[CONF_NAME] = name.strip()

    direction = user_input.get(CONF_TIMER_DIRECTION)
    if direction not in TIMER_DIRECTIONS:
        user_input[CONF_TIMER_DIRECTION] = DEFAULT_TIMER_DIRECTION

    for key, default in (
        (CONF_STATE_INACTIVE_NAME, DEFAULT_STATE_INACTIVE_NAME),
        (CONF_STATE_ACTIVE_NAME, DEFAULT_STATE_ACTIVE_NAME),
    ):
        value = user_input.get(key)
        if not isinstance(value, str) or not value.strip():
            user_input[key] = default
        else:
            user_input[key] = value.strip()


# ---------------------------------------------------------------------------
# Config flow (creation)
# ---------------------------------------------------------------------------


class PowerPilzCompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle creation of a PowerPilz helper (schedule or timer)."""

    VERSION = 1

    # Used to carry the collected timer fields across the two-step flow
    # when the target is a select entity.
    _timer_pending: dict[str, Any] | None = None

    async def async_step_user(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Top-level menu: pick the helper kind."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["schedule", "timer", "curve"],
        )

    # --- Schedule branch ---

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _schedule_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_SCHEDULE
                title = str(user_input.get(CONF_NAME) or "").strip() or "Smart Schedule"
                return self.async_create_entry(
                    title=title, data={}, options=user_input
                )

        return self.async_show_form(
            step_id="schedule",
            data_schema=_schedule_schema(user_input),
            errors=errors,
        )

    # --- Curve branch ---

    async def async_step_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _curve_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_CURVE
                title = str(user_input.get(CONF_NAME) or "").strip() or "Smart Curve"
                return self.async_create_entry(
                    title=title, data={}, options=user_input
                )

        return self.async_show_form(
            step_id="curve",
            data_schema=_curve_schema(user_input),
            errors=errors,
        )

    # --- Timer branch ---

    async def async_step_timer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _timer_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_TIMER
                target = user_input.get(CONF_TARGET_ENTITY)

                # For select targets, ask which option to apply at each
                # boundary. For switch/light/input_boolean/etc. fall back
                # to generic turn_on/turn_off and finalize immediately.
                if isinstance(target, str) and _is_select_domain(target):
                    self._timer_pending = dict(user_input)
                    return await self.async_step_timer_options()

                title = str(user_input.get(CONF_NAME) or "").strip() or "Smart Timer"
                return self.async_create_entry(
                    title=title, data={}, options=user_input
                )

        return self.async_show_form(
            step_id="timer",
            data_schema=_timer_schema(user_input),
            errors=errors,
        )

    async def async_step_timer_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step for select-target timers: which option to set at on/off."""
        pending = self._timer_pending or {}
        target = pending.get(CONF_TARGET_ENTITY, "")
        direction = pending.get(CONF_TIMER_DIRECTION, "both")
        schema, options_list, _defaults = _timer_options_schema(
            self.hass, target, direction, user_input or pending
        )

        if user_input is not None:
            merged = {**pending, **user_input}
            self._timer_pending = None
            title = str(merged.get(CONF_NAME) or "").strip() or "Smart Timer"
            return self.async_create_entry(title=title, data={}, options=merged)

        return self.async_show_form(
            step_id="timer_options",
            data_schema=schema,
            description_placeholders={
                "target": target,
                "options": ", ".join(options_list) if options_list else "—",
            },
        )

    # --- Options flow dispatch ---

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PowerPilzCompanionOptionsFlow(entry)


# ---------------------------------------------------------------------------
# Options flow (edit)
# ---------------------------------------------------------------------------


class PowerPilzCompanionOptionsFlow(OptionsFlow):
    """Edit an existing helper entry. Routes to the appropriate form
    based on the entry's `entry_type`."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._timer_pending: dict[str, Any] | None = None

    async def async_step_init(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry_type = self._entry.options.get(
            CONF_ENTRY_TYPE, ENTRY_TYPE_SCHEDULE
        )
        if entry_type == ENTRY_TYPE_TIMER:
            return await self.async_step_timer()
        if entry_type == ENTRY_TYPE_CURVE:
            return await self.async_step_curve()
        return await self.async_step_schedule()

    async def async_step_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _curve_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_CURVE
                return self.async_create_entry(title="", data=user_input)

        defaults = {**self._entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="curve",
            data_schema=_curve_schema(defaults),
            errors=errors,
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _schedule_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_SCHEDULE
                return self.async_create_entry(title="", data=user_input)

        defaults = {**self._entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="schedule",
            data_schema=_schedule_schema(defaults),
            errors=errors,
        )

    async def async_step_timer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _timer_validate(user_input)
            except vol.Invalid as err:
                errors["base"] = str(err)
            else:
                merged = {**self._entry.options, **user_input}
                merged[CONF_ENTRY_TYPE] = ENTRY_TYPE_TIMER
                target = merged.get(CONF_TARGET_ENTITY)
                if isinstance(target, str) and _is_select_domain(target):
                    self._timer_pending = merged
                    return await self.async_step_timer_options()
                # Not a select → clear any stale option settings.
                merged.pop(CONF_TIMER_ON_OPTION, None)
                merged.pop(CONF_TIMER_OFF_OPTION, None)
                return self.async_create_entry(title="", data=merged)

        defaults = {**self._entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="timer",
            data_schema=_timer_schema(defaults),
            errors=errors,
        )

    async def async_step_timer_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        pending = self._timer_pending or {}
        target = pending.get(CONF_TARGET_ENTITY, "")
        direction = pending.get(CONF_TIMER_DIRECTION, "both")
        schema, options_list, _smart = _timer_options_schema(
            self.hass, target, direction, user_input or pending
        )

        if user_input is not None:
            merged = {**pending, **user_input}
            self._timer_pending = None
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="timer_options",
            data_schema=schema,
            description_placeholders={
                "target": target,
                "options": ", ".join(options_list) if options_list else "—",
            },
        )
