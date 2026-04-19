"""Constants for PowerPilz Companion."""
from __future__ import annotations

DOMAIN = "powerpilz_companion"

# --- Entry-type discriminator ---
# The integration hosts two helper kinds sharing the same domain:
#   - ENTRY_TYPE_SCHEDULE: a `select` entity with 3 modes, linked to a
#     native HA schedule helper
#   - ENTRY_TYPE_TIMER: a `switch` entity with attached on/off datetimes
#     that drives the target device autonomously

CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_SCHEDULE = "schedule"
ENTRY_TYPE_TIMER = "timer"

# --- Config keys ---

CONF_NAME = "name"
CONF_TARGET_ENTITY = "target_entity"

# Smart Timer state — persisted in config entry options between restarts.
# Values are ISO-8601 datetime strings (e.g. "2026-04-19T18:30:00").
CONF_TIMER_ON = "timer_on"
CONF_TIMER_OFF = "timer_off"
CONF_TIMER_ACTIVE = "timer_active"

# Timer direction: which kind of one-shot timer is this?
#   on_only   → fires only at on-time (turns target on, then deactivates)
#   both      → turns target on at on-time, off at off-time (default)
#   off_only  → fires only at off-time (turns target off, then deactivates)
# For targets of domain `select` / `input_select` (including our own
# Smart Schedule), the user picks which option to apply at each boundary
# instead of relying on the generic turn_on/turn_off. Ignored for other
# target domains.
CONF_TIMER_ON_OPTION = "on_option"
CONF_TIMER_OFF_OPTION = "off_option"

CONF_TIMER_DIRECTION = "timer_direction"
TIMER_DIRECTION_ON_ONLY = "on_only"
TIMER_DIRECTION_BOTH = "both"
TIMER_DIRECTION_OFF_ONLY = "off_only"
TIMER_DIRECTIONS = (
    TIMER_DIRECTION_ON_ONLY,
    TIMER_DIRECTION_BOTH,
    TIMER_DIRECTION_OFF_ONLY,
)
DEFAULT_TIMER_DIRECTION = TIMER_DIRECTION_BOTH

# Per-state display name + icon (shown by the Lovelace card).
CONF_STATE_INACTIVE_NAME = "state_inactive_name"
CONF_STATE_INACTIVE_ICON = "state_inactive_icon"
CONF_STATE_ACTIVE_NAME = "state_active_name"
CONF_STATE_ACTIVE_ICON = "state_active_icon"

DEFAULT_STATE_INACTIVE_NAME = "Ready"
DEFAULT_STATE_INACTIVE_ICON = "mdi:timer-outline"
DEFAULT_STATE_ACTIVE_NAME = "Running"
DEFAULT_STATE_ACTIVE_ICON = "mdi:timer-play-outline"

# Entity_id of the native HA Schedule helper that this Smart Schedule tracks.
# Editing of the actual weekly schedule happens in that schedule helper's
# native drag-and-drop UI — we only observe its on/off state.
CONF_LINKED_SCHEDULE = "linked_schedule"

CONF_MODE_OFF_NAME = "mode_off_name"
CONF_MODE_OFF_ICON = "mode_off_icon"

CONF_MODE_ON_NAME = "mode_on_name"
CONF_MODE_ON_ICON = "mode_on_icon"

CONF_MODE_AUTO_NAME = "mode_auto_name"
CONF_MODE_AUTO_ICON = "mode_auto_icon"

CONF_RESTORE_AUTO_ON_BOUNDARY = "restore_auto_on_boundary"

# --- Defaults ---

DEFAULT_MODE_OFF_NAME = "Off"
DEFAULT_MODE_OFF_ICON = "mdi:power-off"

DEFAULT_MODE_ON_NAME = "On"
DEFAULT_MODE_ON_ICON = "mdi:power"

DEFAULT_MODE_AUTO_NAME = "Auto"
DEFAULT_MODE_AUTO_ICON = "mdi:clock-outline"

# --- Logical modes (stable internal identifiers) ---

MODE_OFF = "off"
MODE_ON = "on"
MODE_AUTO = "auto"

LOGICAL_MODES = (MODE_OFF, MODE_ON, MODE_AUTO)

# --- Entity attributes ---

ATTR_LOGICAL_MODE = "logical_mode"
ATTR_TARGET_ENTITY = "target_entity"
ATTR_TARGET_STATE = "target_state"
ATTR_LINKED_SCHEDULE = "linked_schedule"
ATTR_SCHEDULE_STATE = "schedule_state"
ATTR_MODE_ICONS = "mode_icons"
ATTR_MODE_NAMES = "mode_names"
ATTR_NEXT_EVENT = "next_event"
ATTR_ON_DATETIME = "on_datetime"
ATTR_OFF_DATETIME = "off_datetime"
ATTR_DIRECTION = "direction"
ATTR_STATE_NAMES = "state_names"
ATTR_STATE_ICONS = "state_icons"
# For select/input_select targets the timer stores which option to set at
# each boundary. The *_LABEL variants carry the resolved display name so
# the Lovelace card can render "Set to 'On' at:" style labels without
# having to duplicate the Smart-Schedule mode_names lookup.
ATTR_ON_OPTION = "on_option"
ATTR_OFF_OPTION = "off_option"
ATTR_ON_OPTION_LABEL = "on_option_label"
ATTR_OFF_OPTION_LABEL = "off_option_label"

# --- Services ---

SERVICE_SET_TIMER = "set_timer"
