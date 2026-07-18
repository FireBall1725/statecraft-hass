"""The fragile bit: the person monkeypatch installs, restores, and — when core
has moved out from under it — raises a visible Repair instead of failing silent.
"""

from __future__ import annotations

import pytest
from custom_components.statecraft import const
from custom_components.statecraft.augment import (
    _apply_cascade,
    install_augmenter,
    remove_augmenter,
)
from custom_components.statecraft.const import DOMAIN, ISSUE_PERSON_PATCH
from custom_components.statecraft.data import StatecraftData
from custom_components.statecraft.evaluator import StateEngine
from custom_components.statecraft.models import parse_subject
from homeassistant.components.person import Person
from homeassistant.helpers import condition
from homeassistant.helpers import issue_registry as ir


@pytest.fixture(autouse=True)
def _restore_person():
    """Guarantee the global Person class is put back after each test."""
    orig_update = Person._update_state
    orig_added = Person.async_added_to_hass
    yield
    Person._update_state = orig_update
    Person.async_added_to_hass = orig_added


@pytest.fixture
def data(hass):
    d = StatecraftData(hass)
    hass.data[DOMAIN] = d
    return d


async def test_install_patches_the_person_internals(hass, data):
    orig = Person._update_state
    install_augmenter(hass)
    assert data.patched is True
    # If core ever renames _update_state, this assertion fails loudly here.
    assert Person._update_state is not orig
    remove_augmenter(hass)
    assert Person._update_state is orig
    assert data.patched is False


async def test_install_clears_a_stale_patch_issue(hass, data):
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_PERSON_PATCH,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_PERSON_PATCH,
    )
    install_augmenter(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PERSON_PATCH) is None


async def test_missing_core_internal_raises_repair_issue(hass, data, monkeypatch):
    # Simulate a Home Assistant update that renamed the wrapped internal.
    monkeypatch.delattr(Person, "_update_state", raising=False)
    install_augmenter(hass)
    assert data.patched is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PERSON_PATCH)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR


# --- per-state icons --------------------------------------------------------
class _FakeEntity:
    """Stands in for a core Person: the cascade only touches _attr_* + write."""

    def __init__(self, hass):
        self.hass = hass
        self._attr_state = None
        self._attr_icon = "mdi:stale"  # so a failure to clear is visible
        self._attr_extra_state_attributes: dict = {}
        self.writes = 0

    def async_write_ha_state(self):
        self.writes += 1


async def _engine(hass, states):
    # Conditions are stored already-validated (the config flow and ws_save both
    # validate before writing). Validation is what expands entity_id into a
    # list; skip it and async_from_config walks the string character by
    # character and every state silently evaluates false.
    validated = []
    for raw in states:
        checked = dict(raw)
        checked[const.CONF_CONDITION] = await condition.async_validate_condition_config(
            hass, checked[const.CONF_CONDITION]
        )
        validated.append(checked)

    subject = parse_subject(
        {const.CONF_SUBJECT: "person.a", const.CONF_SCOPE_TYPE: const.SCOPE_PERSON},
        {const.CONF_STATES: validated},
    )
    engine = StateEngine(hass, subject)
    await engine.async_build()  # compiles the condition checkers
    return engine


def _state_def(name, entity_id, icon=None):
    raw = {
        const.CONF_NAME: name,
        const.CONF_CONDITION: {
            "condition": "state",
            "entity_id": entity_id,
            "state": "on",
        },
    }
    if icon:
        raw[const.CONF_ICON] = icon
    return raw


async def test_cascade_sets_icon_of_winning_state(hass, data):
    hass.states.async_set("input_boolean.sleep", "on")
    await hass.async_block_till_done()
    engine = await _engine(
        hass, [_state_def("sleep", "input_boolean.sleep", "mdi:sleep")]
    )
    entity = _FakeEntity(hass)

    _apply_cascade(entity, engine, "home", None)

    assert entity._attr_state == "sleep"
    assert entity._attr_icon == "mdi:sleep"


async def test_cascade_clears_icon_when_state_has_none(hass, data):
    """Falling back to presence must not leave the previous state's icon."""
    hass.states.async_set("input_boolean.sleep", "off")
    await hass.async_block_till_done()
    engine = await _engine(
        hass, [_state_def("sleep", "input_boolean.sleep", "mdi:sleep")]
    )
    entity = _FakeEntity(hass)

    _apply_cascade(entity, engine, "home", None)

    assert entity._attr_state == "home"
    # None, not "": lets the person domain's icons.json default apply.
    assert entity._attr_icon is None


async def test_cascade_away_state_gets_away_icon(hass, data):
    """Renaming not_home -> away lost HA's away glyph; the fallback restores it."""
    engine = await _engine(hass, [])  # no named states, pure presence fallback
    entity = _FakeEntity(hass)

    _apply_cascade(entity, engine, "not_home", None)  # away_from default

    assert entity._attr_state == "away"
    assert entity._attr_icon == "mdi:account-arrow-right"


async def test_cascade_zone_passthrough_borrows_zone_icon(hass, data):
    """A person in a sub-zone reports the zone name; use that zone's icon."""
    hass.states.async_set(
        "zone.karate",
        "0",
        {"friendly_name": "Karate (Cambridge)", "icon": "mdi:karate"},
    )
    await hass.async_block_till_done()
    engine = await _engine(hass, [])
    entity = _FakeEntity(hass)

    _apply_cascade(entity, engine, "Karate (Cambridge)", None)

    assert entity._attr_state == "Karate (Cambridge)"
    assert entity._attr_icon == "mdi:karate"


async def test_cascade_zone_without_icon_falls_through(hass, data):
    """A matching zone with no icon attribute leaves the default in place."""
    hass.states.async_set(
        "zone.work",
        "0",
        {"friendly_name": "Work"},  # no icon
    )
    await hass.async_block_till_done()
    engine = await _engine(hass, [])
    entity = _FakeEntity(hass)

    _apply_cascade(entity, engine, "Work", None)

    assert entity._attr_state == "Work"
    assert entity._attr_icon is None
