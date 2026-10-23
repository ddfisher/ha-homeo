"""The runtime: runs compiled programs against a Host (implementation plan §2).

- Cells hold values with change times; evaluation runs in glitch-free waves in rank order (I2).
- Time quantities are intervals that move with the clock (I1); evaluating a cell records the
  next instant time alone could change it, and one host timer covers the earliest.
- State machines settle (chains, loop detection) before bindings and actions run.
- Bindings write when their known result changes (D16); actions fire once per occurrence (D14).
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import ir
from .host import (
    TRANSITIONAL_STATES,
    UNAVAILABLE_STATES,
    Host,
    ServiceCall,
    StateChange,
)
from .sun import SunTracker
from .timeutil import (
    every_last_tick,
    in_range,
    last_occurrence,
    next_midnight,
    next_occurrence,
    weekday,
)
from .types import T
from .values import (
    INF,
    KEEP,
    NEVER,
    UNKNOWN,
    Days,
    Num,
    OnSpec,
    compare,
    kleene_and,
    kleene_not,
    kleene_or,
    num_div,
    num_mul,
    parse_timestamp,
    same_value,
)


class _Nothing:
    def __repr__(self) -> str:
        return "nothing"


NOTHING = _Nothing()  # "no known value yet"
NOMATCH = object()  # no arm matched
MAX_SETTLE_ROUNDS = 100


def maybe_before(t: float) -> Num:
    """A time known only to be at or before t (or never)."""
    return Num(-INF, t)


def to_bool(v: Any) -> Any:
    if v is True or v is False:
        return v
    if isinstance(v, str):
        s = v.lower()
        if s in ("on", "true"):
            return True
        if s in ("off", "false"):
            return False
    return UNKNOWN


@dataclass(eq=False)
class RCell:
    defn: ir.CellDef
    rank: int = 0
    dependents: list[RCell] = field(default_factory=list)
    value: Any = UNKNOWN
    known: Any = NOTHING
    ct: Num = field(default_factory=lambda: maybe_before(0.0))  # change time
    t_true: Num = field(default_factory=lambda: NEVER)  # when it last became true
    prev: Any = UNKNOWN
    wake: float = INF
    extra: dict[str, Any] = field(default_factory=dict)  # per-kind state
    failed: bool = False  # machines: stopped after a loop

    @property
    def key(self) -> str:
        return self.defn.key

    def occurrence(self) -> Num:
        """t(x) (§3): the value for timestamps, when it last became true for bools."""
        if self.defn.type == T.TIMESTAMP:
            v = self.known
            return v if isinstance(v, Num) else NEVER
        return self.t_true


@dataclass(eq=False)
class RSink:
    """A binding or action."""

    defn: ir.BindingDef | ir.ActionDef
    rank: int = 0
    wake: float = INF
    last_known: Any = NOTHING  # bindings
    last_written: Any = NOTHING  # bindings
    last_handled: float = 0.0  # actions
    dry_run: bool = False

    @property
    def key(self) -> str:
        return self.defn.key


class Runtime:
    def __init__(self, host: Host):
        self.host = host
        self.programs: dict[str, ir.Program] = {}
        self.sun = SunTracker(host.tz)  # kept across reloads: it remembers past solar events
        self.cells: dict[str, RCell] = {}
        self.sinks: list[RSink] = []
        self.machines: dict[str, RCell] = {}
        self.bindings_by_entity: dict[str, list[RSink]] = {}
        self.entity_cells: dict[str, list[RCell]] = {}
        self.bus_cells: dict[str, list[RCell]] = {}
        self.deps_of: dict[int, list[RCell]] = {}
        self.now = host.now()
        self.started = False
        self._wake_heap: list[tuple[float, int, Any]] = []
        self._seq = itertools.count()
        self._timer_cancel = None
        self._timer_at = INF
        self._unsubs: list[Any] = []
        self._queue: list[tuple[str, Any]] = []
        self._busy = False
        self._cur_wake = INF
        self._machine_ctx: RCell | None = None
        self._dirty: set[int] = set()
        self._dirty_items: list[Any] = []
        self._machines_to_eval: set[RCell] = set()

    # ================================================================ loading

    def load(self, programs: dict[str, ir.Program]) -> None:
        """Replace all programs, carrying state over by key (D17 rule 6)."""
        self._enqueue(("load", dict(programs)))

    def start(self) -> None:
        """Begin evaluating (call once HA has started, D17 rule 1)."""
        self.started = True
        self._enqueue(("load", dict(self.programs)))

    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        self.started = False

    def _do_load(self, programs: dict[str, ir.Program]) -> None:
        self.now = self.host.now()
        old_cells, old_sinks, old_machines = (
            self.cells,
            {s.key: s for s in self.sinks},
            self.machines,
        )
        self.programs = programs
        if not self.started:
            return
        for u in self._unsubs:
            u()
        self._unsubs = []

        # cells, shared across files by key
        defs: dict[str, ir.CellDef] = {}
        for p in programs.values():
            for k, c in p.cells.items():
                defs.setdefault(k, c)
        self.cells = {k: RCell(c) for k, c in defs.items()}
        self.deps_of = {}
        for rc in self.cells.values():
            deps = [self.cells[d.key] for d in rc.defn.deps()]
            self.deps_of[id(rc)] = deps
        self.machines = {
            rc.defn.name: rc for rc in self.cells.values() if isinstance(rc.defn, ir.MachineCell)
        }

        # sinks
        self.sinks = []
        for p in programs.values():
            for b in p.bindings:
                self.sinks.append(RSink(b, dry_run=p.dry_run))
            for a in p.actions:
                self.sinks.append(RSink(a, dry_run=p.dry_run))
        self._rank()

        # indexes
        self.bindings_by_entity = {}
        for s in self.sinks:
            if isinstance(s.defn, ir.BindingDef):
                self.bindings_by_entity.setdefault(s.defn.entity_id, []).append(s)
        self.entity_cells = {}
        self.bus_cells = {}
        for rc in self.cells.values():
            d = rc.defn
            eid = getattr(d, "entity_id", None)
            if eid:
                self.entity_cells.setdefault(eid, []).append(rc)
            if isinstance(d, ir.BusCell):
                self.bus_cells.setdefault(d.event_type, []).append(rc)

        # initial values, carrying over old state
        self._wake_heap = []
        for rc in sorted(self.cells.values(), key=lambda c: c.rank):
            old = old_cells.get(rc.key)
            if isinstance(rc.defn, ir.MachineCell):
                self._init_machine(rc, old_machines.get(rc.defn.name))
                continue
            if old is not None:
                rc.value, rc.known, rc.ct, rc.t_true, rc.prev = (
                    old.value,
                    old.known,
                    old.ct,
                    old.t_true,
                    old.prev,
                )
                rc.extra = dict(old.extra)
                self._recompute(rc)
            else:
                self._init_cell(rc)
        for s in self.sinks:
            old = old_sinks.get(s.key)
            if old is not None and type(old.defn) is type(s.defn):
                s.last_known, s.last_written, s.last_handled = (
                    old.last_known,
                    old.last_written,
                    old.last_handled,
                )
            else:
                s.last_handled = self.now

        # subscriptions
        entity_ids = sorted(set(self.entity_cells) | set(self.bindings_by_entity))
        if entity_ids:
            self._unsubs.append(self.host.subscribe_states(entity_ids, self.on_state_change))
        for et in self.bus_cells:
            self._unsubs.append(
                self.host.subscribe_bus(
                    et, lambda data, t, et=et: self._enqueue(("bus", (et, data, t)))
                )
            )

        # settle machines from their initial values, then evaluate every sink
        self._machines_to_eval = set(self.machines.values())
        for m in self.machines.values():
            self._publish(m)
        self._propagate([], all_sinks=True)

    def _rank(self) -> None:
        ranks: dict[int, int] = {}

        def rank(rc: RCell, stack: set[int]) -> int:
            if id(rc) in ranks:
                return ranks[id(rc)]
            if isinstance(rc.defn, ir.MachineCell):
                ranks[id(rc)] = 0
                return 0
            if id(rc) in stack:
                raise RuntimeError(f"cycle through {rc.key}")
            stack.add(id(rc))
            r = 0
            for d in self.deps_of[id(rc)]:
                r = max(r, rank(d, stack) + 1)
            stack.discard(id(rc))
            ranks[id(rc)] = r
            return r

        for rc in self.cells.values():
            rc.rank = rank(rc, set())
            rc.dependents = []
        for rc in self.cells.values():
            for d in self.deps_of[id(rc)]:
                d.dependents.append(rc)
        top = max((rc.rank for rc in self.cells.values()), default=0) + 1
        for s in self.sinks:
            deps = [self.cells[d.key] for d in self._sink_deps(s)]
            s.rank = top
            for d in deps:
                d.dependents.append(s)  # type: ignore[arg-type]

    def _sink_deps(self, s: RSink) -> list[ir.CellDef]:
        if isinstance(s.defn, ir.ActionDef):
            return [s.defn.trigger]
        return s.defn.deps()

    # ============================================================= cell values

    def _init_cell(self, rc: RCell) -> None:
        """Initial value of a new cell (D17). A change time from before the restart means
        "some time before the restart" (rule 2); one after boot is a real change, so it's exact."""
        d = rc.defn
        self._cur_wake = INF
        if isinstance(d, ir.EventTypeCell):
            rc.extra["t"] = self._initial_event_type(d)
        elif isinstance(d, ir.ManualCell):
            rc.extra["t"] = NEVER  # manual() history isn't kept across restarts (§17)
        elif isinstance(d, ir.BusCell):
            rc.extra["t"] = maybe_before(self.now)
        elif isinstance(d, ir.ToggleCell):
            rc.extra["seen"] = self.cells[d.source.key].occurrence()
            rc.extra["on"] = d.initial
        elif isinstance(d, ir.CountCell):
            rc.extra["seen"] = self.cells[d.source.key].occurrence()
            rc.extra["times"] = []
        v = self._compute(rc)
        rc.wake = self._cur_wake
        self._push_wake(rc)
        rc.value = v
        rc.ct = maybe_before(self.now)
        if isinstance(d, ir.ToggleCell):
            rc.ct = Num.of(self.now)
        elif isinstance(d, ir.EntityCell):
            rc.ct = self._initial_change_time(d)
        if v is UNKNOWN:
            rc.known = NOTHING
            rc.t_true = maybe_before(self.now)
        else:
            rc.known = v
            rc.t_true = rc.ct if to_bool(v) is True else maybe_before(rc.ct.hi)

    def _initial_change_time(self, d: ir.EntityCell) -> Num:
        st = self.host.get_state(d.entity_id)
        boot = self.host.boot_time
        if st is None:
            return maybe_before(self.now)
        if d.attr is None:
            if boot < st.last_changed <= self.now:
                return Num.of(st.last_changed)  # changed since boot: exact
            return maybe_before(min(boot, self.now))  # some time before the restart
        # HA doesn't track attribute changes; it changed at or before the last update.
        return maybe_before(min(self.now, max(boot, st.last_updated)))

    def _initial_event_type(self, d: ir.EventTypeCell) -> Num:
        st = self.host.get_state(d.entity_id)
        if st is None or st.state == "unavailable":
            return maybe_before(self.now)
        if st.state == "unknown":
            return NEVER
        t = parse_timestamp(st.state)
        if t is None:
            return maybe_before(self.now)
        if st.attributes.get("event_type") == d.event_type:
            return Num.of(t)
        return maybe_before(t)

    def _compute(self, rc: RCell) -> Any:
        d = rc.defn
        try:
            match d:
                case ir.EntityCell():
                    return self._entity_value(d)
                case ir.EventTypeCell():
                    st = self.host.get_state(d.entity_id)
                    if st is not None and st.attributes.get("event_type") == d.event_type:
                        t = parse_timestamp(st.state)
                        cur = rc.extra.get("t", NEVER)
                        if t is not None and t > cur.hi:
                            rc.extra["t"] = Num.of(t)
                    return rc.extra.get("t", NEVER)
                case ir.ManualCell() | ir.BusCell():
                    return rc.extra.get("t", NEVER)
                case ir.EveryCell():
                    last, nxt = every_last_tick(d.period, self.now, self.host.tz)
                    self._wake_at(nxt)
                    return Num.of(last)
                case ir.SunCell():
                    self.sun.update(self.host.get_state(d.entity_id), self.now)
                    return self.sun.snapshot() if self.sun.available else UNKNOWN
                case ir.TimePointCell():
                    if d.daily.solar is not None and not self.sun.available:
                        return UNKNOWN
                    last = last_occurrence(d.daily, self.now, self.host.tz, self.sun.occurrence)
                    nxt = next_occurrence(d.daily, self.now, self.host.tz, self.sun.occurrence)
                    if nxt is not None:
                        self._wake_at(nxt)
                    return Num.of(last) if last is not None else NEVER
                case ir.ExprCell():
                    return self._eval(d.expr)
                case ir.ToggleCell():
                    occ = self.cells[d.source.key].occurrence()
                    if self._advanced(occ, rc.extra.get("seen", NEVER)):
                        rc.extra["on"] = not rc.extra.get("on", d.initial)
                    rc.extra["seen"] = occ
                    return rc.extra.get("on", d.initial)
                case ir.CountCell():
                    occ = self.cells[d.source.key].occurrence()
                    times: list[float] = rc.extra.setdefault("times", [])
                    if self._advanced(occ, rc.extra.get("seen", NEVER)):
                        times.append(occ.lo)
                    rc.extra["seen"] = occ
                    cutoff = self.now - d.window
                    times[:] = [t for t in times if t > cutoff]
                    if times:
                        self._wake_at(min(times) + d.window)
                    return Num.of(len(times))
                case ir.MachineCell():
                    return rc.value
        except Exception as exc:  # noqa: BLE001 - a bad cell must not stop the others
            self.host.log("error", f"error evaluating {d.text}: {exc!r}")
            return UNKNOWN
        raise TypeError(type(d))

    @staticmethod
    def _advanced(occ: Num, seen: Num) -> bool:
        """A new, exactly known occurrence since `seen`."""
        return occ.lo == occ.hi and occ.lo > seen.hi

    def _entity_value(self, d: ir.EntityCell) -> Any:
        st = self.host.get_state(d.entity_id)
        if st is None:
            return UNKNOWN
        if d.attr is None:
            if d.type == T.TIMESTAMP and st.state == "unknown":
                return NEVER  # never pressed / never fired
            if st.state in UNAVAILABLE_STATES:
                return UNKNOWN
            return self._convert(st.state, d.type)
        if not st.available:
            return UNKNOWN
        raw = st.attributes.get(d.attr)
        if raw is None:
            return UNKNOWN
        return self._convert(raw, d.type)

    @staticmethod
    def _convert(raw: Any, t: T) -> Any:
        if t == T.BOOL:
            return to_bool(raw)
        if t == T.NUMBER:
            if isinstance(raw, bool):
                return UNKNOWN
            try:
                return Num.of(float(raw))
            except (TypeError, ValueError):
                return UNKNOWN
        if t == T.TIMESTAMP:
            if isinstance(raw, datetime):
                return Num.of(raw.timestamp())
            ts = parse_timestamp(str(raw))
            return Num.of(ts) if ts is not None else UNKNOWN
        if t == T.STRING:
            return raw if isinstance(raw, str) else str(raw)
        if t == T.LIST:
            return tuple(raw) if isinstance(raw, (list, tuple)) else UNKNOWN
        # ANY
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return Num.of(raw)
        if isinstance(raw, str):
            try:
                return Num.of(float(raw))
            except ValueError:
                return raw
        return raw

    def _set(self, rc: RCell, v: Any) -> bool:
        """Store a newly computed value; returns whether dependents need updating.
        Change times skip unknown periods (D16)."""
        old_value = rc.value
        if v is UNKNOWN:
            rc.value = UNKNOWN
            return old_value is not UNKNOWN
        if rc.known is NOTHING:
            rc.known = v
            rc.value = v
            rc.prev = UNKNOWN
            rc.ct = maybe_before(self.now)
            if to_bool(v) is True:
                rc.t_true = maybe_before(self.now)
            return True
        if not same_value(v, rc.known):
            rc.prev = rc.known
            rc.known = v
            rc.value = v
            rc.ct = Num.of(self.now)
            if to_bool(v) is True:
                rc.t_true = Num.of(self.now)
            return True
        rc.value = v
        return old_value is UNKNOWN

    def _recompute(self, rc: RCell) -> bool:
        self._cur_wake = INF
        v = self._compute(rc)
        rc.wake = self._cur_wake
        self._push_wake(rc)
        return self._set(rc, v)

    # ============================================================== evaluation

    def _wake_at(self, t: float) -> None:
        if t > self.now and t < self._cur_wake:
            self._cur_wake = t

    def _eval(self, n: ir.Node) -> Any:
        match n:
            case ir.Const(value=v):
                return v
            case ir.Now():
                return Num(self.now, self.now, 1.0, 1.0)
            case ir.Today():
                self._wake_at(next_midnight(self.now, self.host.tz))
                return Days(frozenset({weekday(self.now, self.host.tz)}))
            case ir.Read(cell=c):
                return self.cells[c.key].value
            case ir.Not(operand=o):
                return kleene_not(to_bool(self._eval(o)))
            case ir.BoolOp(op="and", left=a, right=b):
                x = to_bool(self._eval(a))
                if x is False:
                    return False
                return kleene_and(x, to_bool(self._eval(b)))
            case ir.BoolOp(op="or", left=a, right=b):
                x = to_bool(self._eval(a))
                if x is True:
                    return True
                return kleene_or(x, to_bool(self._eval(b)))
            case ir.Neg(operand=o):
                v = self._eval(o)
                return -v if isinstance(v, Num) else UNKNOWN
            case ir.Scale(operand=o, factor=k):
                v = self._num(self._eval(o))
                return v.scale(k) if isinstance(v, Num) else UNKNOWN
            case ir.Arith(op=op, left=a, right=b):
                x, y = self._num(self._eval(a)), self._num(self._eval(b))
                if not isinstance(x, Num) or not isinstance(y, Num):
                    return UNKNOWN
                if op == "+":
                    return x + y
                if op == "-":
                    return x - y
                if op == "*":
                    return num_mul(x, y)
                return num_div(x, y)
            case ir.Compare(op=op, left=a, right=b):
                return self._compare(op, self._eval(a), self._eval(b))
            case ir.Coalesce(left=a, right=b):
                v = self._eval(a)
                return self._eval(b) if v is UNKNOWN else v
            case ir.IfElse(cond=c, then=t, otherwise=o):
                cv = to_bool(self._eval(c))
                if cv is UNKNOWN:
                    return UNKNOWN
                return self._eval(t) if cv else self._eval(o)
            case ir.InList(operand=o, items=items, negate=neg):
                v = self._eval(o)
                result: Any = False
                for it in items:
                    result = kleene_or(result, self._compare("==", v, self._eval(it)))
                return kleene_not(result) if neg else result
            case ir.InTimeRange(start=s, end=e, negate=neg, sun=sun):
                if sun is not None and not self.sun.available:
                    return UNKNOWN
                inside, wake = in_range(s, e, self.now, self.host.tz, self.sun.occurrence)
                self._wake_at(wake)
                return (not inside) if neg else inside
            case ir.DayIn(days=days, negate=neg):
                self._wake_at(next_midnight(self.now, self.host.tz))
                inside = weekday(self.now, self.host.tz) in days.days
                return (not inside) if neg else inside
            case ir.Age(cell=c):
                rc = self.cells[c.key]
                t = rc.occurrence() if c.type == T.TIMESTAMP else rc.ct
                return Num(self.now, self.now, 1.0, 1.0) - t
            case ir.Fresh(cell=c, machine=m):
                occ = self.cells[c.key].occurrence()
                mct = self.machines[m.name].ct
                if occ.lo > mct.hi:
                    return True
                if occ.hi <= mct.lo:
                    return False
                return UNKNOWN
            case ir.Previous(cell=c):
                return self.cells[c.key].prev
            case ir.Available(cell=c):
                return self.cells[c.key].value is not UNKNOWN
            case ir.OnValue(params=params):
                vals = []
                for name, p in params:
                    v = self._eval(p)
                    if v is UNKNOWN:
                        return UNKNOWN
                    vals.append((name, v))
                return OnSpec(tuple(sorted(vals, key=lambda kv: kv[0])))
            case ir.Cascade(arms=arms):
                r = self._eval_arms(arms)
                return UNKNOWN if r is NOMATCH else r
            case ir.Latest(arms=arms):
                return self._eval_latest(arms)
        raise TypeError(f"can't evaluate {type(n).__name__}")

    @staticmethod
    def _num(v: Any) -> Any:
        if isinstance(v, Num):
            return v
        if isinstance(v, str):
            try:
                return Num.of(float(v))
            except ValueError:
                return UNKNOWN
        return UNKNOWN

    def _compare(self, op: str, a: Any, b: Any) -> Any:
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        if isinstance(a, Num) or isinstance(b, Num):
            x, y = self._num(a), self._num(b)
            if not isinstance(x, Num) or not isinstance(y, Num):
                return UNKNOWN if op not in ("==", "!=") else (op == "!=")
            result, wake = compare(op, x, y, self.now)
            self._wake_at(wake)
            return result
        if isinstance(a, bool) or isinstance(b, bool):
            a, b = to_bool(a), to_bool(b)
            if a is UNKNOWN or b is UNKNOWN:
                return UNKNOWN
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        return UNKNOWN

    def _eval_arms(self, arms: list[ir.ArmIR | ir.GroupIR]) -> Any:
        """First match wins; unknown conditions fall through (D16)."""
        for a in arms:
            if isinstance(a, ir.GroupIR):
                if to_bool(self._eval(a.guard)) is True:
                    r = self._eval_arms(a.arms)
                    if r is not NOMATCH:
                        return r
                continue
            if a.cond is None or to_bool(self._eval(a.cond)) is True:
                return self._eval(a.result)
        return NOMATCH

    def _eval_latest(self, arms: list[ir.ArmIR | ir.GroupIR]) -> Any:
        """The arm whose condition happened most recently (D19, I13)."""
        else_arm = next((a for a in arms if isinstance(a, ir.ArmIR) and a.cond is None), None)
        cands = []
        for i, a in enumerate(arms):
            if a is else_arm:
                continue
            cond = a.guard if isinstance(a, ir.GroupIR) else a.cond
            assert isinstance(cond, ir.Read)
            occ = self.cells[cond.cell.key].occurrence()
            if occ.hi > -INF:  # possibly happened
                cands.append((i, a, occ))
        while cands:
            winner = None
            for i, a, occ in cands:
                if all(
                    j == i or occ.lo > o2.hi or (occ.lo == occ.hi == o2.lo == o2.hi and i < j)
                    for j, _, o2 in cands
                ):
                    winner = (i, a, occ)
                    break
            if winner is None:
                return UNKNOWN
            _, a, _ = winner
            if isinstance(a, ir.ArmIR):
                return self._eval(a.result)
            r = self._eval_arms(a.arms)
            if r is not NOMATCH:
                return r
            cands.remove(winner)
        if else_arm is not None:
            return self._eval(else_arm.result)
        return UNKNOWN

    # ============================================================== propagation

    def _mark(self, item: Any) -> None:
        if id(item) not in self._dirty:
            self._dirty.add(id(item))
            self._dirty_items.append(item)

    def _propagate(self, sources: Iterable[Any], all_sinks: bool = False) -> None:
        for s in sources:
            self._mark(s)
        if all_sinks:
            for s in self.sinks:
                self._mark(s)
        pending_sinks: dict[int, RSink] = {}
        visited: dict[int, list[str]] = {id(m): [m.value] for m in self.machines.values()}
        rounds = 0
        while True:
            heap = [(it.rank, next(self._seq), it) for it in self._dirty_items]
            heapq.heapify(heap)
            self._dirty_items = []
            self._dirty = set()
            done: set[int] = set()
            while heap:
                _, _, it = heapq.heappop(heap)
                if id(it) in done:
                    continue
                done.add(id(it))
                if isinstance(it, RSink):
                    pending_sinks[id(it)] = it
                    continue
                if isinstance(it.defn, ir.MachineCell):
                    self._machines_to_eval.add(it)
                    continue
                if self._recompute(it):
                    for dep in it.dependents:
                        if isinstance(dep, RCell) and isinstance(dep.defn, ir.MachineCell):
                            self._machines_to_eval.add(dep)
                        elif id(dep) not in done:
                            heapq.heappush(heap, (dep.rank, next(self._seq), dep))
            transitions = []
            for m in sorted(self._machines_to_eval, key=lambda c: c.defn.name):
                new = self._eval_machine(m)
                if new is not NOMATCH and new != m.value:
                    transitions.append((m, new))
            self._machines_to_eval = set()
            if not transitions:
                break
            rounds += 1
            for m, new in transitions:
                seen = visited.setdefault(id(m), [m.value])
                if new in seen or rounds > MAX_SETTLE_ROUNDS:
                    m.failed = True
                    self.host.log(
                        "error",
                        f"state machine `{m.defn.name}` loops: "
                        + " → ".join([*seen, new])
                        + "; stopped until the rules are reloaded",
                    )
                    continue
                seen.append(new)
                self._transition(m, new)
            if not self._dirty_items and not self._machines_to_eval:
                break
        for s in sorted(pending_sinks.values(), key=lambda s: s.rank):
            self._run_sink(s)
        self._arm_timer()

    def _eval_machine(self, m: RCell) -> Any:
        if m.failed:
            return NOMATCH
        d = m.defn
        assert isinstance(d, ir.MachineCell)
        self._cur_wake = INF
        self._machine_ctx = m
        try:
            r = self._eval_arms(d.arms)
        except Exception as exc:  # noqa: BLE001
            self.host.log("error", f"error evaluating state machine `{d.name}`: {exc!r}")
            r = NOMATCH
        finally:
            self._machine_ctx = None
        m.wake = self._cur_wake
        self._push_wake(m)
        if r is UNKNOWN or r is NOMATCH or not isinstance(r, str) or r not in d.states:
            return NOMATCH
        return r

    def _transition(self, m: RCell, new: str) -> None:
        m.prev = m.value
        m.value = new
        m.known = new
        m.ct = Num.of(self.now)
        m.t_true = m.ct
        for dep in m.dependents:
            if isinstance(dep, RCell) and isinstance(dep.defn, ir.MachineCell):
                self._machines_to_eval.add(dep)
            else:
                self._mark(dep)
        self._machines_to_eval.add(m)
        self._publish(m)

    def _init_machine(self, m: RCell, old: RCell | None) -> None:
        d = m.defn
        assert isinstance(d, ir.MachineCell)
        if old is not None and old.value in d.states:
            m.value, m.known, m.ct, m.prev = old.value, old.known, old.ct, old.prev
        elif old is not None:
            self.host.log(
                "info",
                f'state machine `{d.name}`: state "{old.value}" no longer exists; '
                f'reset to "{d.initial}"',
            )
            m.value = m.known = d.initial
            m.ct = Num.of(self.now)
        else:
            restored = self.host.restore_machine(d.name)
            if restored is not None and restored[0] in d.states:
                m.value = m.known = restored[0]
                m.ct = Num.of(min(restored[1], self.now))
            else:
                m.value = m.known = d.initial
                m.ct = Num.of(self.now)
        m.t_true = m.ct

    def _publish(self, m: RCell) -> None:
        d = m.defn
        assert isinstance(d, ir.MachineCell)
        self.host.publish_machine(d.name, m.value, m.ct.hi, list(d.states))

    # =================================================================== sinks

    def _run_sink(self, s: RSink) -> None:
        self._cur_wake = INF
        try:
            if isinstance(s.defn, ir.BindingDef):
                self._run_binding(s, s.defn)
            else:
                self._run_action(s, s.defn)
        except Exception as exc:  # noqa: BLE001
            self.host.log("error", f"error running {s.defn.text}: {exc!r}")
        s.wake = self._cur_wake
        self._push_wake(s)

    def _run_binding(self, s: RSink, b: ir.BindingDef) -> None:
        v = self._eval(b.expr)
        if v is UNKNOWN:
            return  # leave the device alone; remember the last known result (D16)
        if v is KEEP:
            s.last_known = NOTHING  # device state stands in when keep ends (D5)
            return
        if s.last_known is not NOTHING:
            write = not same_value(v, s.last_known)
        else:
            write = not b.writer.matches(self.host.get_state(b.entity_id), v)
        s.last_known = v
        if write:
            self._write(s, b, v)

    def _write(self, s: RSink, b: ir.BindingDef, v: Any) -> None:
        s.last_written = v
        source = f"{b.file}:{b.span.line}"
        for call in b.writer.calls(b.entity_id, v):
            call = ServiceCall(call.domain, call.service, call.data, call.entity_id, source, b.key)
            self._call(call, s.dry_run)

    def _call(self, call: ServiceCall, dry_run: bool) -> None:
        desc = f"{call.domain}.{call.service}"
        if call.entity_id:
            desc += f" on {call.entity_id}"
        if call.data:
            desc += f" {call.data}"
        if dry_run:
            self.host.log("info", f"[dry run] {call.source}: would call {desc}")
            return
        self.host.log("debug", f"{call.source}: calling {desc}")
        self.host.call_service(call)

    def _run_action(self, s: RSink, a: ir.ActionDef) -> None:
        occ = self.cells[a.trigger.key].occurrence()
        if not (occ.lo > s.last_handled):
            return
        s.last_handled = occ.lo
        if a.guard is not None and to_bool(self._eval(a.guard)) is not True:
            return
        source = f"{a.file}:{a.span.line}"
        for c in a.calls:
            data = {k: self._service_value(v, n.type) for k, n in c.data for v in [self._eval(n)]}
            self._call(
                ServiceCall(c.domain, c.service, data, c.entity_id, source, a.text), s.dry_run
            )

    @staticmethod
    def _service_value(v: Any, t: T) -> Any:
        if v is UNKNOWN:
            return None
        if isinstance(v, Num):
            x = v.lo
            if t == T.TIMESTAMP:
                return datetime.fromtimestamp(x, UTC).isoformat()
            if math.isfinite(x) and x == int(x):
                return int(x)
            return x
        if isinstance(v, tuple):
            return list(v)
        return v

    # ================================================================== timers

    def _push_wake(self, item: Any) -> None:
        if item.wake < INF:
            heapq.heappush(self._wake_heap, (item.wake, next(self._seq), item))

    def _arm_timer(self) -> None:
        while self._wake_heap and self._wake_heap[0][0] != self._wake_heap[0][2].wake:
            heapq.heappop(self._wake_heap)  # stale entry
        nxt = self._wake_heap[0][0] if self._wake_heap else INF
        if nxt == self._timer_at:
            return
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        self._timer_at = nxt
        if nxt < INF and self.started:
            self._timer_cancel = self.host.schedule(nxt, lambda t: self._enqueue(("timer", t)))

    # ================================================================== events

    def on_state_change(self, change: StateChange) -> None:
        self._enqueue(("state", change))

    def set_machine(self, name: str, value: str) -> None:
        """A selection from the machine's `select` entity (D13d)."""
        self._enqueue(("select", (name, value)))

    def _enqueue(self, item: tuple[str, Any]) -> None:
        self._queue.append(item)
        if not self._busy:
            self._drain()

    def _drain(self) -> None:
        self._busy = True
        try:
            while self._queue:
                kind, payload = self._queue.pop(0)
                if kind == "load":
                    self._do_load(payload)
                elif not self.started:
                    continue
                elif kind == "state":
                    self._handle_state(payload)
                elif kind == "bus":
                    self._handle_bus(*payload)
                elif kind == "timer":
                    self._handle_timer(payload)
                elif kind == "select":
                    self._handle_select(*payload)
        finally:
            self._busy = False

    def _handle_state(self, ch: StateChange) -> None:
        self.now = max(self.now, ch.time)
        dirty: list[Any] = list(self.entity_cells.get(ch.entity_id, []))
        manual = self._detect_manual(ch)
        if manual:
            for rc in dirty:
                if isinstance(rc.defn, ir.ManualCell):
                    rc.extra["t"] = Num.of(ch.time)
        sinks = self.bindings_by_entity.get(ch.entity_id, [])
        if ch.new is not None and ch.new.available and (ch.old is None or not ch.old.available):
            # Back from unavailable: a write may have been lost while it was offline, so the
            # device's state stands in for the previous result again (D16), as after a restart.
            for s in sinks:
                s.last_known = NOTHING
        # a device state can matter to a binding with no previous result
        dirty.extend(sinks)
        self._propagate(dirty)

    def _detect_manual(self, ch: StateChange) -> bool:
        """D21: not our context, moves away from what we last wrote, not (un)availability."""
        if ch.ours or ch.old is None or ch.new is None:
            return False
        if not ch.old.available or not ch.new.available:
            return False
        if ch.new.state in TRANSITIONAL_STATES:
            # Progress reports of a slow device (a cover still moving) arrive with a fresh
            # context once HA stops reusing ours; judge the change when it settles.
            return False
        sinks = self.bindings_by_entity.get(ch.entity_id, [])
        if not sinks:
            return ch.old.state != ch.new.state
        # Per binding: manual if the part it drives changed, away from what it last wrote
        # (or it hasn't written anything yet).
        for s in sinks:
            w = s.defn.writer  # type: ignore[union-attr]
            if w.view(ch.old) == w.view(ch.new):
                continue
            if s.last_written is NOTHING or not w.matches(ch.new, s.last_written):
                return True
        return False

    def _handle_bus(self, event_type: str, data: dict[str, Any], t: float) -> None:
        self.now = max(self.now, t)
        dirty = []
        for rc in self.bus_cells.get(event_type, []):
            d = rc.defn
            assert isinstance(d, ir.BusCell)
            if all(data.get(k) == v for k, v in d.filters.items()):
                rc.extra["t"] = Num.of(t)
                dirty.append(rc)
        if dirty:
            self._propagate(dirty)

    def _handle_timer(self, t: float) -> None:
        self._timer_cancel = None
        self._timer_at = INF
        self.now = max(self.now, t)
        due = []
        while self._wake_heap and self._wake_heap[0][0] <= self.now:
            w, _, item = heapq.heappop(self._wake_heap)
            if w == item.wake:
                item.wake = INF
                due.append(item)
        for item in due:
            if isinstance(item, RCell) and isinstance(item.defn, ir.MachineCell):
                self._machines_to_eval.add(item)
        self._propagate(
            [i for i in due if not (isinstance(i, RCell) and isinstance(i.defn, ir.MachineCell))]
        )

    def _handle_select(self, name: str, value: str) -> None:
        self.now = max(self.now, self.host.now())
        m = self.machines.get(name)
        if m is None or value not in m.defn.states:  # type: ignore[attr-defined]
            return
        if value == m.value:
            return
        m.failed = False
        self._transition(m, value)
        self._propagate([])

    # ================================================================ queries

    def value_of(self, key: str) -> Any:
        rc = self.cells.get(key)
        return rc.value if rc else UNKNOWN

    def machine_value(self, name: str) -> str | None:
        m = self.machines.get(name)
        return m.value if m else None

    def binding(self, key: str) -> RSink | None:
        for s in self.sinks:
            if s.key == key:
                return s
        return None
