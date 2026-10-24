"""Runtime values.

Numbers, durations and timestamps are all `Num`: an interval `[lo, hi]` whose bounds move with
the clock at rates `rlo`/`rhi` per second (I1). Constants are exact (`lo == hi`) with rate 0;
`now` and `age(x)` have rate 1. A timestamp known only to be "at or before T" (a change time from
before a restart) is `[-inf, T]`; "never happened" is `NEVER = [-inf, -inf]`.

Comparisons of `Num`s are three-valued (definite only if the whole interval is on one side) and
report the instant at which their result could next change, so the runtime can schedule a
wake-up instead of polling. Comparisons are evaluated "just after" the evaluation instant
(right limit), so at the exact crossing time of a moving value the comparison already shows its
new result and no epsilon wake-ups are needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

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


def _sign_after(v: float, r: float) -> int:
    """Sign of a bound just after now (right limit)."""
    if math.isnan(v):
        return 0
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 1 if r > 0 else (-1 if r < 0 else 0)


def _crossing(v: float, r: float, now: float) -> float:
    """When a bound with value v and rate r next changes sign (strictly after now)."""
    if r == 0 or math.isinf(v) or math.isnan(v):
        return INF
    t = -v / r
    return now + t if t > 0 else INF


def compare(op: str, a: Num, b: Num, now: float) -> tuple[bool | _Unknown, float]:
    """Three-valued comparison of `a op b`, and the next instant its result may change."""
    d = a - b
    lo = _sign_after(d.lo, d.rlo)  # sign of the smallest possible a - b
    hi = _sign_after(d.hi, d.rhi)  # sign of the largest possible a - b
    wake = min(_crossing(d.lo, d.rlo, now), _crossing(d.hi, d.rhi, now))
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


def parse_time_of_day(raw: object) -> DailyTime | object:
    """A time-of-day entity's state (`input_datetime` without a date, `time.*`): `HH:MM:SS`,
    maybe with fractions of a second. Anything else is unknown."""
    from datetime import time

    try:
        t = time.fromisoformat(str(raw))
    except ValueError:
        return UNKNOWN
    return DailyTime(t.hour * 3600 + t.minute * 60 + t.second)


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
