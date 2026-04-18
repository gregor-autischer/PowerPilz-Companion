# 🍄‍🟫 PowerPilz Companion

A Home Assistant custom integration that adds a **Smart Schedule** helper — a single entity that wraps a target device, three override modes (Off / On / Auto) and a linked weekly schedule.

The weekly plan is drawn with **Home Assistant's native drag-and-drop Schedule UI** — the Companion auto-creates and links the schedule helper for you during setup, so you get a first-class native editing experience with zero extra configuration.

## Why

HA's built-in `schedule` helper is great for defining weekly on/off blocks, but **it doesn't control anything by itself** — you'd normally need to write two bridging automations per schedule, handle manual overrides yourself, and juggle multiple entities on every dashboard.

The PowerPilz **Smart Schedule** helper bundles all of that behind one entity:

- Create one helper → a linked `schedule.*` helper is auto-created in the same step.
- The integration drives the target device according to that schedule.
- Exposes a `select` entity with three customizable modes (Off / On / Auto) for manual overrides from dashboards, voice assistants, or automations.
- Optional auto-restore of Auto mode at the next schedule boundary (Nest-thermostat style).

## Features

- **Zero bridging automations** — no YAML, no scripts, no glue code.
- **Three override modes** with per-mode name + icon:
  - **Off** — device forced off, schedule ignored.
  - **On** — device forced on, schedule ignored.
  - **Auto** — device mirrors the linked schedule's on/off state.
- **Auto-link to schedule helper** — when creating the helper, leave the schedule field empty and one is created automatically with the same name. Or pick an existing `schedule.*` entity to link an already-existing schedule.
- **Native drag-and-drop editing** — weekly blocks are edited in Home Assistant's own schedule helper UI (Settings → Helpers → click the linked schedule).
- **Auto-restore at boundary** (optional) — a manual On/Off override is lifted automatically at the next on/off transition of the linked schedule.
- **Automatic cleanup** — deleting a Smart Schedule also removes its auto-created linked schedule helper.
- **Multilingual** — English + German bundled.

## Installation

### HACS (Integration)

1. Open HACS → Integrations.
2. Menu → Custom repositories.
3. Paste `https://github.com/gregor-autischer/PowerPilz-Companion` and select category **Integration**.
4. Click **Add**, search for **PowerPilz Companion**, download.
5. Restart Home Assistant.

### Manual

Copy `custom_components/powerpilz_companion` into your Home Assistant config directory's `custom_components/` folder. Restart Home Assistant.

## Setup

1. **Settings → Devices & Services → Helpers → Create Helper → PowerPilz Smart Schedule**.
2. Fill in:
   - **Name** — e.g. `Living Room Heating`.
   - **Device to control** — the switch, light, input_boolean, fan or climate entity.
   - **Linked schedule helper** — leave empty to auto-create a new `schedule.living_room_heating` with an empty weekly plan, or pick an existing schedule to link to that one instead.
   - **Mode names & icons** — customize how Off / On / Auto appear.
   - **Resume Auto on next schedule boundary** — whether a manual override is auto-lifted at the next on/off transition.
3. Confirm. You get:
   - `select.living_room_heating` — the Smart Schedule entity (three modes).
   - `schedule.living_room_heating` — the linked weekly schedule (empty; ready to be filled in).

## Editing the weekly schedule

Open **Settings → Devices & Services → Helpers**, click the linked schedule entry, and use Home Assistant's native drag-and-drop UI to draw the weekly blocks. The Smart Schedule entity picks up changes in real time.

To relink to a different schedule (or change name/device/modes), click the gear icon on the Smart Schedule entry.

## Lovelace integration

Reference only the Smart Schedule entity (`select.<name>`) in your cards — the linked schedule's state is exposed through its attributes:

| Attribute | Description |
|-----------|-------------|
| `logical_mode` | Current mode: `off` / `on` / `auto` |
| `mode_names` | `{off: "...", on: "...", auto: "..."}` display names |
| `mode_icons` | `{off: "mdi:...", on: "mdi:...", auto: "mdi:..."}` icons |
| `target_entity` / `target_state` | Controlled device and its live state |
| `linked_schedule` | Entity ID of the linked `schedule.*` helper |
| `schedule_state` | `on` / `off` — live state of the linked schedule |
| `next_event` | ISO timestamp of the next scheduled on/off transition |

## How it works

- On creation, the Companion either uses a schedule entity you picked, or writes a new entry into HA's schedule storage collection and asks HA to register it at runtime — no restart needed.
- The Smart Schedule `select` entity subscribes to the linked schedule's state changes and drives the target device accordingly when in Auto mode.
- In Off / On mode, the target is forced to that state. When `Resume Auto on next schedule boundary` is enabled, the next on↔off transition of the linked schedule automatically switches the mode back to Auto.
- On removal of the Smart Schedule helper, the auto-created linked schedule is cleaned up.

## Relation to PowerPilz Cards

[PowerPilz Cards](https://github.com/gregor-autischer/PowerPilz) is a separate Lovelace plugin for energy/wallbox/graph/switch/timer/schedule cards. It's **not required** for this integration. If both are installed they complement each other: the Companion provides the stateful schedule helper, the Cards plugin provides visualization.

## License

Apache-2.0
