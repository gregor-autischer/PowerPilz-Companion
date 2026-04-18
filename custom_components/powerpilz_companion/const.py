"""Constants for PowerPilz Companion."""
from __future__ import annotations

DOMAIN = "powerpilz_companion"

# --- Config keys ---

CONF_NAME = "name"
CONF_TARGET_ENTITY = "target_entity"

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
