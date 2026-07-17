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
    assert subj.states[0].icon is None


def _icon_subject(*states):
    return parse_subject(
        {const.CONF_SUBJECT: "person.a", const.CONF_SCOPE_TYPE: const.SCOPE_PERSON},
        {const.CONF_STATES: list(states)},
    )


def test_parse_state_icon():
    cond = {"condition": "state", "entity_id": "x.y", "state": "on"}
    subj = _icon_subject(_state("sleep", cond, icon="mdi:sleep"))
    assert subj.states[0].icon == "mdi:sleep"
    assert subj.state_icon("sleep") == "mdi:sleep"


def test_state_icon_absent_or_blank_is_none():
    """A missing key and an empty string both mean 'no opinion', not ''.

    An empty string would reach attributes.icon and shadow the domain default.
    """
    cond = {"condition": "state", "entity_id": "x.y", "state": "on"}
    subj = _icon_subject(_state("sleep", cond), _state("dnd", cond, icon=""))
    assert subj.state_icon("sleep") is None
    assert subj.state_icon("dnd") is None


def test_state_icon_unknown_name_is_none():
    """Fallback states (away/home/zone names) have no StateDef."""
    cond = {"condition": "state", "entity_id": "x.y", "state": "on"}
    subj = _icon_subject(_state("sleep", cond, icon="mdi:sleep"))
    assert subj.state_icon("away") is None
    assert subj.state_icon("home") is None


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
