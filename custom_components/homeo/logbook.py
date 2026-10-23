"""Logbook: attribute homeo's service calls to the rule that made them (§19)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_CONTEXT_ID,
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
    LOGBOOK_ENTRY_SOURCE,
    LazyEventPartialState,
)
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, EVENT_RULE_FIRED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[LazyEventPartialState], dict[str, Any]]], None
    ],
) -> None:
    @callback
    def describe(event: LazyEventPartialState) -> dict[str, Any]:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: "Homeo",
            LOGBOOK_ENTRY_MESSAGE: f"rule {data.get('source')} ({data.get('name')}) fired",
            LOGBOOK_ENTRY_SOURCE: data.get("source"),
            LOGBOOK_ENTRY_ENTITY_ID: data.get("entity_id"),
            LOGBOOK_ENTRY_CONTEXT_ID: event.context_id,
        }

    async_describe_event(DOMAIN, EVENT_RULE_FIRED, describe)
