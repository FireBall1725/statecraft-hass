"""Pure cascade logic (core.pick_state) — no Home Assistant needed."""

from __future__ import annotations

from custom_components.statecraft.core import pick_state


def test_first_true_state_wins():
    ordered = [("dnd", False), ("sleep", True), ("away", True)]
    assert pick_state(ordered, "home", "not_home", "away", "idle") == "sleep"


def test_custom_scope_falls_back_to_default_when_nothing_matches():
    # presence=None marks a custom scope.
    assert pick_state([("party", False)], None, "not_home", "away", "idle") == "idle"


def test_person_away_maps_presence_to_away_state():
    assert pick_state([], "not_home", "not_home", "away", "idle") == "away"


def test_person_home_and_zones_pass_through():
    assert pick_state([], "home", "not_home", "away", "idle") == "home"
    assert pick_state([], "Work", "not_home", "away", "idle") == "Work"


def test_active_state_beats_presence():
    assert pick_state([("sleep", True)], "home", "not_home", "away", "idle") == "sleep"
