"""The Person State integration.

Layers user-defined composite states and boolean attributes onto the core
person entity, instead of creating a parallel sensor. We never own the person
entity; we wrap its state computation (see augment.py).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.start import async_at_started

from .augment import (
    attach_listeners,
    get_person_entity,
    install_augmenter,
    remove_augmenter,
)
from .const import DOMAIN
from .data import PersonStateData
from .evaluator import StateEngine
from .models import parse_subject

_LOGGER = logging.getLogger(__name__)


async def _get_data(hass: HomeAssistant) -> PersonStateData:
    # Store the singleton BEFORE the first await. If we awaited async_load()
    # between the check and the assignment, two entries setting up concurrently
    # (the normal cold-boot case) would each see "no data", each create their
    # own PersonStateData, and the last writer would win — stranding the other
    # entry's engine in an orphaned instance, leaving that person silently
    # un-augmented. The get-and-set below has no await, so it's atomic.
    data = hass.data.get(DOMAIN)
    if data is None:
        data = PersonStateData(hass)
        hass.data[DOMAIN] = data
        await data.async_load()
    return data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one managed subject from a config entry."""
    data = await _get_data(hass)
    subject = parse_subject(dict(entry.data), dict(entry.options))

    engine = StateEngine(hass, subject)
    await engine.async_build()
    data.engines[subject.subject_entity_id] = engine

    install_augmenter(hass)

    # Attach once the person entity is guaranteed to exist. On a cold boot the
    # person entity may not be registered when we set up, and patching
    # async_added_to_hass can miss it depending on load order (one person wins
    # the race, another loses). async_at_started fires after all entities are
    # loaded, and immediately if HA is already running (e.g. a reload), so it
    # attaches reliably in both cases.
    @callback
    def _attach(*_: object) -> None:
        entity = get_person_entity(hass, subject.subject_entity_id)
        _LOGGER.info(
            "person_state: at_started attach for %s (entity %s)",
            subject.subject_entity_id,
            "found" if entity is not None else "MISSING",
        )
        if entity is None:
            _LOGGER.warning(
                "subject %s not found; cannot augment it",
                subject.subject_entity_id,
            )
            return
        restored = data.last_state.get(subject.subject_entity_id)
        if restored is not None:
            entity._attr_state = restored  # so first eval sees what we were
        attach_listeners(hass, entity, engine)

    entry.async_on_unload(async_at_started(hass, _attach))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one subject and restore plain presence."""
    data = await _get_data(hass)
    subject = parse_subject(dict(entry.data), dict(entry.options))
    subject_id = subject.subject_entity_id

    runtime = data.runtime.pop(subject_id, None)
    if runtime is not None:
        runtime.detach()
    data.engines.pop(subject_id, None)

    entity = get_person_entity(hass, subject_id)
    if entity is not None:
        entity._update_state()  # falls back to core presence (no engine now)

    if not data.engines:
        remove_augmenter(hass)

    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options change."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
