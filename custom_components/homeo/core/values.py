"""Runtime values.

Numbers, durations and timestamps are all `Num`: an interval `[lo, hi]` whose bounds move with
the clock at rates `rlo`/`rhi` per second (I1). Constants are exact (`lo == hi`) with rate 0;
`now` and `age(x)` have rate 1. A timestamp known only to be "at or before T" (a change time from
before a restart) is `[-inf, T]`; "never happened" is `NEVER = [-inf, -inf]`.

Comparisons of `Num`s are three-valued (definite only if the whole interval is on one side) and
report the instant at which their result could next change, so the runtime can schedule a
wake-up instead of polling. Comparisons are evaluated "just after" the evaluation instant
(right limit), so at the exact crossing time of a moving value the comparison already shows its
new result and no epsilon wake-ups are needed. Whether a line has reached a crossing is decided
on the clock, by `side`, so rounding can't leave it a hair short with its wake-up lost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

INF = math.inf


class _Unknown:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self) -> str:
        return "unknown"

    def __bool__(self) -> bool:
        raise TypeError("unknown has no truth value")


class _Keep:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self) -> str:
        return "keep"


UNKNOWN = _Unknown()
KEEP = _Keep()


@dataclass(frozen=True, slots=True)
class Num:
    lo: float
    hi: float
    rlo: float = 0.0
    rhi: float = 0.0

    @staticmethod
    def of(v: float) -> Num:
        return Num(float(v), float(v))

    @property
    def exact(self) -> bool:
        return self.lo == self.hi and self.rlo == self.rhi

    @property
    def moving(self) -> bool:
        return self.rlo != 0 or self.rhi != 0

    @property
    def value(self) -> float:
        """The value, for exact numbers."""
        return self.lo

    def at(self, dt: float) -> Num:
        """This value `dt` seconds later."""
        return Num(self.lo + self.rlo * dt, self.hi + self.rhi * dt, self.rlo, self.rhi)

    def __add__(self, o: Num) -> Num:
        return Num(self.lo + o.lo, self.hi + o.hi, self.rlo + o.rlo, self.rhi + o.rhi)

    def __sub__(self, o: Num) -> Num:
        return Num(self.lo - o.hi, self.hi - o.lo, self.rlo - o.rhi, self.rhi - o.rlo)

    def __neg__(self) -> Num:
        return Num(-self.hi, -self.lo, -self.rhi, -self.rlo)

    def scale(self, k: float) -> Num:
        if k >= 0:
            return Num(self.lo * k, self.hi * k, self.rlo * k, self.rhi * k)
        return Num(self.hi * k, self.lo * k, self.rhi * k, self.rlo * k)

    def __repr__(self) -> str:
        if self.exact and not self.moving:
            return f"Num({self.lo:g})"
        return f"Num([{self.lo:g}, {self.hi:g}] rates {self.rlo:g},{self.rhi:g})"


NEVER = Num(-INF, -INF)


def num_mul(a: Num, b: Num) -> Num | _Unknown:
    if b.exact and not b.moving:
        return a.scale(b.lo)
    if a.exact and not a.moving:
        return b.scale(a.lo)
    if a.moving or b.moving:
        return UNKNOWN  # product of two moving values isn't linear; checker prevents this
    corners = [a.lo * b.lo, a.lo * b.hi, a.hi * b.lo, a.hi * b.hi]
    return Num(min(corners), max(corners))


def num_div(a: Num, b: Num) -> Num | _Unknown:
    if b.exact and not b.moving:
        if b.lo == 0:
            return UNKNOWN
        return a.scale(1.0 / b.lo)
    if a.moving or b.moving or b.lo <= 0 <= b.hi:
        return UNKNOWN
    corners = [a.lo / b.lo, a.lo / b.hi, a.hi / b.lo, a.hi / b.hi]
    return Num(min(corners), max(corners))


def side(v: float, r: float, target: float, now: float) -> tuple[int, float]:
    """Where a line at `v`, moving at `r`, is just after `now` relative to `target` (-1 below,
    0 on it, 1 above), and the instant that next changes.

    Both answers come from one reading of the clock: the line reaches `target` at
    `now + (target - v) / r`, and if that instant is not after `now`, it is there. So rounding
    can't leave a crossing in between, a hair away with a wake-up that rounds to `now`."""
    if math.isnan(v):
        return 0, INF
    if r == 0 or math.isinf(v):
        return (v > target) - (v < target), INF
    t = now + (target - v) / r
    if t <= now:  # reached or receding: the side it moves to, for good
        return (1 if r > 0 else -1), INF
    return (1 if v > target else -1), t


def compare(op: str, a: Num, b: Num, now: float) -> tuple[bool | _Unknown, float]:
    """Three-valued comparison of `a op b`, and the next instant its result may change."""
    d = a - b
    lo, wake_lo = side(d.lo, d.rlo, 0.0, now)  # sign of the smallest possible a - b
    hi, wake_hi = side(d.hi, d.rhi, 0.0, now)  # sign of the largest possible a - b
    wake = min(wake_lo, wake_hi)
    if op == "<":
        result = True if hi < 0 else False if lo >= 0 else UNKNOWN
    elif op == "<=":
        result = True if hi <= 0 else False if lo > 0 else UNKNOWN
    elif op == ">":
        result = True if lo > 0 else False if hi <= 0 else UNKNOWN
    elif op == ">=":
        result = True if lo >= 0 else False if hi < 0 else UNKNOWN
    elif op in ("==", "!="):
        if lo == 0 and hi == 0:
            result = True
        elif hi < 0 or lo > 0:
            result = False
        else:
            result = UNKNOWN
        if op == "!=" and result is not UNKNOWN:
            result = not result
    else:
        raise ValueError(op)
    return result, wake


@dataclass(frozen=True, slots=True)
class OnSpec:
    """`on(brightness: 80%, kelvin: 2700)`: params as (name, value) pairs, sorted."""

    params: tuple[tuple[str, object], ...]

    def get(self, name: str):
        for k, v in self.params:
            if k == name:
                return v
        return None

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}: {v!r}" for k, v in self.params)
        return f"on({inner})"


@dataclass(frozen=True, slots=True)
class DailyTime:
    """A time of day: fixed (`seconds` since local midnight) or solar (`solar` event name)
    plus an offset in seconds."""

    seconds: int | None = None
    solar: str | None = None
    offset: float = 0.0

    def shifted(self, delta: float) -> DailyTime:
        return DailyTime(self.seconds, self.solar, self.offset + delta)

    def __repr__(self) -> str:
        from .printer import fmt_duration, fmt_time

        base = fmt_time(self.seconds) if self.seconds is not None else self.solar
        if self.offset:
            sign = "+" if self.offset > 0 else "-"
            return f"{base} {sign} {fmt_duration(abs(self.offset))}"
        return str(base)


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class Days:
    """A set of weekdays (0 = Monday)."""

    days: frozenset[int]

    def __repr__(self) -> str:
        return "{" + ",".join(DAY_NAMES[d] for d in sorted(self.days)) + "}"


def parse_timestamp(s: str) -> float | None:
    """UTC epoch seconds of an ISO 8601 timestamp with a time zone (HA's format), else None."""
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def kleene_not(a):
    return UNKNOWN if a is UNKNOWN else (not a)


def kleene_and(a, b):
    if a is False or b is False:
        return False
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return True


def kleene_or(a, b):
    if a is True or b is True:
        return True
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return False


def same_value(a, b) -> bool:
    """Equality used for change detection."""
    if a is b:
        return True
    if isinstance(a, Num) and isinstance(b, Num):
        return a == b
    if type(a) is not type(b):
        return False
    return a == b


# --------------------------------------------------------------- behaviors (D23)
#
# A binding result may contain numbers that move with the clock (a behavior). These helpers
# work on whole results: a `Num`, or an `OnSpec` whose settings are `Num`s.


def _map_nums(v: Any, f) -> Any:
    if isinstance(v, Num):
        return f(v)
    if isinstance(v, OnSpec):
        return OnSpec(tuple((k, _map_nums(x, f)) for k, x in v.params))
    return v


def _nums(v: Any) -> list[Num]:
    if isinstance(v, Num):
        return [v]
    if isinstance(v, OnSpec):
        return [x for _, x in v.params if isinstance(x, Num)]
    return []


def is_moving(v: Any) -> bool:
    return any(n.moving for n in _nums(v))


def is_definite(v: Any) -> bool:
    """Every number in the result is known exactly (not only as a range) and finite."""
    return all(
        n.lo == n.hi and n.rlo == n.rhi and math.isfinite(n.lo) and math.isfinite(n.rlo)
        for n in _nums(v)
    )


def advance(v: Any, dt: float) -> Any:
    """The result `dt` seconds later."""
    return _map_nums(v, lambda n: n.at(dt))


def snapshot(v: Any) -> Any:
    """The result's value at this instant, with nothing moving."""
    return _map_nums(v, lambda n: Num(n.lo, n.hi))


def hull(a: Any, b: Any) -> Any:
    """Every value between two snapshots of one result (settings pairwise), as ranges."""
    if isinstance(a, Num) and isinstance(b, Num):
        return Num(min(a.lo, b.lo), max(a.hi, b.hi))
    if isinstance(a, OnSpec) and isinstance(b, OnSpec):
        other = dict(b.params)
        if set(other) != {k for k, _ in a.params}:
            return b
        return OnSpec(tuple((k, hull(x, other[k])) for k, x in a.params))
    return b


def _close(x: float, y: float) -> bool:
    return x == y or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-6)


def same_line(a: Any, b: Any) -> bool:
    """Whether two results (taken at the same instant) are the same behavior: equal values
    and equal rates, up to float rounding."""
    if isinstance(a, Num) and isinstance(b, Num):
        return all(
            _close(x, y) for x, y in ((a.lo, b.lo), (a.hi, b.hi), (a.rlo, b.rlo), (a.rhi, b.rhi))
        )
    if isinstance(a, OnSpec) and isinstance(b, OnSpec):
        if [k for k, _ in a.params] != [k for k, _ in b.params]:
            return False
        return all(same_line(x, y) for (_, x), (_, y) in zip(a.params, b.params, strict=True))
    return same_value(a, b)
