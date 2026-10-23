"""What the core needs from its environment. HassHost (the integration) and FakeHost (tests)
implement this. Nothing here imports Home Assistant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any, Protocol

Unsub = Callable[[], None]

UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
# States of a device still on its way to a target (covers, valves, locks).
TRANSITIONAL_STATES = frozenset({"opening", "closing", "locking", "unlocking"})


@dataclass(frozen=True, slots=True)
class EntityState:
    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)
    last_changed: float = 0.0  # UTC epoch seconds
    last_updated: float = 0.0

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def available(self) -> bool:
        return self.state not in UNAVAILABLE_STATES


@dataclass(frozen=True, slots=True)
class StateChange:
    entity_id: str
    old: EntityState | None
    new: EntityState | None
    time: float
    ours: bool = False  # caused by a service call homeo made (context id is ours)


@dataclass(frozen=True, slots=True)
class ServiceCall:
    domain: str
    service: str
    data: dict[str, Any]
    entity_id: str | None = None
    source: str = ""  # "file.homeo:12", for logbook attribution
    reason: str = ""  # human-readable, e.g. the binding target


class EntityCatalog(Protocol):
    """Read-only view of HA used by the checker."""

    def get_state(self, entity_id: str) -> EntityState | None: ...

    def entity_exists(self, entity_id: str) -> bool: ...

    def entity_ids(self, domain: str | None = None) -> list[str]: ...

    def domains(self) -> set[str]: ...

    def has_service(self, domain: str, service: str) -> bool | None:
        """True/False, or None if unknown (services can register late)."""
        ...


class Host(EntityCatalog, Protocol):
    boot_time: float
    tz: tzinfo

    def now(self) -> float: ...

    def subscribe_states(
        self, entity_ids: list[str], callback: Callable[[StateChange], None]
    ) -> Unsub: ...

    def subscribe_bus(
        self, event_type: str, callback: Callable[[dict[str, Any], float], None]
    ) -> Unsub: ...

    def schedule(self, when: float, callback: Callable[[float], None]) -> Unsub: ...

    def call_service(self, call: ServiceCall) -> None: ...

    def publish_machine(self, name: str, value: str, changed_at: float, options: list[str]) -> None:
        """Update (or create) the machine's `select` entity."""
        ...

    def restore_machine(self, name: str) -> tuple[str, float] | None:
        """The machine's restored (value, change time), if HA restored one."""
        ...

    def machine_stopped(self, name: str, file: str, loop: str | None) -> None:
        """A state machine stopped after a loop (`loop` shows it, e.g. "a → b → a"), or runs
        again (None): shown to the user as a Repairs issue."""
        ...

    def log(self, level: str, message: str) -> None: ...
