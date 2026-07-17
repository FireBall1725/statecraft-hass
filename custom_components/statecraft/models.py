"""Data model for Statecraft, parsed from the config entry.

These are plain dataclasses with no Home Assistant runtime dependency so the
shape is easy to reason about and test. Condition configs are stored as the
native HA condition dicts (already validated by the config flow on save).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .const import (
    CONF_AWAY_FROM,
    CONF_AWAY_STATE,
    CONF_CONDITION,
    CONF_DEFAULT_STATE,
    CONF_GRACE,
    CONF_GRACE_DOOR,
    CONF_GRACE_OPEN_STATE,
    CONF_GRACE_SECONDS,
    CONF_HOLD,
    CONF_ICON,
    CONF_NAME,
    CONF_PERSIST,
    CONF_PERSIST_CLOSED_STATE,
    CONF_PERSIST_DOOR,
    CONF_PERSIST_WINDOW,
    CONF_PERSIST_WINDOW_OFF,
    CONF_SCOPE_NAME,
    CONF_SCOPE_TYPE,
    CONF_STATES,
    CONF_SUBJECT,
    DEFAULT_AWAY_FROM,
    DEFAULT_AWAY_STATE,
    DEFAULT_CLOSED_STATE,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_OPEN_STATE,
    DEFAULT_STATE,
    DEFAULT_WINDOW_OFF_STATE,
    SCOPE_PERSON,
)


@dataclass(frozen=True)
class StateDef:
    """One user-defined composite state.

    `hold` is an optional native HA condition. Once the state is active, it
    stays active while `hold` is true even after `condition` (the enter
    condition) goes false. That is the whole hysteresis story — no built-in
    notion of doors, windows, or time; the user wires it to their own flow.

    `icon` is optional. None means "no opinion", which lets the frontend fall
    back to the domain default (mdi:account for a person).
    """

    name: str
    condition: dict[str, Any]
    hold: dict[str, Any] | None
    icon: str | None = None


@dataclass(frozen=True)
class SubjectConfig:
    """Everything one config entry (one scope) manages."""

    subject_entity_id: str
    scope_type: str  # SCOPE_PERSON | SCOPE_CUSTOM
    states: tuple[StateDef, ...]  # priority order: first true wins
    # person scopes fall back to presence -> away; custom scopes have no
    # presence and fall back to default_state.
    away_from: str
    away_state: str
    default_state: str
    name: str | None  # friendly name (custom)
    icon: str | None  # mdi icon (custom)

    @property
    def is_custom(self) -> bool:
        return self.scope_type != SCOPE_PERSON

    def state_icon(self, state: str) -> str | None:
        """Return the icon for a state name, or None if it has no opinion.

        Fallback states (away_state, default_state, and pass-through presence
        values like "home" or a zone name) have no StateDef, so they land here
        as None and the frontend applies the domain default.
        """
        for sd in self.states:
            if sd.name == state:
                return sd.icon
        return None


def _legacy_hold(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an old grace/persist modifier into an equivalent hold condition.

    Kept so configs written before the generic hold existed keep working. New
    saves never write grace/persist, so this only runs for pre-existing states.
    """
    branches: list[dict[str, Any]] = []

    persist = raw.get(CONF_PERSIST)
    if persist:
        # Stayed asleep while the window helper was off and the door closed.
        branches.append(
            {
                "condition": "and",
                "conditions": [
                    {
                        "condition": "state",
                        "entity_id": persist[CONF_PERSIST_WINDOW],
                        "state": persist.get(
                            CONF_PERSIST_WINDOW_OFF, DEFAULT_WINDOW_OFF_STATE
                        ),
                    },
                    {
                        "condition": "state",
                        "entity_id": persist[CONF_PERSIST_DOOR],
                        "state": persist.get(
                            CONF_PERSIST_CLOSED_STATE, DEFAULT_CLOSED_STATE
                        ),
                    },
                ],
            }
        )

    grace = raw.get(CONF_GRACE)
    if grace:
        secs = int(float(grace.get(CONF_GRACE_SECONDS, DEFAULT_GRACE_SECONDS)))
        open_state = grace.get(CONF_GRACE_OPEN_STATE, DEFAULT_OPEN_STATE)
        # A door-open trip still counts while it has been open less than `secs`:
        # door is open AND has NOT been open for the full grace window.
        branches.append(
            {
                "condition": "and",
                "conditions": [
                    {
                        "condition": "state",
                        "entity_id": grace[CONF_GRACE_DOOR],
                        "state": open_state,
                    },
                    {
                        "condition": "not",
                        "conditions": [
                            {
                                "condition": "state",
                                "entity_id": grace[CONF_GRACE_DOOR],
                                "state": open_state,
                                "for": {"seconds": secs},
                            }
                        ],
                    },
                ],
            }
        )

    if not branches:
        return None
    if len(branches) == 1:
        return branches[0]
    return {"condition": "or", "conditions": branches}


def _parse_hold(raw: dict[str, Any]) -> dict[str, Any] | None:
    hold = raw.get(CONF_HOLD)
    if hold:
        return hold
    return _legacy_hold(raw)


def parse_subject(data: dict[str, Any], options: dict[str, Any]) -> SubjectConfig:
    """Build a SubjectConfig from entry.data + entry.options."""
    merged = {**data, **options}
    states = tuple(
        StateDef(
            name=raw[CONF_NAME],
            condition=raw[CONF_CONDITION],
            hold=_parse_hold(raw),
            icon=raw.get(CONF_ICON) or None,
        )
        for raw in merged.get(CONF_STATES, [])
    )
    return SubjectConfig(
        subject_entity_id=merged[CONF_SUBJECT],
        scope_type=merged.get(CONF_SCOPE_TYPE, SCOPE_PERSON),
        states=states,
        away_from=merged.get(CONF_AWAY_FROM, DEFAULT_AWAY_FROM),
        away_state=merged.get(CONF_AWAY_STATE, DEFAULT_AWAY_STATE),
        default_state=merged.get(CONF_DEFAULT_STATE, DEFAULT_STATE),
        name=merged.get(CONF_SCOPE_NAME),
        icon=merged.get(CONF_ICON),
    )


def collect_for_horizons(config: Any) -> list[float]:
    """Walk a condition config and collect every `for:` duration in seconds.

    Lets the engine schedule a precise re-evaluation when a `for:` window is due
    to elapse, instead of relying only on the periodic safety net.
    """
    horizons: list[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "for":
                    secs = _to_seconds(value)
                    if secs is not None:
                        horizons.append(secs)
                else:
                    _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(config)
    return horizons


def collect_for_targets(config: Any) -> list[tuple[str, float]]:
    """Collect (entity_id, for_seconds) for every condition that has a `for:`.

    Lets the engine schedule a re-evaluation at the *actual* boundary
    (entity.last_changed + for), which is stable against unrelated state-changed
    events that would otherwise keep pushing a "now + for" timer forward.
    """
    targets: list[tuple[str, float]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "for" in node and "entity_id" in node:
                secs = _to_seconds(node["for"])
                if secs:
                    eids = node["entity_id"]
                    if isinstance(eids, str):
                        eids = [eids]
                    for eid in eids:
                        targets.append((eid, secs))
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(config)
    return targets


def _to_seconds(value: Any) -> float | None:
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        td = timedelta(
            hours=float(value.get("hours", 0)),
            minutes=float(value.get("minutes", 0)),
            seconds=float(value.get("seconds", 0)),
        )
        return td.total_seconds()
    if isinstance(value, str):
        parts = value.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        while len(nums) < 3:
            nums.insert(0, 0.0)
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None
