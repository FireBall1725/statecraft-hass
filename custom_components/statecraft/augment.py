"""Augment the core person entity with composite state and attributes.

This is the deliberately-fragile bit. We do not own the person entity, so we
wrap two of its internal callbacks:

  Person._update_state        -> run core, then layer our cascade on top
  Person.async_added_to_hass  -> run core, then attach our source listeners

Everything we touch is guarded so a core rename fails loudly (error in the log,
person falls back to plain presence) instead of silently producing wrong state.
Pinned against the HA person component as of BUILT_AGAINST; re-test on upgrades.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    ATTR_PRESENCE,
    DOMAIN,
    ISSUE_PERSON_PATCH,
    PERSON_DOMAIN,
    SAFETY_REEVAL_SECONDS,
)

if TYPE_CHECKING:
    from .data import StatecraftData
    from .evaluator import StateEngine

_LOGGER = logging.getLogger(__name__)

# HA person component version this wrapper was validated against.
BUILT_AGAINST = "2026.6"


@callback
def _noop() -> None:
    """Swallow a call. Used to suppress core's intermediate state write."""


def _data(hass: HomeAssistant) -> StatecraftData:
    return hass.data[DOMAIN]


def _engine_for(entity) -> StateEngine | None:
    """Return the engine for this person entity, or None if unmanaged."""
    hass = getattr(entity, "hass", None)
    entity_id = getattr(entity, "entity_id", None)
    if hass is None or entity_id is None:
        return None
    return _data(hass).engines.get(entity_id)


# --- the patch --------------------------------------------------------------
def install_augmenter(hass: HomeAssistant) -> None:
    """Monkeypatch the core Person entity. Idempotent."""
    data = _data(hass)
    if data.patched:
        return

    try:
        from homeassistant.components.person import Person  # noqa: PLC0415
    except ImportError:  # pragma: no cover - person is core
        _LOGGER.error("person component not importable; augmenter disabled")
        _raise_patch_issue(hass)
        return

    if not hasattr(Person, "_update_state") or not hasattr(
        Person, "async_added_to_hass"
    ):
        _LOGGER.error(
            "core person internals changed (built against %s); augmenter disabled, "
            "people will show plain presence",
            BUILT_AGAINST,
        )
        _raise_patch_issue(hass)
        return

    orig_update = Person._update_state
    orig_added = Person.async_added_to_hass

    @callback
    def _patched_update(self) -> None:
        # capture previous *composite* state before core overwrites it; the
        # grace/persist modifiers need to know what we were.
        previous_state = getattr(self, "_attr_state", None)
        # Core's _update_state writes the plain presence to the state machine
        # itself. Left alone, every recompute emits a one-frame flap to the
        # plain presence (e.g. "home") before our cascade overwrites it with the
        # composite state (e.g. "dnd"). Suppress that intermediate write so we
        # publish the state exactly once, after the cascade.
        real_write = self.async_write_ha_state
        self.async_write_ha_state = _noop  # type: ignore[method-assign]
        try:
            orig_update(self)
        finally:
            # remove the instance shadow so the class method is used again
            try:
                del self.async_write_ha_state
            except AttributeError:  # pragma: no cover - defensive
                self.async_write_ha_state = real_write  # type: ignore[method-assign]
        engine = _engine_for(self)
        if engine is None:
            # unmanaged person: emit the plain presence core just computed
            self.async_write_ha_state()
            return
        presence = getattr(self, "_attr_state", None)  # core just set this
        # never let our layer break core's person update: on any failure the
        # entity keeps the plain presence core just wrote.
        try:
            _apply_cascade(self, engine, presence, previous_state)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "statecraft cascade failed for %s; left plain presence",
                getattr(self, "entity_id", "?"),
            )
            self.async_write_ha_state()

    async def _patched_added(self) -> None:
        await orig_added(self)
        engine = _engine_for(self)
        if engine is None:
            return
        try:
            attach_listeners(self.hass, self, engine)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "statecraft failed to attach listeners for %s",
                getattr(self, "entity_id", "?"),
            )

    # Monkeypatching core methods is the whole point; mypy can't model it.
    Person._update_state = _patched_update  # type: ignore[method-assign]
    Person.async_added_to_hass = _patched_added  # type: ignore[method-assign]
    data.patched = True
    data.orig_update = orig_update
    data.orig_added = orig_added
    # Success clears any stale "patch disabled" repair from a prior boot.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_PERSON_PATCH)
    _LOGGER.debug("person augmenter installed (built against %s)", BUILT_AGAINST)


def _raise_patch_issue(hass: HomeAssistant) -> None:
    """Surface the silent person-patch failure as a visible Repair."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_PERSON_PATCH,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_PERSON_PATCH,
        translation_placeholders={"built_against": BUILT_AGAINST},
    )


def remove_augmenter(hass: HomeAssistant) -> None:
    """Restore core Person methods. Call when the last entry unloads."""
    data = _data(hass)
    if not data.patched:
        return
    try:
        from homeassistant.components.person import Person  # noqa: PLC0415

        if data.orig_update is not None:
            Person._update_state = data.orig_update  # type: ignore[method-assign]
        if data.orig_added is not None:
            Person.async_added_to_hass = data.orig_added  # type: ignore[method-assign]
    except ImportError:  # pragma: no cover
        pass
    data.patched = False
    data.orig_update = None
    data.orig_added = None


# --- cascade application ----------------------------------------------------
@callback
def _apply_cascade(
    entity, engine: StateEngine, presence: str | None, previous_state: str | None
) -> None:
    """Override the plain presence with the composite state + attrs.

    `presence` is the raw presence value (core's person state, before our
    layer). On core-driven updates it is what core just computed; on our own
    source/timer re-evaluations presence has not changed, so the caller passes
    the last-known value it stashed in the ATTR_PRESENCE attribute.
    """
    hass = entity.hass

    # First cascade after (re)build: restore the saved composite state and start
    # the reboot bridge. This must happen on the FIRST evaluation, not later via
    # async_at_started: on a cold boot the person entity already exists when we
    # patch it, so a device-tracker update can run the cascade before the
    # restore would otherwise fire, and remember() would overwrite the persisted
    # state with the fallback. Priming here uses the saved value as previous.
    if not engine.primed:
        engine.primed = True
        saved = _data(hass).last_state.get(engine.subject.subject_entity_id)
        if saved is not None:
            previous_state = saved
        engine.begin_bridge(saved)

    # Circuit breaker: if we're being re-applied in a tight burst, a feedback
    # loop is in progress. Stop applying (leave plain presence) so HA can't be
    # hung, and log once with the watched entities to pinpoint the source.
    if not engine.allow_apply():
        if not getattr(engine, "_breaker_logged", False):
            engine._breaker_logged = True
            _LOGGER.error(
                "statecraft: circuit breaker tripped for %s — a feedback loop "
                "is re-triggering the cascade. Watched entities: %s. Leaving "
                "plain presence; fix the state config and reload to re-enable.",
                engine.subject.subject_entity_id,
                sorted(engine.entities),
            )
        entity._attr_state = presence
        entity.async_write_ha_state()
        return

    state, active = engine.evaluate(presence, previous_state)

    entity._attr_state = state

    attrs = dict(getattr(entity, "_attr_extra_state_attributes", None) or {})
    attrs[ATTR_PRESENCE] = presence
    for name, is_active in active.items():
        attrs[name] = is_active
    entity._attr_extra_state_attributes = attrs

    _data(hass).remember(engine.subject.subject_entity_id, state)
    entity.async_write_ha_state()


# --- listeners --------------------------------------------------------------
def attach_listeners(hass: HomeAssistant, entity, engine: StateEngine) -> None:
    """Wire up source + timer triggers for one subject.

    Presence changes are already handled: core recomputes the person on tracker
    updates, which runs our patched _update_state. We add the source entities
    every state references, plus timers for `for:`/grace horizons, plus a slow
    safety re-eval.
    """
    runtime = _data(hass).runtime.setdefault(
        engine.subject.subject_entity_id, RuntimeListeners()
    )
    runtime.detach()

    @callback
    def _recompute(*_: object) -> None:
        # Re-evaluate through core's _update_state. Core recomputes the true
        # plain presence from the device trackers, then our patched wrapper
        # runs the cascade on top. We must NOT try to derive presence ourselves
        # here: on the first run after a restore, _attr_state holds the restored
        # *composite* state (e.g. "dnd"), and treating that as presence locks
        # the person into it permanently. Core's intermediate write is
        # suppressed in _patched_update, so this no longer flaps.
        entity._update_state()
        # Re-arm `for:` boundary timers from the real last_changed of each
        # watched entity. Doing this every recompute (not just on a source
        # change) means an unrelated update reschedules to the same absolute
        # boundary instead of pushing it out, so a `for:` fires on time.
        runtime.cancel_timers()
        for delay in engine.pending_for_delays():
            runtime.timers.append(async_call_later(hass, delay, _recompute))

    # Never watch the subject entity itself. We write to it in the cascade, so
    # watching it would turn our own write into a state-changed event that
    # re-triggers the cascade — a tight feedback loop that hangs HA. The
    # subject's real changes still come through core's tracker path (the patched
    # _update_state), so excluding it here loses nothing.
    watched = [e for e in engine.entities if e != engine.subject.subject_entity_id]

    if watched:
        runtime.unsubs.append(async_track_state_change_event(hass, watched, _recompute))

    # slow safety net so anything we failed to schedule precisely still converges
    runtime.unsubs.append(
        async_track_time_interval(
            hass, _recompute, timedelta(seconds=SAFETY_REEVAL_SECONDS)
        )
    )

    _LOGGER.debug(
        "attached %s, watching %d source entities",
        engine.subject.subject_entity_id,
        len(watched),
    )

    # run once now so the composite state is correct immediately
    _recompute()


class RuntimeListeners:
    """Subscription handles for one managed subject."""

    def __init__(self) -> None:
        self.unsubs: list[CALLBACK_TYPE] = []
        self.timers: list[CALLBACK_TYPE] = []

    def cancel_timers(self) -> None:
        for cancel in self.timers:
            cancel()
        self.timers.clear()

    def detach(self) -> None:
        for unsub in self.unsubs:
            unsub()
        self.unsubs.clear()
        self.cancel_timers()


def get_person_entity(hass: HomeAssistant, entity_id: str):
    """Return the live core Person entity instance, or None if not loaded yet."""
    from homeassistant.helpers.entity_component import DATA_INSTANCES  # noqa: PLC0415

    component = hass.data.get(DATA_INSTANCES, {}).get(PERSON_DOMAIN)
    if component is None:
        return None
    for entity in component.entities:
        if entity.entity_id == entity_id:
            return entity
    return None
