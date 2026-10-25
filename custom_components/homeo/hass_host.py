"""HassHost: the Home Assistant implementation of the core's Host interface."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import async_get as async_get_restore_data
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_RULE_FIRED, MACHINE_UNIQUE_ID_PREFIX
from .core.host import EntityState, ServiceCall, StateChange, Unsub

if TYPE_CHECKING:
    from .manager import HomeoManager

_LOGGER = logging.getLogger(__package__)
_OUR_CONTEXTS = 2000  # how many issued context ids to remember (D21)


def to_entity_state(st: State | None) -> EntityState | None:
    if st is None:
        return None
    return EntityState(
        st.entity_id,
        st.state,
        dict(st.attributes),
        st.last_changed.timestamp(),
        st.last_updated.timestamp(),
    )


class HassHost:
    def __init__(self, hass: HomeAssistant, manager: HomeoManager):
        self.hass = hass
        self.manager = manager
        self.boot_time = dt_util.utcnow().timestamp()
        self.tz = dt_util.get_default_time_zone()
        self._our_ids: deque[str] = deque(maxlen=_OUR_CONTEXTS)
        self._our_set: set[str] = set()
        self._issued: dict[str, int] = {}  # calls issued so far, per entity

    # ------------------------------------------------------------- catalog

    def get_state(self, entity_id: str) -> EntityState | None:
        return to_entity_state(self.hass.states.get(entity_id))

    def entity_exists(self, entity_id: str) -> bool:
        return (
            self.hass.states.get(entity_id) is not None
            or er.async_get(self.hass).async_get(entity_id) is not None
        )

    def entity_ids(self, domain: str | None = None) -> list[str]:
        ids = set(self.hass.states.async_entity_ids(domain))
        for entry in er.async_get(self.hass).entities.values():
            if domain is None or entry.domain == domain:
                ids.add(entry.entity_id)
        return sorted(ids)

    def domains(self) -> set[str]:
        out = {eid.split(".", 1)[0] for eid in self.hass.states.async_entity_ids()}
        out |= {e.domain for e in er.async_get(self.hass).entities.values()}
        return out

    def has_service(self, domain: str, service: str) -> bool | None:
        return self.hass.services.has_service(domain, service)

    # ---------------------------------------------------------------- host

    def now(self) -> float:
        return dt_util.utcnow().timestamp()

    def subscribe_states(
        self, entity_ids: list[str], callback_: Callable[[StateChange], None]
    ) -> Unsub:
        @callback
        def handle(event: Event[EventStateChangedData]) -> None:
            old = event.data["old_state"]
            new = event.data["new_state"]
            ours = event.context.id in self._our_set or (
                new is not None and new.context.id in self._our_set
            )
            callback_(
                StateChange(
                    event.data["entity_id"],
                    to_entity_state(old),
                    to_entity_state(new),
                    event.time_fired.timestamp(),
                    ours,
                )
            )

        return async_track_state_change_event(self.hass, entity_ids, handle)

    def subscribe_bus(
        self, event_type: str, callback_: Callable[[dict[str, Any], float], None]
    ) -> Unsub:
        @callback
        def handle(event: Event) -> None:
            callback_(dict(event.data), event.time_fired.timestamp())

        return self.hass.bus.async_listen(event_type, handle)

    def schedule(self, when: float, callback_: Callable[[float], None]) -> Unsub:
        @callback
        def fire(_now: datetime) -> None:
            callback_(when)  # the scheduled instant, so change times stay exact

        return async_track_point_in_utc_time(self.hass, fire, datetime.fromtimestamp(when, UTC))

    def call_service(self, call: ServiceCall) -> None:
        target, ctx = self._prepare(call)
        self.hass.async_create_task(
            self._call(call, target, ctx), f"homeo {call.domain}.{call.service}"
        )

    def call_services(self, calls: list[ServiceCall]) -> None:
        prepared = [(call, *self._prepare(call)) for call in calls]
        entity = calls[0].entity_id if calls else None
        mine = self._issued.get(entity, 0) if entity else 0

        async def run() -> None:
            for i, (call, target, ctx) in enumerate(prepared):
                if i and entity and self._issued.get(entity, 0) != mine:
                    return  # superseded: a newer call to this entity went out meanwhile
                await self._call(call, target, ctx)

        self.hass.async_create_task(run(), f"homeo {len(calls)} calls")

    def _prepare(self, call: ServiceCall) -> tuple[dict | None, Context]:
        """Our context for a call (remembered for manual-change detection, D21), announced
        with the rule that caused it."""
        ctx = Context()
        if call.entity_id:
            self._issued[call.entity_id] = self._issued.get(call.entity_id, 0) + 1
        if len(self._our_ids) == self._our_ids.maxlen:
            self._our_set.discard(self._our_ids[0])
        self._our_ids.append(ctx.id)
        self._our_set.add(ctx.id)
        # The first event with this context is what the logbook shows as the cause (§19).
        self.hass.bus.async_fire(
            EVENT_RULE_FIRED,
            {"name": call.reason, "source": call.source, "entity_id": call.entity_id},
            context=ctx,
        )
        target = {"entity_id": call.entity_id} if call.entity_id else None
        return target, ctx

    async def _call(self, call: ServiceCall, target: dict | None, ctx: Context) -> None:
        try:
            await self.hass.services.async_call(
                call.domain, call.service, call.data, blocking=True, context=ctx, target=target
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash the runtime
            _LOGGER.error(
                "%s: calling %s.%s%s failed: %s",
                call.source,
                call.domain,
                call.service,
                f" on {call.entity_id}" if call.entity_id else "",
                exc,
            )

    def publish_machine(self, name: str, value: str, changed_at: float, options: list[str]) -> None:
        self.manager.publish_machine(name, value, changed_at, options)

    def restore_machine(self, name: str) -> tuple[str, float] | None:
        entity_id = (
            er.async_get(self.hass).async_get_entity_id(
                "select", DOMAIN, MACHINE_UNIQUE_ID_PREFIX + name
            )
            or f"select.homeo_{name}"
        )
        # A live entity (e.g. across a config-entry reload) wins over stored restore data.
        live = self.hass.states.get(entity_id)
        if live is not None and "changed_at" in live.attributes:
            return self._restored(live.state, live.attributes.get("changed_at"), live)
        stored = async_get_restore_data(self.hass).last_states.get(entity_id)
        if stored is None:
            return None
        extra = stored.extra_data.as_dict() if stored.extra_data else {}
        return self._restored(stored.state.state, extra.get("changed_at"), stored.state)

    def machine_stopped(self, name: str, file: str, loop: str | None) -> None:
        self.manager.machine_stopped(name, file, loop)

    @staticmethod
    def _restored(value: str, changed_at: Any, state: State) -> tuple[str, float] | None:
        ts = None
        if isinstance(changed_at, str):
            parsed = dt_util.parse_datetime(changed_at)
            ts = parsed.timestamp() if parsed else None
        if ts is None:
            ts = state.last_changed.timestamp()
        return value, ts

    def log(self, level: str, message: str) -> None:
        getattr(_LOGGER, {"warning": "warning"}.get(level, level), _LOGGER.info)(message)
