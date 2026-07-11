"""Custom-scope entity: a statecraft.* entity the integration owns and drives.

A person scope layers states onto an existing person entity (augment.py). A
custom scope has no host entity, so we create one here and drive its state from
the same StateEngine cascade. There is no presence for a custom scope, so the
fallback when nothing matches is the scope's `default_state`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_ICON, DOMAIN, SAFETY_REEVAL_SECONDS
from .evaluator import StateEngine
from .models import SubjectConfig

_LOGGER = logging.getLogger(__name__)

_COMPONENT_KEY = f"{DOMAIN}_component"


def get_component(hass: HomeAssistant) -> EntityComponent:
    """Return the shared EntityComponent that hosts every statecraft.* entity.

    Created lazily on the first custom scope; reused for the rest. This is what
    lets us own a whole `statecraft.` entity domain the way core `person` or
    `zone` own theirs.
    """
    component = hass.data.get(_COMPONENT_KEY)
    if component is None:
        component = EntityComponent(_LOGGER, DOMAIN, hass)
        hass.data[_COMPONENT_KEY] = component
    return component


class StatecraftScope(RestoreEntity):
    """One custom scope, e.g. statecraft.house_state."""

    _attr_should_poll = False

    def __init__(
        self, entry_id: str, subject: SubjectConfig, engine: StateEngine
    ) -> None:
        self._subject = subject
        self._engine = engine
        self._attr_unique_id = entry_id
        self._attr_name = subject.name or subject.subject_entity_id.split(".", 1)[-1]
        self._attr_icon = subject.icon or DEFAULT_ICON
        # Pin the entity_id to the id chosen at config time (statecraft.<slug>).
        self.entity_id = subject.subject_entity_id
        self._state = subject.default_state
        self._attrs: dict[str, Any] = {"options": self._options()}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timers: list[CALLBACK_TYPE] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs

    def _options(self) -> list[str]:
        """Every value this scope can report: its state names + the default."""
        names = [sd.name for sd in self._subject.states]
        if self._subject.default_state not in names:
            names.append(self._subject.default_state)
        return names

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            self._state = last.state  # so the first eval sees what we were
        self._attach()
        self._recompute()

    async def async_will_remove_from_hass(self) -> None:
        self._detach()

    # --- listeners ----------------------------------------------------------
    def _attach(self) -> None:
        horizons = sorted({round(h) + 1 for h in self._engine.for_horizons if h > 0})
        # Never watch ourselves: we write our own state, and watching it would
        # turn that write into a change event that re-triggers evaluation.
        watched = [e for e in self._engine.entities if e != self.entity_id]

        @callback
        def _changed(*_: object) -> None:
            self._recompute()
            for cancel in self._timers:
                cancel()
            self._timers.clear()
            for delay in horizons:
                self._timers.append(
                    async_call_later(self.hass, delay, self._recompute)
                )

        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, _changed)
            )
        # slow safety net so anything not scheduled precisely still converges
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._recompute, timedelta(seconds=SAFETY_REEVAL_SECONDS)
            )
        )

    def _detach(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._timers:
            cancel()
        self._timers.clear()

    # --- evaluation ---------------------------------------------------------
    @callback
    def _recompute(self, *_: object) -> None:
        if not self._engine.allow_apply():
            return
        # No presence for a custom scope; pick_state falls back to default_state.
        state, active = self._engine.evaluate(None, self._state)
        self._state = state
        attrs: dict[str, Any] = dict(active)
        attrs["options"] = self._options()
        self._attrs = attrs
        if self.hass is not None:
            self.async_write_ha_state()
