"""Writers: per-target rules for writing a value with service calls and reading it back (§7, I18).

A writer knows:
1. how to write a value (which service calls);
2. how to read it back (`matches`), with tolerances for device rounding;
3. which result types it accepts (checked at compile time).

`view` extracts the parts of an entity's state the writer cares about, so that changes to
unrelated attributes (a thermostat's current temperature) aren't mistaken for manual changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .host import EntityState, ServiceCall
from .types import T
from .values import Num, OnSpec


def _num(v: Any) -> float | None:
    if isinstance(v, Num):
        return v.lo
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OnParam:
    name: str  # name in homeo: brightness
    type: T  # PERCENT or NUMBER
    service_key: str  # brightness_pct
    read_attr: str  # brightness
    to_service: Any  # value -> service data value
    to_read: Any  # value -> expected attribute value
    tolerance: Any  # state -> tolerance


class Writer:
    domain: str = ""
    attr: str | None = None  # target attribute name in homeo (e.g. "position"), None = whole
    accepts: frozenset[T] = frozenset()
    description: str = ""
    literal_values: tuple[str, ...] | None = None  # allowed string results, if restricted
    on_params: dict[str, OnParam] = {}

    def calls(self, entity_id: str, value: Any) -> list[ServiceCall]:
        raise NotImplementedError

    def matches(self, state: EntityState | None, value: Any) -> bool:
        raise NotImplementedError

    def view(self, state: EntityState | None) -> tuple:
        if state is None:
            return (None,)
        return (state.state,)

    def describe_value(self, value: Any) -> str:
        if value is True:
            return "on"
        if value is False:
            return "off"
        if isinstance(value, Num):
            return f"{value.lo:g}"
        return repr(value)


# --------------------------------------------------------------------- on / off


def _clean(x: float) -> float | int:
    """Send integral numbers as ints (brightness_pct=100, not 100.0)."""
    return int(x) if float(x).is_integer() else x


def _pct_to_255(v: Any) -> float:
    return round(255 * _num(v) / 100)


class OnOffWriter(Writer):
    def __init__(self, domain: str, on_params: dict[str, OnParam] | None = None):
        self.domain = domain
        self.on_params = on_params or {}
        self.accepts = frozenset({T.BOOL, T.ONSPEC} if self.on_params else {T.BOOL})
        self.description = "`on`/`off`" + (
            " or `on(" + ", ".join(f"{p}: …" for p in self.on_params) + ")`"
            if self.on_params
            else ""
        )

    def calls(self, entity_id: str, value: Any) -> list[ServiceCall]:
        if value is False:
            return [ServiceCall(self.domain, "turn_off", {}, entity_id)]
        data: dict[str, Any] = {}
        if isinstance(value, OnSpec):
            for name, v in value.params:
                p = self.on_params[name]
                data[p.service_key] = p.to_service(v)
        return [ServiceCall(self.domain, "turn_on", data, entity_id)]

    def matches(self, state: EntityState | None, value: Any) -> bool:
        if state is None:
            return False
        want_on = value is not False
        if state.state != ("on" if want_on else "off"):
            return False
        if isinstance(value, OnSpec):
            for name, v in value.params:
                p = self.on_params[name]
                actual = _num(state.attributes.get(p.read_attr))
                if actual is None or abs(actual - p.to_read(v)) > p.tolerance(state):
                    return False
        return True

    def view(self, state: EntityState | None) -> tuple:
        if state is None:
            return (None,)
        return (state.state,) + tuple(
            state.attributes.get(p.read_attr) for p in self.on_params.values()
        )


LIGHT_PARAMS = {
    "brightness": OnParam(
        "brightness",
        T.PERCENT,
        "brightness_pct",
        "brightness",
        lambda v: _clean(round(_num(v), 2)),
        _pct_to_255,
        lambda s: 3,
    ),
    "kelvin": OnParam(
        "kelvin",
        T.NUMBER,
        "color_temp_kelvin",
        "color_temp_kelvin",
        lambda v: int(round(_num(v))),
        lambda v: _num(v),
        lambda s: 0.02 * (_num(s.attributes.get("color_temp_kelvin")) or 3000),
    ),
}

FAN_PARAMS = {
    "percentage": OnParam(
        "percentage",
        T.PERCENT,
        "percentage",
        "percentage",
        lambda v: int(round(_num(v))),
        lambda v: _num(v),
        lambda s: max(1.0, (_num(s.attributes.get("percentage_step")) or 1.0) / 2),
    ),
}


# ------------------------------------------------------------ string-valued states


class StateServiceWriter(Writer):
    """String targets where each value has its own service (cover open/close, lock)."""

    def __init__(self, domain: str, services: dict[str, str], transitional: dict[str, str]):
        self.domain = domain
        self.services = services
        self.transitional = transitional  # e.g. "opening" counts as "open"
        self.accepts = frozenset({T.STRING})
        self.literal_values = tuple(services)
        self.description = " or ".join(f'"{v}"' for v in services)

    def calls(self, entity_id: str, value: Any) -> list[ServiceCall]:
        service = self.services.get(value)
        if service is None:
            return []
        return [ServiceCall(self.domain, service, {}, entity_id)]

    def matches(self, state: EntityState | None, value: Any) -> bool:
        if state is None:
            return False
        return state.state == value or self.transitional.get(state.state) == value


class ServiceValueWriter(Writer):
    """Targets set by one service with the value as data (set_value, select_option, ...)."""

    def __init__(
        self,
        domain: str,
        service: str,
        key: str,
        accepts: set[T],
        description: str,
        *,
        attr: str | None = None,
        read_attr: str | None = None,
        convert=None,
        expected=None,
        tolerance=None,
    ):
        self.domain = domain
        self.attr = attr
        self.service = service
        self.key = key
        self.accepts = frozenset(accepts)
        self.description = description
        self.read_attr = read_attr
        self.convert = convert or (lambda v: v)
        self.expected = expected or self.convert
        self.tolerance = tolerance

    def calls(self, entity_id: str, value: Any) -> list[ServiceCall]:
        return [ServiceCall(self.domain, self.service, {self.key: self.convert(value)}, entity_id)]

    def _actual(self, state: EntityState) -> Any:
        return state.attributes.get(self.read_attr) if self.read_attr else state.state

    def matches(self, state: EntityState | None, value: Any) -> bool:
        if state is None:
            return False
        actual = self._actual(state)
        want = self.expected(value)
        if self.tolerance is not None:
            a = _num(actual)
            return a is not None and abs(a - want) <= self.tolerance(state)
        return actual == want

    def view(self, state: EntityState | None) -> tuple:
        if state is None:
            return (None,)
        return (self._actual(state),)


def _str(v: Any) -> str:
    return v if isinstance(v, str) else str(v)


def _float(v: Any) -> float | int:
    return _clean(float(_num(v)))


def _step_tol(attr: str, default: float):
    def tol(state: EntityState) -> float:
        step = _num(state.attributes.get(attr))
        return (step / 2) if step else default

    return tol


def build_writers() -> dict[tuple[str, str | None], Writer]:
    w: dict[tuple[str, str | None], Writer] = {}
    for d in ("switch", "input_boolean", "siren"):
        w[(d, None)] = OnOffWriter(d)
    w[("light", None)] = OnOffWriter("light", LIGHT_PARAMS)
    w[("fan", None)] = OnOffWriter("fan", FAN_PARAMS)
    w[("climate", None)] = ServiceValueWriter(
        "climate",
        "set_hvac_mode",
        "hvac_mode",
        {T.STRING},
        'an HVAC mode such as "heat"',
        convert=_str,
    )
    w[("climate", "temperature")] = ServiceValueWriter(
        "climate",
        "set_temperature",
        "temperature",
        {T.NUMBER},
        "a temperature",
        attr="temperature",
        read_attr="temperature",
        convert=_float,
        tolerance=_step_tol("target_temp_step", 0.25),
    )
    w[("cover", None)] = StateServiceWriter(
        "cover",
        {"open": "open_cover", "closed": "close_cover"},
        {"opening": "open", "closing": "closed"},
    )
    w[("cover", "position")] = ServiceValueWriter(
        "cover",
        "set_cover_position",
        "position",
        {T.NUMBER, T.PERCENT},
        "a position from 0 to 100%",
        attr="position",
        read_attr="current_position",
        convert=lambda v: int(round(_num(v))),
        tolerance=lambda s: 1,
    )
    for d in ("input_number", "number"):
        w[(d, None)] = ServiceValueWriter(
            d,
            "set_value",
            "value",
            {T.NUMBER},
            "a number",
            convert=_float,
            tolerance=_step_tol("step", 1e-6),
        )
    for d in ("input_select", "select"):
        w[(d, None)] = ServiceValueWriter(
            d, "select_option", "option", {T.STRING}, "an option string", convert=_str
        )
    for d in ("input_text", "text"):
        w[(d, None)] = ServiceValueWriter(
            d, "set_value", "value", {T.STRING}, "a string", convert=_str
        )
    w[("lock", None)] = StateServiceWriter(
        "lock",
        {"locked": "lock", "unlocked": "unlock"},
        {"locking": "locked", "unlocking": "unlocked"},
    )
    w[("media_player", "volume")] = ServiceValueWriter(
        "media_player",
        "volume_set",
        "volume_level",
        {T.PERCENT},
        "a percentage such as 30%",
        attr="volume",
        read_attr="volume_level",
        convert=lambda v: round(_num(v) / 100, 4),
        tolerance=lambda s: 0.01,
    )
    return w


WRITERS = build_writers()

# Domains bound as a whole: their attributes can't be bound separately (§7).
WHOLE_ONLY = {"light", "fan"}
