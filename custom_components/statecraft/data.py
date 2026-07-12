"""Runtime data + persistence for Statecraft."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

if TYPE_CHECKING:
    from .augment import RuntimeListeners
    from .entity import StatecraftScope
    from .evaluator import StateEngine


class StatecraftData:
    """Everything the integration keeps in hass.data[DOMAIN]."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # subject_entity_id -> compiled engine (one config entry per subject)
        self.engines: dict[str, StateEngine] = {}
        # subject_entity_id -> live listener handles (person scopes)
        self.runtime: dict[str, RuntimeListeners] = {}
        # subject_entity_id -> owned entity (custom scopes)
        self.custom_entities: dict[str, StatecraftScope] = {}
        # last composite state per subject, restored across restarts
        self.last_state: dict[str, str] = {}

        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # monkeypatch bookkeeping (see augment.py). These hold unbound core
        # Person methods, so they take the entity as first arg, not CALLBACK_TYPE.
        self.patched: bool = False
        self.orig_update: Callable[..., Any] | None = None
        self.orig_added: Callable[..., Any] | None = None

        # sidebar panel registration flags (see panel.py)
        self.panel_flags: dict[str, bool] = {}

    async def async_load(self) -> None:
        """Load persisted last-state map."""
        data = await self._store.async_load()
        if isinstance(data, dict):
            self.last_state = dict(data)

    def remember(self, subject_entity_id: str, state: str) -> None:
        """Record the latest composite state and persist lazily."""
        if self.last_state.get(subject_entity_id) == state:
            return
        self.last_state[subject_entity_id] = state
        self._store.async_delay_save(lambda: dict(self.last_state), 5)
