"""Local-time helpers: daily times (fixed or solar), ranges, days, `every` ticks.

DST (I4): a wall-clock time that doesn't exist on a day (spring forward) resolves to the first
instant after the gap; an ambiguous one (fall back) resolves to its first occurrence. So each
daily time happens exactly once per local day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from .values import DailyTime

Solar = Callable[[str, date], float | None]


def local_date(t: float, tz: tzinfo) -> date:
    return datetime.fromtimestamp(t, tz).date()


def wall_to_utc(day: date, seconds: int, tz: tzinfo) -> float:
    """UTC epoch seconds for a local wall-clock time on `day` (I4)."""
    extra_days, rem = divmod(int(seconds), 86400)
    day = day + timedelta(days=extra_days)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    naive = datetime.combine(day, time(h, m, s))
    first = naive.replace(tzinfo=tz, fold=0)
    t0 = first.timestamp()
    if datetime.fromtimestamp(t0, tz).replace(tzinfo=None) == naive:
        return t0  # exists (fold=0 is the first occurrence if ambiguous)
    # In a gap: find the transition instant between the two candidate interpretations.
    t1 = naive.replace(tzinfo=tz, fold=1).timestamp()
    lo, hi = min(t0, t1), max(t0, t1)
    off_lo = datetime.fromtimestamp(lo, tz).utcoffset()
    for _ in range(40):
        mid = (lo + hi) / 2
        if datetime.fromtimestamp(mid, tz).utcoffset() == off_lo:
            lo = mid
        else:
            hi = mid
    return round(hi)


def resolve(dt: DailyTime, day: date, tz: tzinfo, solar: Solar) -> float | None:
    """The instant `dt` happens for local date `day` (None if a solar event doesn't occur)."""
    if dt.seconds is not None:
        return wall_to_utc(day, dt.seconds, tz) + dt.offset
    assert dt.solar is not None
    base = solar(dt.solar, day)
    if base is None:
        return None
    return base + dt.offset


# How far to search for occurrences: fixed times recur daily; solar events may skip days
# (polar day/night), so search further.
def _span(dt: DailyTime) -> int:
    return 3 if dt.seconds is not None else 190


def last_occurrence(dt: DailyTime, now: float, tz: tzinfo, solar: Solar) -> float | None:
    """The most recent occurrence at or before `now`."""
    today = local_date(now, tz)
    best: float | None = None
    for back in range(-1, _span(dt)):
        t = resolve(dt, today - timedelta(days=back), tz, solar)
        if t is not None and t <= now and (best is None or t > best):
            best = t
        if best is not None and back >= 1:
            break
    return best


def next_occurrence(dt: DailyTime, now: float, tz: tzinfo, solar: Solar) -> float | None:
    """The first occurrence strictly after `now`."""
    today = local_date(now, tz)
    best: float | None = None
    for fwd in range(-1, _span(dt)):
        t = resolve(dt, today + timedelta(days=fwd), tz, solar)
        if t is not None and t > now and (best is None or t < best):
            best = t
        if best is not None and fwd >= 1:
            break
    return best


def in_range(
    start: DailyTime, end: DailyTime, now: float, tz: tzinfo, solar: Solar
) -> tuple[bool, float]:
    """`now in start..end` (I3) and the next instant it may change."""
    ls = last_occurrence(start, now, tz, solar)
    le = last_occurrence(end, now, tz, solar)
    if ls is None:
        inside = False
    elif le is None:
        inside = True
    else:
        inside = ls > le
    wakes = [
        w
        for w in (next_occurrence(start, now, tz, solar), next_occurrence(end, now, tz, solar))
        if w is not None
    ]
    return inside, min(wakes) if wakes else float("inf")


def next_midnight(now: float, tz: tzinfo) -> float:
    return wall_to_utc(local_date(now, tz) + timedelta(days=1), 0, tz)


def weekday(now: float, tz: tzinfo) -> int:
    return datetime.fromtimestamp(now, tz).weekday()


def every_last_tick(period: float, now: float, tz: tzinfo) -> tuple[float, float]:
    """Last tick at or before `now` and the next tick (I6): aligned to local midnight when
    `period` divides a day, else to the Unix epoch."""
    if 86400 % period == 0:
        midnight = wall_to_utc(local_date(now, tz), 0, tz)
        k = int((now - midnight) // period)
        last = midnight + k * period
        nxt = min(last + period, next_midnight(now, tz))
        return last, nxt
    k = int(now // period)
    return k * period, (k + 1) * period


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()
