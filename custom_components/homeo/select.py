"""`select.homeo_<name>` entities: state machines, visible and settable in HA (D13d)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import MACHINE_UNIQUE_ID_PREFIX

if TYPE_CHECKING:
    from .manager import HomeoManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HomeoManager = entry.runtime_data
    manager.add_select_entities = async_add_entities


class _MachineData(ExtraStoredData):
    def __init__(self, changed_at: float):
        self.changed_at = changed_at

    def as_dict(self) -> dict[str, Any]:
        return {"changed_at": datetime.fromtimestamp(self.changed_at, UTC).isoformat()}


class MachineSelect(SelectEntity, RestoreEntity):
    """The machine's value lives in the runtime; this entity mirrors and persists it.

    HA restores the entity's last state on restart; the runtime reads it back through
    HassHost.restore_machine. The machine's own change time is kept in extra restore data,
    because the entity's `last_changed` is reset when it re-writes its state at boot.
    """

    _attr_should_poll = False
    _attr_icon = "mdi:state-machine"

    def __init__(
        self, manager: HomeoManager, name: str, value: str, changed_at: float, options: list[str]
    ):
        self.manager = manager
        self.machine = name
        self._attr_unique_id = MACHINE_UNIQUE_ID_PREFIX + name
        self._attr_name = f"Homeo {name.replace('_', ' ')}"
        self.entity_id = f"select.homeo_{name}"
        self._attr_options = list(options)
        self._attr_current_option = value
        self._changed_at = changed_at

    def update_machine(self, value: str, changed_at: float, options: list[str]) -> None:
        self._attr_current_option = value
        self._attr_options = list(options)
        self._changed_at = changed_at
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"changed_at": datetime.fromtimestamp(self._changed_at, UTC).isoformat()}

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return _MachineData(self._changed_at)

    async def async_select_option(self, option: str) -> None:
        self.manager.select_option(self.machine, option)
