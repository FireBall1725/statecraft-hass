"""Pure decision logic for Statecraft.

No Home Assistant imports on purpose: the cascade is a plain function over
primitive values, so it is trivial to unit test. Everything HA-aware (building
condition checkers, reading entity states, the enter/hold latch) lives in
evaluator.py.
"""

from __future__ import annotations


def pick_state(
    active_in_order: list[tuple[str, bool]],
    presence: str | None,
    away_from: str,
    away_state: str,
    default_state: str,
) -> str:
    """First state whose flag is true wins; else fall back.

    Person scopes pass a `presence` (core's person state: home, not_home, or a
    zone name); the away_from value becomes away_state and anything else (home,
    Work, School) passes straight through. Custom scopes have no presence, so
    they pass presence=None and fall back to default_state.
    """
    for name, on in active_in_order:
        if on:
            return name
    if presence is None:
        return default_state
    if presence == away_from:
        return away_state
    return presence or away_state
