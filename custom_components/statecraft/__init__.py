"""The Statecraft integration.

A "scope" (one config entry) attaches user-defined composite states to a
subject. Two kinds:

  person -> layer the states onto an existing person.* entity by wrapping core
            (augment.py); we never own the person entity.
  custom -> the integration owns a new statecraft.* entity and drives it from
            the same cascade (entity.py).

Both share the StateEngine (evaluator.py) and the editor panel (panel.py).
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
from .data import StatecraftData
from .entity import StatecraftScope, get_component
from .evaluator import StateEngine
from .models import parse_subject
from .panel import async_register_panel, async_unregister_panel

_LOGGER = logging.getLogger(__name__)


async def _get_data(hass: HomeAssistant) -> StatecraftData:
    # Store the singleton BEFORE the first await. If we awaited async_load()
    # between the check and the assignment, two entries setting up concurrently
    # (the normal cold-boot case) would each see "no data", each create their
    # own StatecraftData, and the last writer would win — stranding the other
    # entry's engine in an orphaned instance, leaving that person silently
    # un-augmented. The get-and-set below has no await, so it's atomic.
    data = hass.data.get(DOMAIN)
    if data is None:
        data = StatecraftData(hass)
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

    # The panel is cosmetic; its registration must never take down a scope. If
    # it fails, log and carry on so evaluation still runs.
    try:
        await async_register_panel(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "statecraft: panel registration failed for %s; the editor panel "
            "may be unavailable, but state evaluation will continue",
            subject.subject_entity_id,
        )

    if subject.is_custom:
        # Custom scope: create and drive our own statecraft.* entity.
        scope_entity = StatecraftScope(entry.entry_id, subject, engine)
        data.custom_entities[subject.subject_entity_id] = scope_entity
        await get_component(hass).async_add_entities([scope_entity])
    else:
        # Person scope: wrap core's Person and attach once it exists. On a cold
        # boot the person entity may not be registered when we set up, and
        # patching async_added_to_hass can miss it depending on load order.
        # async_at_started fires after all entities load (and immediately on a
        # reload), so it attaches reliably in both cases.
        install_augmenter(hass)

        @callback
        def _attach(*_: object) -> None:
            entity = get_person_entity(hass, subject.subject_entity_id)
            _LOGGER.debug(
                "attach for %s (entity %s)",
                subject.subject_entity_id,
                "found" if entity is not None else "missing",
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
    """Unload one scope."""
    data = await _get_data(hass)
    subject = parse_subject(dict(entry.data), dict(entry.options))
    subject_id = subject.subject_entity_id

    data.engines.pop(subject_id, None)

    if subject.is_custom:
        scope_entity = data.custom_entities.pop(subject_id, None)
        if scope_entity is not None:
            await scope_entity.async_remove(force_remove=True)
    else:
        runtime = data.runtime.pop(subject_id, None)
        if runtime is not None:
            runtime.detach()
        entity = get_person_entity(hass, subject_id)
        if entity is not None:
            entity._update_state()  # falls back to core presence (no engine now)

    # Restore core Person once no person scopes remain patched.
    if not any(not e.subject.is_custom for e in data.engines.values()):
        remove_augmenter(hass)
    if not data.engines:
        async_unregister_panel(hass)

    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options change, via HA's machinery.

    Calling async_unload_entry/async_setup_entry directly bypasses HA's
    config-entry unload bookkeeping, so the registered async_on_unload cleanups
    (update listener, async_at_started) never run and leak/accumulate across
    saves. async_reload does the full teardown + setup correctly.
    """
    await hass.config_entries.async_reload(entry.entry_id)
