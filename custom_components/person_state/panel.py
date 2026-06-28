"""Register the Person State sidebar panel, its static JS, and websocket API."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .websocket_api import async_register_websocket_api

PANEL_URL_PATH = "person-state"
JS_URL = "/person_state_frontend/person-state-panel.js"
_JS_FILE = Path(__file__).parent / "frontend" / "person-state-panel.js"


def _flags(hass: HomeAssistant) -> dict[str, bool]:
    """Panel registration flags, stored on the PersonStateData singleton."""
    data = hass.data.get(DOMAIN)
    if data is None:
        return {}
    return data.panel_flags


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the panel, static JS, and websocket commands (idempotent)."""
    flags = _flags(hass)
    components = hass.config.components

    if "websocket_api" in components and not flags.get("ws"):
        async_register_websocket_api(hass)
        flags["ws"] = True

    if not {"http", "frontend", "panel_custom"} <= components:
        return

    if not flags.get("static"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(JS_URL, str(_JS_FILE), cache_headers=False)]
        )
        flags["static"] = True

    if not flags.get("panel"):
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name="person-state-panel",
            module_url=JS_URL,
            sidebar_title="Person State",
            sidebar_icon="mdi:account-cog",
            require_admin=True,
            config={},
            embed_iframe=False,
        )
        flags["panel"] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel when the last entry unloads."""
    flags = _flags(hass)
    if flags.get("panel"):
        async_remove_panel(hass, PANEL_URL_PATH)
        flags["panel"] = False