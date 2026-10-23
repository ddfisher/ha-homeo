"""The Homeo sidebar panel: an editor for rule files (§19). Admin only.

The panel is one JavaScript module, built from `frontend/` at the repository root (see its
README) into `custom_components/homeo/frontend/`, because HACS only ships this directory.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__package__)

PANEL_PATH = DOMAIN  # /homeo in the browser
STATIC_URL = "/homeo_static"
MODULE = "homeo-panel.js"
_FRONTEND_DIR = Path(__file__).parent / "frontend"
_STATIC_REGISTERED = f"{DOMAIN}_static_registered"


def _module_hash() -> str | None:
    """Runs in the executor: a cache-busting version for the module's URL."""
    try:
        return hashlib.sha256((_FRONTEND_DIR / MODULE).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return None


async def async_register_panel(hass: HomeAssistant) -> None:
    if "frontend" not in hass.config.components:
        _LOGGER.debug("the frontend isn't loaded; not adding the Homeo panel")
        return
    from homeassistant.components import panel_custom
    from homeassistant.components.http import StaticPathConfig

    version = await hass.async_add_executor_job(_module_hash)
    if version is None:
        _LOGGER.warning("the editor panel's code is missing (%s)", _FRONTEND_DIR / MODULE)
        return
    if not hass.data.get(_STATIC_REGISTERED):  # static paths can't be unregistered
        hass.data[_STATIC_REGISTERED] = True
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(_FRONTEND_DIR), cache_headers=True)]
        )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="homeo-panel",
        sidebar_title="Homeo",
        sidebar_icon="mdi:home-thermometer-outline",
        module_url=f"{STATIC_URL}/{MODULE}?v={version}",
        require_admin=True,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
