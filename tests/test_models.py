"""Subject parsing, for: collection, and legacy-hold conversion."""

from __future__ import annotations

from custom_components.statecraft import const
from custom_components.statecraft.models import (
    collect_for_horizons,
    collect_for_targets,
    parse_subject,
)


def _state(name, condition, **extra):
    return {const.CONF_NAME: name, const.CONF_CONDITION: condition, **extra}


def test_parse_person_subject():
    data = {const.CONF_SUBJECT: "person.a", const.CONF_SCOPE_TYPE: const.SCOPE_PERSON}
    options = {
        const.CONF_STATES: [
            _state("sleep", {"condition": "state", "entity_id": "x.y", "state": "on"})
        ]
    }
    subj = parse_subject(data, options)
    assert subj.subject_entity_id == "person.a"
    assert subj.is_custom is False
    assert len(subj.states) == 1
    assert subj.states[0].name == "sleep"
    assert subj.states[0].hold is None


def test_parse_custom_subject_defaults():
    data = {
        const.CONF_SUBJECT: "statecraft.house",
        const.CONF_SCOPE_TYPE: const.SCOPE_CUSTOM,
    }
    subj = parse_subject(data, {})
    assert subj.is_custom is True
    assert subj.default_state == const.DEFAULT_STATE


def test_hold_condition_is_kept():
    hold = {"condition": "state", "entity_id": "binary_sensor.door", "state": "off"}
    subj = parse_subject(
        {const.CONF_SUBJECT: "person.a"},
        {const.CONF_STATES: [_state("sleep", {"condition": "state"}, hold=hold)]},
    )
    assert subj.states[0].hold == hold


def test_legacy_grace_converts_to_hold():
    grace = {
        const.CONF_GRACE_DOOR: "binary_sensor.door",
        const.CONF_GRACE_OPEN_STATE: "on",
        const.CONF_GRACE_SECONDS: 120,
    }
    subj = parse_subject(
        {const.CONF_SUBJECT: "person.a"},
        {
            const.CONF_STATES: [
                _state("sleep", {"condition": "state"}, **{const.CONF_GRACE: grace})
            ]
        },
    )
    # The legacy grace becomes a real hold condition so old configs keep working.
    assert subj.states[0].hold is not None
    assert subj.states[0].hold["condition"] == "and"


def test_collect_for_horizons_and_targets():
    cond = {
        "condition": "state",
        "entity_id": "binary_sensor.door",
        "state": "off",
        "for": {"minutes": 3},
    }
    assert collect_for_horizons(cond) == [180.0]
    assert collect_for_targets(cond) == [("binary_sensor.door", 180.0)]
