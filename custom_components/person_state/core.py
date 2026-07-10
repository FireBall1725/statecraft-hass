"""Pure decision logic for Person State.

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
) -> str:
    """First state whose flag is true wins; else fall back to presence.

    `presence` is core's person state (home, not_home, or a zone name). The
    away_from value becomes away_state; anything else (home, Work, School)
    passes straight through.
    """
    for name, on in active_in_order:
        if on:
            return name
    if presence == away_from:
        return away_state
    return presence or away_state
