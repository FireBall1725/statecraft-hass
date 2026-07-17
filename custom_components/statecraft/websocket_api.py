"""WebSocket API feeding the Statecraft sidebar panel.

The panel is the primary editor for composite states. It reads the configured
subjects + their live state, and writes the states list back. Builder rows are
compiled to native HA conditions here (reusing condition_builder) so the JS
never duplicates that logic, and every condition is validated before it is
saved.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
import yaml
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import condition

from .condition_builder import compile_condition, decompile_condition
from .const import (
    CONF_AWAY_FROM,
    CONF_AWAY_STATE,
    CONF_CONDITION,
    CONF_DEFAULT_STATE,
    CONF_HOLD,
    CONF_HOLD_BUILDER,
    CONF_ICON,
    CONF_NAME,
    CONF_SCOPE_NAME,
    CONF_SCOPE_TYPE,
    CONF_STATES,
    CONF_SUBJECT,
    DEFAULT_AWAY_FROM,
    DEFAULT_AWAY_STATE,
    DEFAULT_STATE,
    DOMAIN,
    PERSON_DOMAIN,
    SCOPE_PERSON,
)

CONF_BUILDER = "builder"
B_COMBINE = "combine"
B_SOURCES = "sources"
F_MODE = "mode"
MODE_BUILDER = "builder"
MODE_YAML = "yaml"


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the panel's websocket commands (idempotent at call site)."""
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_people)
    websocket_api.async_register_command(hass, ws_save)
    websocket_api.async_register_command(hass, ws_to_yaml)
    websocket_api.async_register_command(hass, ws_from_yaml)


def _live(hass: HomeAssistant, subject_entity_id: str) -> dict[str, Any]:
    state = hass.states.get(subject_entity_id)
    if state is None:
        return {"state": None, "attributes": {}}
    return {"state": state.state, "attributes": dict(state.attributes)}


@callback
@websocket_api.websocket_command({vol.Required("type"): "statecraft/list"})
def ws_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all configured subjects with their states + live status."""
    subjects = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        merged = {**entry.data, **entry.options}
        subject_id = merged.get(CONF_SUBJECT)
        subjects.append(
            {
                "entry_id": entry.entry_id,
                "loaded": entry.state is ConfigEntryState.LOADED,
                "subject": subject_id,
                "scope_type": merged.get(CONF_SCOPE_TYPE, SCOPE_PERSON),
                "name": merged.get(CONF_SCOPE_NAME),
                "icon": merged.get(CONF_ICON),
                "away_from": merged.get(CONF_AWAY_FROM, DEFAULT_AWAY_FROM),
                "away_state": merged.get(CONF_AWAY_STATE, DEFAULT_AWAY_STATE),
                "default_state": merged.get(CONF_DEFAULT_STATE, DEFAULT_STATE),
                "states": merged.get(CONF_STATES, []),
                "live": _live(hass, subject_id) if subject_id else {},
            }
        )
    subjects.sort(key=lambda s: s["subject"] or "")
    connection.send_result(msg["id"], {"subjects": subjects})


@callback
@websocket_api.websocket_command({vol.Required("type"): "statecraft/people"})
def ws_people(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all person entities (for reference / source pickers)."""
    people = [
        {
            "entity_id": s.entity_id,
            "name": s.attributes.get("friendly_name", s.entity_id),
        }
        for s in hass.states.async_all(PERSON_DOMAIN)
    ]
    people.sort(key=lambda p: p["name"])
    connection.send_result(msg["id"], {"people": people})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "statecraft/to_yaml",
        vol.Required("combine"): str,
        vol.Required("sources"): [dict],
    }
)
@websocket_api.async_response
async def ws_to_yaml(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Compile a builder tree to native HA condition YAML (validated)."""
    cfg = compile_condition(msg["combine"], msg["sources"])
    if not cfg:
        connection.send_result(msg["id"], {"yaml": ""})
        return
    try:
        await condition.async_validate_condition_config(hass, cfg)
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "invalid", str(err))
        return
    text = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False).strip()
    connection.send_result(msg["id"], {"yaml": text})


@websocket_api.websocket_command(
    {vol.Required("type"): "statecraft/from_yaml", vol.Required("yaml"): str}
)
@websocket_api.async_response
async def ws_from_yaml(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Parse HA condition YAML into a builder tree, if representable.

    Returns representable=False (not an error) for a valid condition the builder
    can't draw as rows (template, sun, zone, ...), so the panel keeps YAML mode.
    """
    try:
        cfg = yaml.safe_load(msg["yaml"] or "")
    except yaml.YAMLError as err:
        connection.send_error(msg["id"], "invalid_yaml", str(err))
        return
    if not cfg:
        connection.send_result(
            msg["id"],
            {"builder": {"combine": "or", "sources": []}, "representable": True},
        )
        return
    # Decompile from the plain parsed form (validation may coerce for:/entity_id
    # into objects that don't map back cleanly).
    builder = decompile_condition(cfg)
    try:
        await condition.async_validate_condition_config(hass, cfg)
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "invalid_condition", str(err))
        return
    if builder is None:
        connection.send_result(msg["id"], {"representable": False})
        return
    connection.send_result(msg["id"], {"builder": builder, "representable": True})


def _compile_block(payload: dict[str, Any], label: str) -> tuple[Any, Any]:
    """Compile one condition block (builder rows or YAML) from a panel payload.

    Returns (condition_cfg, builder_rows). builder_rows is the raw builder
    object to stash for UI round-trip in builder mode, else None. condition_cfg
    may be None when the block is empty (no sources / blank YAML).
    """
    mode = payload.get(F_MODE, MODE_BUILDER)
    if mode == MODE_YAML:
        try:
            return yaml.safe_load(payload.get("yaml") or ""), None
        except yaml.YAMLError as err:
            raise ValueError(f"{label}: invalid YAML ({err})") from err
    builder = payload.get(CONF_BUILDER) or {}
    cfg = compile_condition(builder.get(B_COMBINE, "or"), builder.get(B_SOURCES, []))
    return cfg, payload.get(CONF_BUILDER)


def _build_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a panel state payload into the stored state dict (uncompiled check).

    Raises ValueError with a human message on structural problems. Condition
    validation against HA happens in the async handler.
    """
    name = (raw.get(CONF_NAME) or "").strip()
    if not name:
        raise ValueError("a state needs a name")

    condition_cfg, enter_builder = _compile_block(raw, name)
    if not condition_cfg:
        raise ValueError(f"{name}: needs at least one source / a condition")

    state: dict[str, Any] = {CONF_NAME: name, CONF_CONDITION: condition_cfg}
    if enter_builder is not None:
        state[CONF_BUILDER] = enter_builder

    # Optional per-state icon. Blank means "no icon", so drop the key entirely
    # rather than storing "" and shadowing the domain default.
    icon = (raw.get(CONF_ICON) or "").strip()
    if icon:
        state[CONF_ICON] = icon

    # Optional hold (latch) condition, authored the same way as the enter one.
    hold_raw = raw.get(CONF_HOLD)
    if hold_raw:
        hold_cfg, hold_builder = _compile_block(hold_raw, f"{name} hold")
        if hold_cfg:  # an enabled-but-empty hold just means "no latch"
            state[CONF_HOLD] = hold_cfg
            if hold_builder is not None:
                state[CONF_HOLD_BUILDER] = hold_builder
    return state


@websocket_api.websocket_command(
    {
        vol.Required("type"): "statecraft/save",
        vol.Required("entry_id"): str,
        # Fallback fields differ by scope type; both optional so either kind of
        # panel payload validates, and we pick the right one from the entry.
        vol.Optional(CONF_AWAY_FROM): str,
        vol.Optional(CONF_AWAY_STATE): str,
        vol.Optional(CONF_DEFAULT_STATE): str,
        vol.Required(CONF_STATES): [dict],
    }
)
@websocket_api.async_response
async def ws_save(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and persist a subject's states; the entry reloads on update."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "unknown config entry")
        return

    states_out: list[dict[str, Any]] = []
    for raw in msg[CONF_STATES]:
        try:
            state = _build_state(raw)
            state[CONF_CONDITION] = await condition.async_validate_condition_config(
                hass, state[CONF_CONDITION]
            )
            if CONF_HOLD in state:
                state[CONF_HOLD] = await condition.async_validate_condition_config(
                    hass, state[CONF_HOLD]
                )
        except (ValueError, vol.Invalid) as err:
            connection.send_error(msg["id"], "invalid_state", str(err))
            return
        except Exception as err:  # noqa: BLE001 - any condition error -> message
            name = raw.get(CONF_NAME, "?")
            connection.send_error(
                msg["id"], "invalid_state", f"{name}: invalid condition ({err})"
            )
            return
        states_out.append(state)

    options: dict[str, Any] = {CONF_STATES: states_out}
    if entry.data.get(CONF_SCOPE_TYPE, SCOPE_PERSON) == SCOPE_PERSON:
        options[CONF_AWAY_FROM] = msg.get(CONF_AWAY_FROM, DEFAULT_AWAY_FROM)
        options[CONF_AWAY_STATE] = msg.get(CONF_AWAY_STATE, DEFAULT_AWAY_STATE)
    else:
        options[CONF_DEFAULT_STATE] = msg.get(CONF_DEFAULT_STATE, DEFAULT_STATE)
    # async_update_entry fires the entry's update listener, which reloads it.
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], {"ok": True, "states": states_out})
