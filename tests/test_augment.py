"""The fragile bit: the person monkeypatch installs, restores, and — when core
has moved out from under it — raises a visible Repair instead of failing silent.
"""

from __future__ import annotations

import pytest
from custom_components.statecraft.augment import install_augmenter, remove_augmenter
from custom_components.statecraft.const import DOMAIN, ISSUE_PERSON_PATCH
from custom_components.statecraft.data import StatecraftData
from homeassistant.components.person import Person
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
