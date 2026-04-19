# 🍄‍🟫 PowerPilz Companion

A Home Assistant custom integration that adds two **Smart helper** types, each designed to collapse a multi-entity + bridging-automation setup into a single entity that drives your devices autonomously:

- 🗓 **Smart Schedule** — a `select` entity wrapping a target device, three override modes (Off / On / Auto) and a linked native Schedule helper for the weekly plan.
- ⏱ **Smart Timer** — a `switch` entity that autonomously turns a target on at a configured start time and off at an end time (one-shot), with customizable icons, labels and direction.

Companion to the [PowerPilz](https://github.com/gregor-autischer/PowerPilz) Lovelace cards pack — use them together for a drastically simpler dashboard setup, or use the Companion standalone with any other cards.

## Why

Home Assistant's native building blocks (`schedule`, `input_datetime`, `input_boolean`) are great primitives but don't do anything by themselves — you always need bridging automations to turn devices on/off at the right time, plus manual-override glue to handle "I want it on right now" cases.

Each Companion helper bundles all of that logic into one entity:

| Traditional HA setup | Companion replacement |
| :-- | :-- |
| schedule helper + switch + input_select mode + 2 automations | **one** Smart Schedule entity |
| switch + 2 input_datetimes + input_boolean + 2 automations | **one** Smart Timer entity |

## Features

### Smart Schedule

- Single `select.*` entity exposing three modes (renameable, with custom icons): Off / On / Auto
- Auto-linked native HA `schedule.*` helper (created automatically on setup, editable via HA's native drag-and-drop FullCalendar UI)
- Optional: pick an existing schedule helper instead of auto-creating one
- In **Auto** mode: mirrors the linked schedule's on/off state onto the target device
- In **Off** / **On** mode: forces the target to the corresponding state
- Optional "resume Auto at next schedule boundary" — a manual override is automatically lifted at the next on/off transition (Nest-thermostat style)
- Auto-cleanup: removing the helper deletes the auto-created linked schedule too

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
- No external dependencies beyond HA's built-in `schedule` component

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
   - **Linked schedule helper** — leave empty to auto-create a new `schedule.living_room_heating`, or pick an existing `schedule.*` to link to that one
   - **Mode names / icons** — customize how Off / On / Auto appear
   - **Resume Auto on next schedule boundary** — whether manual overrides auto-lift

On confirmation you get:

- `select.living_room_heating` — the Smart Schedule entity (three modes)
- `schedule.living_room_heating` — the native weekly schedule (empty; ready to fill in via HA's drag-and-drop UI)

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

- **Smart Schedule weekly plan** — open the linked `schedule.*` entry under Settings → Helpers and use HA's native drag-and-drop UI. Changes propagate to the Smart Schedule in real time.
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

## Lovelace integration

The [PowerPilz](https://github.com/gregor-autischer/PowerPilz) dashboard card pack has native "Companion mode" for both Schedule and Timer cards: turn one toggle on and you only need to reference the Smart helper entity — the card derives all the sub-entities from its attributes.

| Companion helper | Paired card | Attribute the card reads |
| :-- | :-- | :-- |
| Smart Schedule select | [Schedule card](https://github.com/gregor-autischer/PowerPilz/blob/main/docs/cards/schedule.md) | `linked_schedule`, `target_entity`, `mode_names`, `mode_icons` |
| Smart Timer switch | [Timer card](https://github.com/gregor-autischer/PowerPilz/blob/main/docs/cards/timer.md) | `target_entity`, `on_datetime`, `off_datetime`, `direction`, `state_names`, `state_icons` |

Both cards stay fully compatible without the Companion installed — the new behaviour is opt-in.

## Exposed attributes

### Smart Schedule `select.*`

| Attribute | Description |
| :-- | :-- |
| `logical_mode` | `off` / `on` / `auto` (stable key, regardless of renamed display names) |
| `mode_names` | `{off, on, auto}` → configured display name |
| `mode_icons` | `{off, on, auto}` → configured MDI icon |
| `target_entity` / `target_state` | Controlled device + its current state |
| `linked_schedule` | Linked `schedule.*` entity ID |
| `schedule_state` | Live `on` / `off` of the linked schedule |
| `next_event` | ISO timestamp of the next on/off transition |

### Smart Timer `switch.*`

| Attribute | Description |
| :-- | :-- |
| `target_entity` / `target_state` | Controlled device + current state |
| `on_datetime` / `off_datetime` | Scheduled on/off times (ISO 8601) |
| `next_event` | ISO timestamp of the next boundary that will fire |
| `direction` | `on_only` / `both` / `off_only` |
| `state_names` | `{inactive, active}` → display label |
| `state_icons` | `{inactive, active}` → MDI icon |

## How it works

### Smart Schedule

On setup the integration either reuses an existing `schedule.*` entity or creates one on the fly. Runtime creation reaches into HA's `DictStorageCollection` via the websocket-command registry (`hass.data["websocket_api"]["schedule/create"]` → bound method → `__self__` → live collection) so the new entity is registered **without an HA restart**. A direct Store-write fallback is kept in case HA internals change.

The `select` entity subscribes to the linked schedule's state via `async_track_state_change_event`. In Auto mode it mirrors the schedule's on/off onto the target. In Off / On it forces the target to that state.

On removal (`async_remove_entry`), any auto-created linked schedule is deleted too.

### Smart Timer

Pure `async_track_point_in_time` callbacks at the on/off datetimes. No polling. State and datetimes persist across restarts via `RestoreEntity`, so no config-entry writes are needed for per-timer state — which avoids reload loops.

For select / input_select targets the timer calls `<domain>.select_option`. When the target exposes a `mode_names` attribute (as the Smart Schedule's select does) the timer stores the **logical key** (`off` / `on` / `auto`) rather than the display name, and resolves to the current display name via `mode_names` at fire time — renaming a mode in the Schedule helper therefore doesn't break the timer binding.

## Relation to PowerPilz Cards

The [PowerPilz](https://github.com/gregor-autischer/PowerPilz) repository provides dashboard cards (Energy, Wallbox, Switch, Schedule, Timer, Graph, Graph-Stack). The Schedule and Timer cards there have a first-class "Companion mode" that works with the helpers from this integration.

**Installing both gives you:**

- The Companion integration does the heavy lifting (state machine, autonomous driving, reconciliation)
- The Cards provide the visual + interactive surface on dashboards
- Together: one Smart entity drives your device; one Lovelace card renders the full state with a single entity reference

**Using only the Cards (without this integration)** still works — the cards fall back to manual mode with multi-entity configuration and require the classic bridging automations.

**Using only the Companion (without the Cards)** also works — the Smart helpers are regular HA entities that appear in Auto-generated dashboards, can be triggered from automations/voice assistants, and can be controlled with any `select`/`switch` capable card.

## Development & releases

- Contributions welcome — open an issue or PR on [GitHub](https://github.com/gregor-autischer/PowerPilz-Companion).
- See [RELEASING.md](RELEASING.md) for the full release procedure (version bump, `main → release` fast-forward merge, and troubleshooting).

## License

Apache-2.0 — see [LICENSE](LICENSE).
