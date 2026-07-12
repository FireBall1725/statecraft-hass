"""HA-aware state engine: turns a SubjectConfig into a composite state.

Builds one condition checker per user-defined state via HA's native condition
helper, then on each evaluation runs the cascade and layers the optional grace
/ persist modifiers on top.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import condition

from .core import pick_state
from .models import SubjectConfig, collect_for_horizons, collect_for_targets

_LOGGER = logging.getLogger(__name__)

# Circuit breaker: if the cascade is applied more than this many times within
# the window, assume a feedback loop and stop. This makes it impossible for any
# loop (known or not) to hang the event loop / take HA down. After a cooldown
# it auto-recovers and retries, so a transient burst (a sensor flapping during a
# reboot) doesn't permanently disable a scope — only a persistent loop keeps it
# tripped (it re-trips each time it retries).
_BREAKER_MAX = 25
_BREAKER_WINDOW = 2.0  # seconds
_BREAKER_COOLDOWN = 30.0  # seconds before a tripped breaker retries

# After a restart, HA resets many sensors' last_changed to boot time, so a
# `for:` in an enter condition is false for its whole duration even if the
# sensor held that value across the reboot. To bridge that gap we restore the
# last composite state and, for a short window, evaluate its enter condition
# with `for:` stripped (see begin_bridge). The buffer keeps the window open a
# little past the `for:` horizon so HA's own timing has caught up before we
# hand back to normal evaluation.
_BRIDGE_BUFFER = 30.0  # seconds
_UNAVAILABLE = ("unavailable", "unknown")


def _strip_for(cfg: Any) -> Any:
    """Deep copy of a condition config with every `for:` removed."""
    if isinstance(cfg, dict):
        return {k: _strip_for(v) for k, v in cfg.items() if k != "for"}
    if isinstance(cfg, list):
        return [_strip_for(v) for v in cfg]
    return cfg


def _run_checker(checker: Any, hass: HomeAssistant) -> bool:
    """Evaluate a condition checker, tolerant of None / version differences."""
    if checker is None:
        return False
    try:
        check = getattr(checker, "async_check", None)
        result = check(variables=None) if check is not None else checker(hass, None)
    except Exception as err:  # noqa: BLE001 - a bad condition must not crash person
        _LOGGER.debug("condition check failed: %s", err)
        return False
    return bool(result)


class StateEngine:
    """Evaluates one subject's states."""

    def __init__(self, hass: HomeAssistant, subject: SubjectConfig) -> None:
        self.hass = hass
        self.subject = subject
        self._checkers: dict[str, Any] = {}
        # for-less copy of each enter checker, used only to bridge a reboot
        self._instant_checkers: dict[str, Any] = {}
        # one optional "stay latched while true" checker per state
        self._hold_checkers: dict[str, Any] = {}
        # entities each state's enter condition references (for the settle hold)
        self._state_entities: dict[str, set[str]] = {}
        # longest `for:` in each state's enter condition (bridge window length)
        self._enter_horizon: dict[str, float] = {}
        # entities to subscribe to so we re-evaluate on change
        self.entities: set[str] = set()
        # `for:` / grace durations to schedule precise re-evaluations
        self.for_horizons: list[float] = []
        # (entity_id, for_seconds) for every `for:` condition, enter + hold
        self.for_targets: list[tuple[str, float]] = []
        # reboot bridge: which restored state, and until when (monotonic)
        self._bridge_state: str | None = None
        self._bridge_until: float = 0.0
        # person path: whether the first cascade has restored the saved state yet
        self.primed: bool = False
        # circuit breaker state (see allow_apply)
        self._apply_times: deque[float] = deque()
        self.tripped: bool = False
        self._tripped_at: float = 0.0

    def allow_apply(self) -> bool:
        """Return False if the cascade is running away (feedback loop).

        Trips after too many applies in the window; auto-recovers after a
        cooldown so a transient burst doesn't disable the scope forever. A
        persistent loop simply re-trips on the next retry, so HA stays safe.
        """
        now = time.monotonic()
        if self.tripped:
            if now - self._tripped_at < _BREAKER_COOLDOWN:
                return False
            # cooldown elapsed: reset and retry
            self.tripped = False
            self._apply_times.clear()
            self._breaker_logged = False
        self._apply_times.append(now)
        while self._apply_times and now - self._apply_times[0] > _BREAKER_WINDOW:
            self._apply_times.popleft()
        if len(self._apply_times) > _BREAKER_MAX:
            self.tripped = True
            self._tripped_at = now
            return False
        return True

    async def async_build(self) -> None:
        """Compile condition checkers and collect what to watch."""
        self._checkers.clear()
        self._instant_checkers.clear()
        self._hold_checkers.clear()
        self._state_entities.clear()
        self._enter_horizon.clear()
        self.entities.clear()
        self.for_horizons.clear()
        self.for_targets.clear()

        for state_def in self.subject.states:
            self._checkers[state_def.name] = await self._compile(
                state_def.name, "condition", state_def.condition
            )
            # for-less twin of the enter checker, for reboot bridging
            self._instant_checkers[state_def.name] = await self._compile_quiet(
                _strip_for(state_def.condition)
            )
            # Entities of both enter AND hold: a dropout of any of them should
            # not drop an active state (see _settling).
            entities = self._extract(state_def.condition)
            horizons = collect_for_horizons(state_def.condition)
            self._enter_horizon[state_def.name] = max(horizons) if horizons else 0.0
            self.for_targets.extend(collect_for_targets(state_def.condition))
            if state_def.hold is not None:
                self._hold_checkers[state_def.name] = await self._compile(
                    state_def.name, "hold", state_def.hold
                )
                self.for_targets.extend(collect_for_targets(state_def.hold))
                entities |= self._extract(state_def.hold)
            self._state_entities[state_def.name] = entities

    async def _compile_quiet(self, cfg: dict[str, Any]) -> Any:
        """Build a checker without folding entities/horizons (used for twins)."""
        try:
            return await condition.async_from_config(self.hass, cfg)
        except Exception:  # noqa: BLE001
            return None

    def _extract(self, cfg: dict[str, Any]) -> set[str]:
        try:
            return set(condition.async_extract_entities(cfg))
        except Exception:  # noqa: BLE001
            return set()

    async def _compile(self, name: str, kind: str, cfg: dict[str, Any]) -> Any:
        """Build one condition checker; fold its entities + `for:` horizons in.

        A bad condition returns a None checker (never true) rather than crashing
        setup — the error is logged so the offending state is obvious.
        """
        checker: Any = None
        try:
            checker = await condition.async_from_config(self.hass, cfg)
        except Exception as err:  # noqa: BLE001 - surface, do not crash setup
            _LOGGER.error(
                "state %r has an invalid %s condition, it will never be true: %s",
                name,
                kind,
                err,
            )

        try:
            self.entities |= condition.async_extract_entities(cfg)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("could not extract entities for %r %s: %s", name, kind, err)

        self.for_horizons.extend(collect_for_horizons(cfg))
        return checker

    def pending_for_delays(self) -> list[float]:
        """Seconds until each in-progress `for:` boundary elapses.

        Computed from the entity's real last_changed, so an unrelated
        state-changed event (a chatty sensor's attribute update) reschedules to
        the *same* absolute moment instead of pushing the boundary forward.
        """
        now = dt_util.utcnow()
        delays: list[float] = []
        for entity_id, secs in self.for_targets:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            remaining = secs - (now - state.last_changed).total_seconds()
            if remaining > 0:
                delays.append(remaining + 0.5)  # a hair past the boundary
        return delays

    def begin_bridge(self, restored_state: str | None) -> None:
        """Start the reboot bridge for a restored state.

        For the length of that state's `for:` horizon (plus a buffer), its enter
        condition is evaluated with `for:` stripped, so a state that held across
        a reboot re-enters at once instead of waiting out the timer again. A
        state with no `for:` needs no bridge (it re-enters immediately anyway).
        """
        if not restored_state:
            return
        horizon = self._enter_horizon.get(restored_state, 0.0)
        if horizon <= 0:
            return
        self._bridge_state = restored_state
        self._bridge_until = time.monotonic() + horizon + _BRIDGE_BUFFER

    def _bridging(self, name: str) -> bool:
        return name == self._bridge_state and time.monotonic() < self._bridge_until

    def _settling(self, name: str) -> bool:
        """True while any entity the state references is still unavailable."""
        for entity_id in self._state_entities.get(name, ()):
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _UNAVAILABLE:
                return True
        return False

    @callback
    def evaluate(
        self, presence: str | None, previous_state: str | None
    ) -> tuple[str, dict[str, bool]]:
        """Return (composite_state, {state_name: active})."""
        ordered = [
            (sd.name, self._state_on(sd, previous_state)) for sd in self.subject.states
        ]
        state = pick_state(
            ordered,
            presence,
            self.subject.away_from,
            self.subject.away_state,
            self.subject.default_state,
        )
        return state, dict(ordered)

    @callback
    def _state_on(self, state_def, previous_state: str | None) -> bool:
        name = state_def.name
        bridging = self._bridging(name)
        # Enter: the condition is true right now. During the reboot bridge, use
        # the for-less twin so a state that held across the reboot re-enters
        # without waiting out its `for:` again.
        checker = (
            self._instant_checkers.get(name) if bridging else self._checkers.get(name)
        )
        if _run_checker(checker, self.hass):
            return True
        # Everything below only keeps a state we were already in.
        if previous_state != name:
            return False
        # A referenced sensor is unavailable/unknown: a dropout is not a change,
        # so keep the state until the sensor reports again rather than dropping
        # it on missing data. Handles flaky sensors and the reboot boot window.
        if self._settling(name):
            return True
        # Latch: the hold condition keeps it active even though the enter
        # condition has gone false.
        return state_def.hold is not None and _run_checker(
            self._hold_checkers.get(name), self.hass
        )
