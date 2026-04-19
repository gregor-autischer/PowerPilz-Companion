# 🍄‍🟫 PowerPilz Companion

A Home Assistant custom integration that adds two **Smart helper** types, each designed to collapse a multi-entity + bridging-automation setup into a single entity that drives your devices autonomously:

- 🗓 **Smart Schedule** — a `select` entity with three override modes (Off / On / Auto) plus an accompanying `binary_sensor` that reflects whether the weekly plan is currently active. Weekly blocks are stored inside the integration itself; edit them directly in the PowerPilz Schedule Lovelace card (long-press).
- ⏱ **Smart Timer** — a `switch` entity that autonomously turns a target on at a configured start time and off at an end time (one-shot), with customizable icons, labels and direction.

Companion to the [PowerPilz](https://github.com/gregor-autischer/PowerPilz) Lovelace cards pack — use them together for a drastically simpler dashboard setup, or use the Companion standalone with any other cards.

## Why

Home Assistant's native building blocks (`schedule`, `input_datetime`, `input_boolean`) are great primitives but don't do anything by themselves — you always need bridging automations to turn devices on/off at the right time, plus manual-override glue to handle "I want it on right now" cases.

Each Companion helper bundles all of that logic into one entity:

| Traditional HA setup | Companion replacement |
| :-- | :-- |
| schedule helper + switch + input_select mode + 2 automations | **one** Smart Schedule entity (+ its binary_sensor) |
| switch + 2 input_datetimes + input_boolean + 2 automations | **one** Smart Timer entity |

## Features

### Smart Schedule

- Single `select.*` entity exposing three modes (renameable, with custom icons): Off / On / Auto
- Weekly schedule stored **inside the integration** (v0.4+) — no separate `schedule.*` helper needed
- Companion `binary_sensor.<name>_active` — use as a state trigger in automations, 1:1 replacement for `schedule.*` on/off triggers
- Rich attributes on the select + binary_sensor for templates: `schedule_active`, `next_event`, `next_start`, `next_end`, `current_window`, `today_blocks`, `week_blocks`
- In **Auto** mode: drives the target device on/off based on the weekly plan
- In **Off** / **On** mode: forces the target to the corresponding state
- Optional "resume Auto at next schedule boundary" — a manual override is automatically lifted at the next on/off transition (Nest-thermostat style)
- Edit the weekly plan directly in the PowerPilz Schedule card (long-press) — drag 15-minute blocks, click any block for minute-precise editing and per-block `data` payloads
- Automatic migration from v0.3 linked-schedule helpers: weekly blocks are imported on first startup and the orphan native schedule is deleted

### Smart Timer

- Single `switch.*` entity: `on` = timer active, `off` = inactive
- Autonomous driving: fires callbacks at the configured on/off datetimes, target is turned on/off without any user-written automation
- One-shot semantics: after the off boundary passes (or after the on boundary in on-only mode), the timer self-deactivates
- Resume on restart: if HA restarts mid-window, the timer re-registers its remaining callbacks and applies the current window state
- Direction choice: **both on and off**, **on only** (fire-and-forget turn-on), or **off only** (countdown-style turn-off)
- Customizable icon + label per state (inactive / active) — exposed as attributes so the PowerPilz Timer card can render them
- Works with `select`/`input_select` targets too (including another Smart Schedule!) — at each boundary the timer calls `select_option` with a configured option. Stable logical keys are used when the target exposes `mode_names`, so renaming modes doesn't break the binding.

### Both

- Multilingual (English + German)
- HACS-ready (Integration category)

## Installation

### HACS (Integration)

1. Open HACS → Integrations.
2. Menu → Custom repositories.
3. Paste `https://github.com/gregor-autischer/PowerPilz-Companion` and select category **Integration**.
4. Click **Add**, search for **PowerPilz Smart Helpers**, download.
5. Restart Home Assistant.

### Manual

Copy `custom_components/powerpilz_companion` into your Home Assistant config directory's `custom_components/` folder. Restart Home Assistant.

## Creating a helper

### Smart Schedule

1. **Settings → Devices & Services → Helpers → Create Helper → PowerPilz Smart Helpers**.
2. Pick **Smart Schedule** from the menu.
3. Fill in:
   - **Name** — e.g. `Living Room Heating`
   - **Device to control** — switch / light / input_boolean / fan / climate
   - **Mode names / icons** — customize how Off / On / Auto appear
   - **Resume Auto on next schedule boundary** — whether manual overrides auto-lift

On confirmation you get:

- `select.living_room_heating` — the Smart Schedule entity (three modes, rich schedule attributes)
- `binary_sensor.living_room_heating_active` — flips on/off with the weekly plan

Edit the weekly plan by **long-pressing the PowerPilz Schedule card** on your dashboard.

### Smart Timer

1. **Settings → Devices & Services → Helpers → Create Helper → PowerPilz Smart Helpers**.
2. Pick **Smart Timer** from the menu.
3. Fill in:
   - **Name** — e.g. `Dishwasher Timer`
   - **Device to control** — switch / light / input_boolean / fan / climate / select / input_select
   - **Timer kind** — both / on only / off only
   - **Inactive / Active state name + icon** — how the Lovelace card should label & iconize each state
4. If you picked a `select` target, a second step asks which option to set at start / end.

You get `switch.dishwasher_timer`. Set the on/off times via the [PowerPilz Timer card](https://github.com/gregor-autischer/PowerPilz#cards) or via the `powerpilz_companion.set_timer` service.

## Editing

- **Smart Schedule weekly plan** — long-press the PowerPilz Schedule card on your dashboard to open the inline weekly editor.
- **Smart Schedule / Timer settings** — click the gear icon on the helper under Settings → Devices & Services → PowerPilz Smart Helpers.
- **Smart Timer on/off times** — via the PowerPilz Timer card (recommended) or via the `powerpilz_companion.set_timer` service.

## Services

### `powerpilz_companion.set_timer`

Update the on and/or off datetime of a Smart Timer at runtime.

| Field | Type | Description |
| :-- | :-- | :-- |
| `entity_id` | entity | The Smart Timer switch entity |
| `on` | string (ISO 8601) | Turn-on datetime — e.g. `2026-04-19T18:30:00`. Omit to leave unchanged. |
| `off` | string (ISO 8601) | Turn-off datetime. Omit to leave unchanged. |
| `on_option` | string | (Select targets only) Option to apply at the on-boundary. Omit to leave unchanged. |
| `off_option` | string | (Select targets only) Option to apply at the off-boundary. Omit to leave unchanged. |

### `powerpilz_companion.set_schedule_blocks`

Replace the weekly blocks of a Smart Schedule helper. Used by the PowerPilz Schedule card, but also handy for automations (e.g. rotate between seasonal schedules).

| Field | Type | Description |
| :-- | :-- | :-- |
| `entity_id` | entity | The Smart Schedule select entity |
| `blocks` | dict | `{monday: [...], ..., sunday: [...]}`, each list holding `{from, to, data?}` entries. Times are `HH:MM:SS` (24:00:00 allowed as end-of-day). |

## Lovelace integration

The [PowerPilz](https://github.com/gregor-autischer/PowerPilz) dashboard card pack reads all relevant state from the Smart helper's attributes — one entity on the card, no bridging.

| Companion helper | Paired card | Attributes the card reads |
| :-- | :-- | :-- |
| Smart Schedule select | [Schedule card](https://github.com/gregor-autischer/PowerPilz/blob/main/docs/cards/schedule.md) | `week_blocks`, `target_entity`, `mode_names`, `mode_icons`, `schedule_active` |
| Smart Timer switch | [Timer card](https://github.com/gregor-autischer/PowerPilz/blob/main/docs/cards/timer.md) | `target_entity`, `on_datetime`, `off_datetime`, `direction`, `state_names`, `state_icons` |

## Exposed attributes

### Smart Schedule `select.*`

| Attribute | Description |
| :-- | :-- |
| `logical_mode` | `off` / `on` / `auto` (stable key, regardless of renamed display names) |
| `mode_names` | `{off, on, auto}` → configured display name |
| `mode_icons` | `{off, on, auto}` → configured MDI icon |
| `target_entity` / `target_state` | Controlled device + its current state |
| `schedule_active` | `True` while the current time falls inside an active block |
| `next_event` | ISO timestamp of the next on/off transition |
| `next_start` / `next_end` | ISO timestamps of the next block start / end separately |
| `current_window` | `{from, to, start, end, data?}` of the block currently active, or `null` |
| `today_blocks` | Raw list of today's blocks |
| `week_blocks` | Full `{monday: [...], ..., sunday: [...]}` |

### Smart Schedule `binary_sensor.*_active`

| Attribute | Description |
| :-- | :-- |
| State | `on` while a block is currently active, `off` otherwise |
| `schedule_active` | Mirror of the state for convenience |
| `current_window` / `next_event` / `next_start` / `next_end` / `today_blocks` / `week_blocks` | Mirrored from the parent select for single-entity templates |
| `companion_entity` | Back-pointer to the parent `select.*` |

### Smart Timer `switch.*`

| Attribute | Description |
| :-- | :-- |
| `target_entity` / `target_state` | Controlled device + current state |
| `on_datetime` / `off_datetime` | Scheduled on/off times (ISO 8601) |
| `next_event` | ISO timestamp of the next boundary that will fire |
| `direction` | `on_only` / `both` / `off_only` |
| `state_names` | `{inactive, active}` → display label |
| `state_icons` | `{inactive, active}` → MDI icon |
| `on_option` / `off_option` | (Select targets only) logical option applied at each boundary |
| `on_option_label` / `off_option_label` | (Select targets only) resolved display name for that option |

## Using a Smart Schedule in automations

The companion `binary_sensor.*_active` is a drop-in replacement for a native `schedule.*` helper as a state trigger:

```yaml
# Trigger when the schedule becomes active
trigger:
  platform: state
  entity_id: binary_sensor.living_room_heating_active
  to: "on"

# Or condition
condition:
  condition: state
  entity_id: binary_sensor.living_room_heating_active
  state: "on"
```

Rich attributes on both the select and binary_sensor give you more:

```yaml
# Template: when's the next schedule event?
template: >
  {{ state_attr('select.living_room_heating', 'next_event') }}

# Template: are we in a block with a specific data payload?
template: >
  {% set w = state_attr('select.living_room_heating', 'current_window') %}
  {{ w is not none and w.data is defined and w.data.mode == 'heat' }}
```

## How it works

### Smart Schedule

Weekly blocks live in `.storage/powerpilz_companion.schedules` — a dedicated Store, separate from `core.config_entries` so edits don't churn the entry registry. The select entity computes its `schedule_active` flag directly from the blocks with `async_track_point_in_time` callbacks scheduled at every upcoming boundary (no polling). In Auto mode it drives the target device on/off based on the computed flag; in Off / On mode it forces the target to that state. The accompanying `binary_sensor.*_active` mirrors the same flag for state-trigger automations.

### Smart Timer

Pure `async_track_point_in_time` callbacks at the on/off datetimes. No polling. State and datetimes persist across restarts via `RestoreEntity`, so no config-entry writes are needed for per-timer state — which avoids reload loops.

For select / input_select targets the timer calls `<domain>.select_option`. When the target exposes a `mode_names` attribute (as the Smart Schedule's select does) the timer stores the **logical key** (`off` / `on` / `auto`) rather than the display name, and resolves to the current display name via `mode_names` at fire time — renaming a mode in the Schedule helper therefore doesn't break the timer binding.

## Migration from v0.3.x

v0.4.0 drops the dependency on a linked native `schedule.*` helper. Existing Smart Schedule entries migrate automatically on first startup after the upgrade:

1. The weekly blocks are read from the linked `schedule.*` entity.
2. They are imported into the new internal Store.
3. The orphaned `schedule.*` entity is deleted from HA's storage.
4. The `linked_schedule` config option is removed from the entry.

After the migration your Smart Schedule select continues to work with the same `entity_id`; no dashboard or automation changes required beyond switching state-triggered automations from `schedule.*` to the new `binary_sensor.*_active`.

## Development & releases

- Contributions welcome — open an issue or PR on [GitHub](https://github.com/gregor-autischer/PowerPilz-Companion).
- See [RELEASING.md](RELEASING.md) for the full release procedure (version bump, `main → release` fast-forward merge, and troubleshooting).

## License

Apache-2.0 — see [LICENSE](LICENSE).
