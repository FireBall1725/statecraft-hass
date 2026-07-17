"""Constants for the Statecraft integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "statecraft"

# Core integration we augment. We do not own the person entity; we layer state
# and attributes onto it. Keep in sync with HA's person component domain.
PERSON_DOMAIN: Final = "person"

# --- Config entry keys ------------------------------------------------------
# A "scope" is one config entry: a subject plus its ordered states. Two kinds:
#   person -> attach the states onto an existing person.* entity (we wrap core)
#   custom -> the integration owns a new statecraft.<slug> entity it drives
CONF_SCOPE_TYPE: Final = "scope_type"
SCOPE_PERSON: Final = "person"
SCOPE_CUSTOM: Final = "custom"

CONF_SUBJECT: Final = (
    "subject"  # the entity: person.* (person) or statecraft.* (custom)
)
CONF_SCOPE_NAME: Final = "scope_name"  # friendly name for a custom scope
# Optional mdi icon. Scope-level (entry.data) it is the custom scope entity's
# base icon; inside a state definition it is that state's icon. The two live in
# different dicts, so the same key cannot collide.
CONF_ICON: Final = "icon"
CONF_DEFAULT_STATE: Final = "default_state"  # custom fallback when nothing matches
CONF_AWAY_FROM: Final = "away_from"  # presence value treated as "gone" (person)
CONF_AWAY_STATE: Final = "away_state"  # what to call it (person)
CONF_STATES: Final = "states"  # ordered list of state definitions

# Per-state-definition keys
CONF_NAME: Final = "name"
CONF_CONDITION: Final = "condition"  # native HA condition config
# A state may also carry CONF_ICON (see above).

# Latch: once the state is active, it stays active while this native HA
# condition is true, even after the enter condition goes false. Generic
# hysteresis — the user points it at whatever their flow needs. Replaces the
# old door/window-specific grace + persist modifiers.
CONF_HOLD: Final = "hold"  # compiled hold condition (engine reads this)
CONF_HOLD_BUILDER: Final = "hold_builder"  # builder rows for the panel round-trip

# Legacy per-state keys, kept only so old stored configs convert to a hold
# condition on load (see models._legacy_hold). Not written by new saves.
CONF_GRACE: Final = "grace"
CONF_PERSIST: Final = "persist"

# Grace modifier keys (door-open trip still counts as the state for a while)
CONF_GRACE_DOOR: Final = "door_entity_id"
CONF_GRACE_OPEN_STATE: Final = "open_state"
CONF_GRACE_SECONDS: Final = "seconds"

# Persist modifier keys (stay in the state while a window helper is off)
CONF_PERSIST_WINDOW: Final = "window_entity_id"
CONF_PERSIST_WINDOW_OFF: Final = "window_off_state"
CONF_PERSIST_DOOR: Final = "door_entity_id"
CONF_PERSIST_CLOSED_STATE: Final = "closed_state"

# --- Defaults ---------------------------------------------------------------
DEFAULT_AWAY_FROM: Final = "not_home"
DEFAULT_AWAY_STATE: Final = "away"
DEFAULT_STATE: Final = "idle"  # custom scope fallback
DEFAULT_ICON: Final = "mdi:state-machine"
DEFAULT_OPEN_STATE: Final = "on"
DEFAULT_CLOSED_STATE: Final = "off"
DEFAULT_WINDOW_OFF_STATE: Final = "off"
DEFAULT_GRACE_SECONDS: Final = 300

# Safety re-evaluation cadence so any condition `for:` we failed to schedule
# precisely still converges. Event-driven updates are immediate; this is a net.
SAFETY_REEVAL_SECONDS: Final = 60

# Native presence value for "home" (zone names pass through the cascade).
PRESENCE_HOME: Final = "home"

# --- Attributes published onto the subject entity ---------------------------
ATTR_PRESENCE: Final = "presence"  # raw state before the cascade

# Storage for restoring the last composite state across restarts.
STORAGE_KEY: Final = f"{DOMAIN}.last_state"
STORAGE_VERSION: Final = 1

# --- Repairs ----------------------------------------------------------------
# Raised when the person monkeypatch can't install (core renamed the internals
# it wraps, or the person component is gone), so person scopes have silently
# fallen back to plain presence. Turns a one-line log into a visible Repair.
ISSUE_PERSON_PATCH: Final = "person_patch_disabled"

# --- Services ---------------------------------------------------------------
# Manual override: pin a scope to a state for a while, then revert to automatic.
SERVICE_SET_OVERRIDE: Final = "set_override"
SERVICE_CLEAR_OVERRIDE: Final = "clear_override"
ATTR_STATE: Final = "state"
ATTR_DURATION: Final = "duration"
