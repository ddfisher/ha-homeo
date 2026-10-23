"""`homeo.explain` (§19): why a binding, state machine or `let` has its current value.

The explanation is plain data (for a service response): the current result, a one-line
summary, and for each arm its condition's value and the values of its subexpressions. Sugar
appears expanded (`x for 5min` lists `x` and `age(x) >= 5min`), and `let` names are kept.
Nothing here changes the runtime's state.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from . import ir
from .errors import did_you_mean
from .printer import fmt_duration
from .runtime import NOTHING, RSink, Runtime, to_bool
from .types import T
from .values import KEEP, UNKNOWN, DailyTime, Days, Num, OnSpec

MACHINE_ENTITY_PREFIX = "select.homeo_"


class NotFound(LookupError):
    pass


def explain(rt: Runtime, query: str) -> list[dict[str, Any]]:
    """Explain everything `query` names: a binding target (`light.hall`,
    `climate.x.temperature`), an entity with bindings, a state machine (`house` or
    `select.homeo_house`), or a `let` name. Raises NotFound."""
    return _Explainer(rt).explain(query.strip())


class _Explainer:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.tz = rt.host.tz
        self.labels: dict[int, str] = {}
        self.binding_ctx: ir.BindingDef | None = None  # formats arm results like the binding's
        for program in rt.programs.values():
            for name, node in program.lets.items():
                self.labels.setdefault(id(node), name)

    # ----------------------------------------------------------------- lookup

    def explain(self, query: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._evaluating():
            for s in self.rt.sinks:
                d = s.defn
                if isinstance(d, ir.BindingDef) and query in (d.key, d.entity_id):
                    out.append(self.binding(s, d))
            name = query.removeprefix(MACHINE_ENTITY_PREFIX)
            m = self.rt.machines.get(name)
            if m is not None:
                out.append(self.machine(m))
            for file, program in sorted(self.rt.programs.items()):
                node = program.lets.get(query)
                if node is not None:
                    out.append(self.let(query, file, node))
        if not out:
            raise NotFound(self.not_found(query))
        return out

    def not_found(self, query: str) -> str:
        names = {s.defn.key for s in self.rt.sinks if isinstance(s.defn, ir.BindingDef)}
        names |= {MACHINE_ENTITY_PREFIX + m for m in self.rt.machines}
        names |= {n for p in self.rt.programs.values() for n in p.lets}
        msg = f"no binding, state machine or `let` named `{query}`"
        hint = did_you_mean(query, names)
        return f"{msg}; {hint}" if hint else msg

    @contextmanager
    def _evaluating(self) -> Iterator[None]:
        """Evaluate at the current time, without disturbing the runtime's wake-up bookkeeping."""
        rt = self.rt
        saved_now, saved_wake = rt.now, rt._cur_wake
        rt.now = max(rt.now, rt.host.now())
        try:
            yield
        finally:
            rt.now, rt._cur_wake = saved_now, saved_wake

    # ------------------------------------------------------------- statements

    def binding(self, s: RSink, b: ir.BindingDef) -> dict[str, Any]:
        result = self.eval(b.expr)
        state = self.rt.host.get_state(b.entity_id)
        out: dict[str, Any] = {
            "binding": b.key,
            "rule": f"{b.file}:{b.span.line}",
            "result": self.fmt_result(result, b),
        }
        self.binding_ctx = b
        try:
            trace, chosen = self.body(b.expr)
        finally:
            self.binding_ctx = None
        out["summary"] = self.binding_summary(b, result, chosen)
        out["device"] = self.device(b, state)
        if result is not UNKNOWN and result is not KEEP:
            out["device_matches"] = bool(b.writer.matches(state, result))
        out["last_written"] = (
            "nothing since homeo started"
            if s.last_written is NOTHING
            else self.fmt_result(s.last_written, b)
        )
        if s.dry_run:
            out["dry_run"] = True
        out.update(trace)
        return out

    def machine(self, m: Any) -> dict[str, Any]:
        d: ir.MachineCell = m.defn
        trace, chosen = self.arms_trace(d.arms, cascade=True)
        out: dict[str, Any] = {
            "machine": d.name,
            "entity": MACHINE_ENTITY_PREFIX + d.name,
            "rule": f"{d.file}:{d.span.line}" if d.span else d.file,
            "state": m.value,
            "since": self.fmt_time(m.ct),
        }
        if m.prev is not UNKNOWN:
            out["previous"] = m.prev
        if m.failed:
            out["stopped"] = "it looped; it restarts when the rules are reloaded"
        nxt = self.eval(chosen.result) if isinstance(chosen, ir.ArmIR) else None
        summary = f'{d.name} is "{m.value}" since {self.fmt_time(m.ct)}'
        if isinstance(chosen, ir.ArmIR) and nxt == m.value:
            summary += f"; `{self.cond_text(chosen)}` keeps it there"
        elif isinstance(chosen, ir.ArmIR) and isinstance(nxt, str):
            summary += f'; about to change to "{nxt}" (`{self.cond_text(chosen)}`)'
        else:
            summary += "; no arm applies now"
        out["summary"] = summary
        out["arms"] = trace
        return out

    def let(self, name: str, file: str, node: ir.Node) -> dict[str, Any]:
        value = self.eval(node)
        out: dict[str, Any] = {
            "let": name,
            "file": file,
            "value": self.fmt(value, node.type),
        }
        if not isinstance(node, (ir.Cascade, ir.Latest)):
            out["definition"] = node.text
        trace, _ = self.body(node)
        out.update(trace)
        return out

    def body(self, n: ir.Node) -> tuple[dict[str, Any], Any]:
        """The trace of a binding's or let's expression: arms for blocks, else values."""
        if isinstance(n, ir.Cascade):
            trace, chosen = self.arms_trace(n.arms, cascade=True)
            return {"arms": trace}, chosen
        if isinstance(n, ir.Latest):
            trace, chosen = self.arms_trace(n.arms, cascade=False)
            return {"latest": trace}, chosen
        values = self.values(n, include_self=False)
        return ({"values": values} if values else {}), None

    def binding_summary(self, b: ir.BindingDef, result: Any, chosen: Any) -> str:
        why = ""
        if isinstance(chosen, ir.ArmIR):
            line = chosen.span.line
            why = (
                f", because no other arm applies (`else`, line {line})"
                if chosen.cond is None
                else f", because `{self.cond_text(chosen)}` (line {line})"
            )
        elif isinstance(chosen, ir.GroupIR):
            why = f", because `{chosen.source}` (line {chosen.span.line})"
        if result is UNKNOWN:
            return (
                f"{b.key} is left as it is: the result is unknown "
                "(a sensor is unavailable, or a time is only known to be before the restart)"
            )
        if result is KEEP:
            return f"{b.key} is left as it is (`keep`){why}"
        return f"{b.key} should be {self.fmt_result(result, b)}{why}"

    def fmt_result(self, v: Any, b: ir.BindingDef) -> Any:
        """A binding's result as the device would show it: `on`/`off`, `on(brightness: 50%)`."""
        if v is True or v is False:
            return "on" if v else "off"
        if isinstance(v, OnSpec):
            params = b.writer.on_params
            inner = ", ".join(
                f"{k}: {self.fmt_plain(x, params[k].type if k in params else T.NUMBER)}"
                for k, x in v.params
            )
            return f"on({inner})"
        if isinstance(v, str):
            return v
        return self.fmt(v, b.expr.type)

    def device(self, b: ir.BindingDef, state: Any) -> str:
        if state is None:
            return "doesn't exist"
        attrs = [a for a in sorted(b.writer.read_attrs, key=str) if a is not None]
        details = [
            f"{a}: {state.attributes[a]}" for a in attrs if state.attributes.get(a) is not None
        ]
        return str(state.state) + (f" ({', '.join(details)})" if details else "")

    # ------------------------------------------------------------------- arms

    def arms_trace(
        self, arms: list[ir.ArmIR | ir.GroupIR], cascade: bool
    ) -> tuple[list[dict[str, Any]], Any]:
        """Each arm with its condition's value; marks the one that decides the result."""
        if cascade:
            chosen = self.first_match(arms)
        else:
            chosen, _ = self.rt.latest_choice(arms)
        return [self.arm(a, chosen, cascade) for a in arms], chosen

    def first_match(self, arms: list[ir.ArmIR | ir.GroupIR]) -> Any:
        """The arm a cascade takes, as `Runtime._eval_arms` does (a sub-arm for groups)."""
        for a in arms:
            if isinstance(a, ir.GroupIR):
                if self.truth(a.guard) is True:
                    sub = self.first_match(a.arms)
                    if sub is not None:
                        return sub
                continue
            if a.cond is None or self.truth(a.cond) is True:
                return a
        return None

    def arm(self, a: ir.ArmIR | ir.GroupIR, chosen: Any, cascade: bool) -> dict[str, Any]:
        if isinstance(a, ir.GroupIR):
            out: dict[str, Any] = {"guard": a.source or a.text, "line": a.span.line}
            out.update(self.condition(a.guard, cascade))
            if self.contains(a, chosen):
                out["active"] = True
            out["arms"] = [self.arm(s, chosen, True) for s in a.arms]
            return out
        out = {"arm": a.source or a.text, "line": a.span.line}
        if a.cond is not None:
            out.update(self.condition(a.cond, cascade))
        if a is chosen:
            out["active"] = True
        if not isinstance(a.result, ir.Const):
            v = self.eval(a.result)
            b = self.binding_ctx
            out["result"] = self.fmt_result(v, b) if b else self.fmt(v, a.result.type)
            values = self.values(a.result, include_self=False)
            if values:
                out.setdefault("values", {}).update(values)
        return out

    def condition(self, cond: ir.Node, cascade: bool) -> dict[str, Any]:
        if not cascade:  # latest: when the condition last happened
            assert isinstance(cond, ir.Read)
            occ = self.rt.cells[cond.cell.key].occurrence()
            out: dict[str, Any] = {"happened": self.fmt_time(occ)}
            values = self.cell_values(cond.cell, include_self=False)
        else:
            out = {"condition": self.fmt(self.eval(cond), T.BOOL)}
            values = self.values(cond, include_self=False)
        if values:
            out["values"] = values
        return out

    def contains(self, g: ir.GroupIR, chosen: Any) -> bool:
        return any(
            s is chosen or (isinstance(s, ir.GroupIR) and self.contains(s, chosen)) for s in g.arms
        )

    def cond_text(self, a: ir.ArmIR) -> str:
        if a.cond is None:
            return "else"
        return a.cond_source if a.source else a.cond.text

    # ----------------------------------------------------------------- values

    def values(self, n: ir.Node, include_self: bool = True) -> dict[str, Any]:
        """Every non-constant subexpression of `n` with its current value, outermost first."""
        out: dict[str, Any] = {}
        seen: set[int] = set()
        self._visit(n, include_self, out, seen)
        return out

    def cell_values(self, c: ir.CellDef, include_self: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self._visit_cell(c, include_self, out, set())
        return out

    def _visit(self, n: ir.Node, include: bool, out: dict[str, Any], seen: set[int]) -> None:
        if id(n) in seen or isinstance(n, ir.Const):
            return
        seen.add(id(n))
        if include:
            out.setdefault(self.label(n), self.fmt(self.eval(n), n.type))
        match n:
            case ir.Read(cell=c):
                self._visit_cell(c, False, out, seen)
            case ir.Age(cell=c) | ir.Previous(cell=c) | ir.Available(cell=c) | ir.Fresh(cell=c):
                self._visit_cell(c, True, out, seen)
        for ch in ir.children(n):
            self._visit(ch, True, out, seen)

    def _visit_cell(
        self, c: ir.CellDef, include: bool, out: dict[str, Any], seen: set[int]
    ) -> None:
        if id(c) in seen:
            return
        seen.add(id(c))
        rc = self.rt.cells.get(c.key)
        if include and rc is not None:
            out.setdefault(c.text, self.fmt(rc.value, c.type))
        match c:
            case ir.ExprCell(expr=e):
                seen.add(id(e))  # the cell's own value is already listed
                for ch in ir.children(e):
                    self._visit(ch, True, out, seen)
                if isinstance(e, ir.Read):
                    self._visit_cell(e.cell, False, out, seen)
            case ir.ToggleCell(source=src) | ir.CountCell(source=src):
                self._visit_cell(src, True, out, seen)

    def label(self, n: ir.Node) -> str:
        name = self.labels.get(id(n))
        if name is None or name == n.text:
            return n.text
        return f"{name} ({n.text})"

    # ------------------------------------------------------------- evaluation

    def eval(self, n: ir.Node) -> Any:
        try:
            return self.rt._eval(n)
        except Exception:  # noqa: BLE001 - an explanation must not fail on one bad node
            return UNKNOWN

    def truth(self, n: ir.Node) -> Any:
        return to_bool(self.eval(n))

    # ------------------------------------------------------------- formatting

    def fmt(self, v: Any, t: T) -> Any:
        """A JSON-friendly value: bools and plain numbers as such, everything else as text."""
        if v is UNKNOWN:
            return "unknown"
        if v is True or v is False:
            return v
        if isinstance(v, Num):
            if t == T.DURATION:
                return self.fmt_duration(v)
            if t == T.TIMESTAMP:
                return self.fmt_time(v)
            if v.lo == v.hi and math.isfinite(v.lo):
                x = round(v.lo, 6)
                x = int(x) if x == int(x) else x
                return f"{x}%" if t == T.PERCENT else x
            return f"between {v.lo:g} and {v.hi:g}"
        if isinstance(v, str):
            return v
        return self.fmt_plain(v, t)

    def fmt_plain(self, v: Any, t: T) -> str:
        if v is True:
            return "on" if t == T.ONSPEC else "true"
        if v is False:
            return "off" if t == T.ONSPEC else "false"
        if v is KEEP:
            return "keep"
        if v is UNKNOWN:
            return "unknown"
        if isinstance(v, Num):
            f = self.fmt(v, t)
            return f if isinstance(f, str) else f"{f:g}" if isinstance(f, float) else str(f)
        if isinstance(v, OnSpec):
            inner = ", ".join(f"{k}: {self.fmt_plain(x, T.NUMBER)}" for k, x in v.params)
            return f"on({inner})"
        if isinstance(v, str):
            return f'"{v}"' if t != T.ANY else v
        if isinstance(v, (DailyTime, Days)):
            return repr(v)
        if isinstance(v, tuple):
            return "[" + ", ".join(self.fmt_plain(x, T.ANY) for x in v) + "]"
        return str(v)

    def fmt_duration(self, v: Num) -> str:
        lo, hi = v.lo, v.hi
        if lo == math.inf:
            return "never happened"
        if lo == hi:
            return ("-" if lo < 0 else "") + fmt_duration(abs(round(lo)))
        if hi == math.inf:
            return f"at least {fmt_duration(max(0, round(lo)))}"
        if lo == -math.inf:
            return f"at most {fmt_duration(max(0, round(hi)))}"
        return f"between {fmt_duration(max(0, round(lo)))} and {fmt_duration(max(0, round(hi)))}"

    def fmt_time(self, v: Num) -> str:
        """A point in time, with how long ago it was."""
        lo, hi = v.lo, v.hi
        if hi == -math.inf:
            return "never"
        if lo == hi:
            return self.instant(lo)
        if lo == -math.inf:
            return f"never, or at or before {self.instant(hi)}"
        return f"between {self.instant(lo)} and {self.instant(hi)}"

    def instant(self, t: float) -> str:
        now = self.rt.now
        local = datetime.fromtimestamp(t, self.tz)
        today = datetime.fromtimestamp(now, self.tz).date()
        text = (
            local.strftime("%H:%M:%S")
            if local.date() == today
            else local.strftime("%Y-%m-%d %H:%M:%S")
        )
        delta = now - t
        if delta >= 0.5:
            text += f" ({fmt_duration(round(delta))} ago)"
        elif delta <= -0.5:
            text += f" (in {fmt_duration(round(-delta))})"
        else:
            text += " (now)"
        return text
