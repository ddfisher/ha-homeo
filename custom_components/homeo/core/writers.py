"""Writers: per-target rules for writing a value with service calls and reading it back (§7, I18).

A writer knows:
1. how to write a value (which service calls);
2. how to read it back (`matches`), with tolerances for device rounding;
3. which result types it accepts (checked at compile time).

`view` extracts the parts of an entity's state the writer cares about, so that changes to
unrelated attributes (a thermostat's current temperature) aren't mistaken for manual changes.

Numeric writers can also follow a behavior (D23): the runtime asks whether the entity supports
transitions (`supports_transition`, `with_transition`) and, if not, how long one step of the
setting's resolution takes (`step_time`). Values given to `matches` may be ranges (a `Num` with
`lo < hi`): "anywhere along the piece being followed".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .host import EntityState, ServiceCall
from .types import T
from .values import Num, OnSpec

# HA's LightEntityFeature.TRANSITION, as reported in a light's `supported_features`.
LIGHT_TRANSITION_FEATURE = 32


def _range(v: Any) -> tuple[float, float] | None:
    """A value as (lo, hi): a range for the pieces of a behavior being followed (D23)."""
    if isinstance(v, Num):
        return (v.lo, v.hi)
    x = _num(v)
    return None if x is None else (x, x)


def _within(actual: float | None, want: Any, to_read, tol: float) -> bool:
    """`actual` matches `want` (a value or a range), converted for read-back by `to_read`."""
    r = _range(want)
    if actual is None or r is None or not all(math.isfinite(x) for x in r):
        return False
    a, b = to_read(r[0]), to_read(r[1])
    return min(a, b) - tol <= actual <= max(a, b) + tol


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
    resolution: Any  # (state, value) -> smallest change worth a step, in homeo's units (D23)
    bounds: Any  # state -> (lo, hi) the setting is clamped to, or None (D23)


class Writer:
    domain: str = ""
    attr: str | None = None  # target attribute name in homeo (e.g. "position"), None = whole
    accepts: frozenset[T] = frozenset()
    description: str = ""
    literal_values: tuple[str, ...] | None = None  # allowed string results, if restricted
    on_params: dict[str, OnParam] = {}
    follows: bool = False  # can follow a behavior (D23)

    def accepts_for(self, state: EntityState | None) -> tuple[frozenset[T], str]:
        """The result types this entity takes, and how to describe them."""
        return self.accepts, self.description

    def supports_transition(self, state: EntityState | None) -> bool:
        return False

    def with_transition(self, calls: list[ServiceCall], seconds: float) -> list[ServiceCall]:
        return calls

    def resolution(self, name: str | None, state: EntityState | None, value: float) -> float:
        """The smallest change of a setting (`name`; None for the value itself) worth a step."""
        return 1.0

    def bounds(self, name: str | None, state: EntityState | None) -> tuple[float, float] | None:
        """The range a numeric setting (`name`; None for the value itself) is clamped to."""
        return None

    def step_time(self, value: Any, state: EntityState | None) -> float:
        """How long until a behavior has moved by one step of resolution (D23)."""
        best = float("inf")
        parts = value.params if isinstance(value, OnSpec) else ((None, value),)
        for name, v in parts:
            if isinstance(v, Num) and v.rlo != 0:
                best = min(best, self.resolution(name, state, abs(v.lo)) / abs(v.rlo))
        return best

    @property
    def read_attrs(self) -> frozenset[str | None]:
        """What `matches` reads: attribute names, or None for the state itself. A rule that
        reads one of these reads its own target."""
        return frozenset({None})

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


def _seconds(x: float) -> float | int:
    """A transition length for service data: tenths of a second are enough."""
    return _clean(round(x, 1))


def _pct_to_255(v: Any) -> float:
    return round(255 * _num(v) / 100)


class OnOffWriter(Writer):
    def __init__(
        self,
        domain: str,
        on_params: dict[str, OnParam] | None = None,
        transition_feature: int | None = None,
    ):
        self.domain = domain
        self.on_params = on_params or {}
        self.follows = bool(self.on_params)
        self.transition_feature = transition_feature
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

    def supports_transition(self, state: EntityState | None) -> bool:
        if self.transition_feature is None or state is None:
            return False
        try:
            features = int(state.attributes.get("supported_features") or 0)
        except (TypeError, ValueError):
            return False
        return bool(features & self.transition_feature)

    def with_transition(self, calls: list[ServiceCall], seconds: float) -> list[ServiceCall]:
        out = []
        for c in calls:
            if c.service == "turn_on":
                c = ServiceCall(
                    c.domain, c.service, {**c.data, "transition": _seconds(seconds)}, c.entity_id
                )
            out.append(c)
        return out

    def resolution(self, name: str | None, state: EntityState | None, value: float) -> float:
        p = self.on_params.get(name or "")
        return p.resolution(state, value) if p else 1.0

    def bounds(self, name: str | None, state: EntityState | None) -> tuple[float, float] | None:
        p = self.on_params.get(name or "")
        return p.bounds(state) if p else None

    @property
    def read_attrs(self) -> frozenset[str | None]:
        return frozenset({None, *(p.read_attr for p in self.on_params.values())})

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
                if not _within(actual, v, p.to_read, p.tolerance(state)):
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
        lambda s, v: 1.0,
        lambda s: (0.0, 100.0),
    ),
    "kelvin": OnParam(
        "kelvin",
        T.NUMBER,
        "color_temp_kelvin",
        "color_temp_kelvin",
        lambda v: int(round(_num(v))),
        lambda v: _num(v),
        lambda s: 0.02 * (_num(s.attributes.get("color_temp_kelvin")) or 3000),
        lambda s, v: max(10.0, 0.01 * v),
        lambda s: _attr_bounds(s, "min_color_temp_kelvin", "max_color_temp_kelvin"),
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
        lambda s, v: (_num(s.attributes.get("percentage_step")) if s else None) or 1.0,
        lambda s: (0.0, 100.0),
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
        resolution=None,
        bounds=None,
        percent_unit: bool = False,
    ):
        self.domain = domain
        self.percent_unit = percent_unit  # takes a percentage when the entity's unit is %
        self.attr = attr
        self.service = service
        self.key = key
        self.accepts = frozenset(accepts)
        self.description = description
        self.read_attr = read_attr
        self.convert = convert or (lambda v: v)
        self.expected = expected or self.convert
        self.tolerance = tolerance
        self._resolution = resolution
        self._bounds = bounds
        self.follows = resolution is not None

    def accepts_for(self, state: EntityState | None) -> tuple[frozenset[T], str]:
        unit = state.attributes.get("unit_of_measurement") if state is not None else None
        if self.percent_unit and unit == "%":
            return frozenset({T.PERCENT}), "a percentage"
        return self.accepts, self.description

    def calls(self, entity_id: str, value: Any) -> list[ServiceCall]:
        return [ServiceCall(self.domain, self.service, {self.key: self.convert(value)}, entity_id)]

    @property
    def read_attrs(self) -> frozenset[str | None]:
        return frozenset({self.read_attr})

    def resolution(self, name: str | None, state: EntityState | None, value: float) -> float:
        return self._resolution(state) if self._resolution else 1.0

    def bounds(self, name: str | None, state: EntityState | None) -> tuple[float, float] | None:
        return self._bounds(state) if self._bounds else None

    def _actual(self, state: EntityState) -> Any:
        return state.attributes.get(self.read_attr) if self.read_attr else state.state

    def matches(self, state: EntityState | None, value: Any) -> bool:
        if state is None:
            return False
        actual = self._actual(state)
        if self.tolerance is not None:
            return _within(_num(actual), value, self.expected, self.tolerance(state))
        return actual == self.expected(value)

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


def _attr_bounds(state: EntityState | None, lo: str, hi: str) -> tuple[float, float] | None:
    """A setting's range from the entity's own attributes (e.g. `min`/`max`), if it has both."""
    if state is None:
        return None
    a, b = _num(state.attributes.get(lo)), _num(state.attributes.get(hi))
    return (a, b) if a is not None and b is not None and a <= b else None


def _step(attr: str, default: float):
    """Resolution of a stepped value: the entity's own step attribute (D23)."""

    def res(state: EntityState | None) -> float:
        step = _num(state.attributes.get(attr)) if state is not None else None
        return step if step and step > 0 else default

    return res


def build_writers() -> dict[tuple[str, str | None], Writer]:
    w: dict[tuple[str, str | None], Writer] = {}
    for d in ("switch", "input_boolean", "siren"):
        w[(d, None)] = OnOffWriter(d)
    w[("light", None)] = OnOffWriter("light", LIGHT_PARAMS, LIGHT_TRANSITION_FEATURE)
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
        resolution=_step("target_temp_step", 0.5),
        bounds=lambda s: _attr_bounds(s, "min_temp", "max_temp"),
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
        {T.PERCENT},
        "a position from 0% to 100%",
        attr="position",
        read_attr="current_position",
        convert=lambda v: int(round(_num(v))),
        tolerance=lambda s: 1,
        resolution=lambda s: 1.0,
        bounds=lambda s: (0.0, 100.0),
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
            resolution=_step("step", 1.0),
            bounds=lambda s: _attr_bounds(s, "min", "max"),
            percent_unit=True,
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
        resolution=lambda s: 1.0,
        bounds=lambda s: (0.0, 100.0),
    )
    return w


WRITERS = build_writers()

# Domains bound as a whole: their attributes can't be bound separately (§7).
WHOLE_ONLY = {"light", "fan"}
