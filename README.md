# Statecraft

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Hassfest](https://github.com/fireball1725/person-state-hass/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/fireball1725/person-state-hass/actions/workflows/hassfest.yaml)
[![HACS](https://github.com/fireball1725/person-state-hass/actions/workflows/hacs.yaml/badge.svg)](https://github.com/fireball1725/person-state-hass/actions/workflows/hacs.yaml)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=fireball1725&repository=person-state-hass&category=integration)

Statecraft turns a set of conditions into a single named state. You define states like `sleep`, `dnd`, `party`, or `night`, order them by priority, and the first one whose conditions match becomes the current state. Everything is authored from a sidebar panel.

A **scope** is one of these state machines. There are two kinds:

- **Person scope** layers the states onto an existing `person.*` entity. The person stays the one object you click on the map; its state reads `sleep` or `dnd` instead of just `home`. Zone names (Work, School) pass through unchanged, and `not_home` becomes your away state.
- **Custom scope** creates a new entity Statecraft owns and drives, for example `statecraft.house_state`. It has no presence, so when nothing matches it falls back to a default state you set (`idle` by default).

Both kinds share one condition engine, one editor, and one debug view.

> **Status: pre-1.0, under active development.** Expect breaking changes until 1.0.

## Install (HACS custom repository)

1. HACS, three-dot menu, Custom repositories.
2. Add `https://github.com/fireball1725/person-state-hass` as an Integration.
3. Install **Statecraft**, then restart Home Assistant.
4. Settings, Devices and Services, Add Integration, **Statecraft**. Pick Person or Custom.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=statecraft)

Then open the **Statecraft** panel in the sidebar to define the states.

## Defining a state

Each state has an **enter** condition and an optional **hold** condition, both authored the same way: a visual builder of entity rows (with AND/OR and an optional `for:` duration) or raw Home Assistant condition YAML.

A state is active when its enter condition is true, or when it was already active and its hold condition is still true. That second clause is generic hysteresis: the state stays latched until a condition you choose breaks it. To keep a person `sleep` in the morning until they open the door, the enter condition is "sleep window on and door closed" and the hold condition is "door closed". When the door opens, the hold goes false and the state drops.

The panel shows a plain-language summary of each rule and a **Debug** toggle that reports every row's live value, whether it passes, any `for:` countdown, and the engine's verdict for each state.

## How it works

A person scope does not own the person entity. It wraps two callbacks on core's `Person` (`_update_state` and `async_added_to_hass`) in `augment.py`, runs core first, then applies the cascade on top. The wrapping is pinned to a known Home Assistant version through `BUILT_AGAINST`; a core rename fails loud and falls back to plain presence rather than going silently wrong.

A custom scope owns its entity. It registers a `statecraft` entity domain and drives one `RestoreEntity` per scope from the same `StateEngine` (`entity.py`), subscribing to the entities its conditions reference plus timers for any `for:` windows.

The decision logic (first-match cascade, enter-or-hold latch, presence fallback) has no Home Assistant imports, so it stays testable on its own.

## Example: person state and attributes

```
person.adalea
  state: sleep            # sleep | dnd | away | home | <zone name>
  attributes:
    presence: home        # raw person state before the cascade
    sleep: true
    dnd: false
```

A custom scope publishes the same per-state booleans plus an `options` list of every value it can report.

## Caveat

The person path touches core internals on purpose. Re-test after Home Assistant upgrades that change the person component, and bump `BUILT_AGAINST` in `augment.py`.

## License

Copyright (C) 2026 FireBall1725. Licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
