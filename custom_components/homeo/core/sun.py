"""Solar times from HA's `sun.sun` entity (D4, I5).

`sun.sun` publishes only the *next* occurrence of each event (`next_rising`, `next_setting`,
`next_dawn`, `next_dusk`) and moves it forward as each one passes. Rules also need the most
recent occurrence (`fresh(sunset)`, `on sunset + 30min:`, `now in sunset..sunrise`), so the
tracker remembers each occurrence when `sun.sun` rolls it forward. Until it has seen one (right
after homeo starts), the previous occurrence is estimated as the next one minus 24 h: off by a
few minutes, in line with D17 (restarts are rare; predictable beats exact).
"""

from __future__ import annotations

from datetime import date, timedelta, tzinfo

from .host import EntityState
from .timeutil import local_date
from .values import parse_timestamp

SUN_ENTITY = "sun.sun"
EVENT_ATTRS = {
    "sunrise": "next_rising",
    "sunset": "next_setting",
    "dawn": "next_dawn",
    "dusk": "next_dusk",
}
DAY = 86400.0


class SunTracker:
    def __init__(self, tz: tzinfo):
        self.tz = tz
        self.available = False
        self.next: dict[str, float] = {}
        self.last: dict[str, float] = {}  # occurrences seen passing since homeo started

    def update(self, state: EntityState | None, now: float) -> None:
        """Take in sun.sun's current state."""
        if state is None or not state.available:
            self.available = False
            return
        nexts = {}
        for event, attr in EVENT_ATTRS.items():
            raw = state.attributes.get(attr)
            t = parse_timestamp(raw) if isinstance(raw, str) else None
            if t is None:
                self.available = False
                return
            nexts[event] = t
        for event, t in nexts.items():
            old = self.next.get(event)
            if old is not None and t != old and old <= now:
                self.last[event] = old  # it just happened; sun.sun moved on to the next one
        self.next = nexts
        self.available = True

    def snapshot(self) -> tuple:
        """Changes exactly when solar times change (not on elevation/azimuth updates)."""
        return tuple(sorted(self.next.items())) + tuple(sorted(self.last.items()))

    def occurrence(self, event: str, day: date) -> float | None:
        """When `event` happens on local date `day`: a known occurrence if there is one that
        day, else extrapolated from the nearest known one by whole days."""
        if not self.available or event not in self.next:
            return None
        known = [self.next[event]]
        if event in self.last:
            known.append(self.last[event])
        for t in known:
            if local_date(t, self.tz) == day:
                return t
        base = min(known, key=lambda t: abs((local_date(t, self.tz) - day).days))
        return base + (day - local_date(base, self.tz)) / timedelta(days=1) * DAY
