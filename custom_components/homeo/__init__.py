"""Homeo: declarative rules for Home Assistant.

Watches `<config>/homeo/*.homeo`, compiles the rules (see `core/`) and keeps devices in line
with them through service calls.

Home Assistant is imported lazily here, so the host-agnostic core (`custom_components.homeo.core`)
can be imported and tested without Home Assistant installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

    from .manager import HomeoManager

    type HomeoConfigEntry = ConfigEntry[HomeoManager]

PLATFORMS = ["select"]


async def async_setup_entry(hass: HomeAssistant, entry: HomeoConfigEntry) -> bool:
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.helpers import config_validation as cv

    from .manager import HomeoManager

    manager = HomeoManager(hass, entry)
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()

    async def reload_rules(call: ServiceCall) -> None:
        await manager.async_reload()

    hass.services.async_register(DOMAIN, "reload", reload_rules)
    hass.services.async_register(
        DOMAIN,
        "explain",
        manager.explain,
        schema=vol.Schema({vol.Required("target"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: HomeoConfigEntry) -> None:
    await entry.runtime_data.async_reload()


async def async_unload_entry(hass: HomeAssistant, entry: HomeoConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    hass.services.async_remove(DOMAIN, "reload")
    hass.services.async_remove(DOMAIN, "explain")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
