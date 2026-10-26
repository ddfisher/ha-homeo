"""Name resolution and type checking: AST → Program (IR).

Implements the compile-time rules of the design: names (D2, I11), attributes (D3), types and
hints (§3, I14–I16), writers and targets (D5), one writer per target across files, required
`else` in bindings (D6), `fresh` only in state machines (D20), state names checked (D13c),
service calls on bound entities (D14), cycles (I17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import ast as A
from . import ir
from .errors import DiagnosticSink, Span, did_you_mean
from .host import EntityCatalog, EntityState
from .printer import fmt_duration, to_source
from .sun import SUN_ENTITY
from .types import NUMERIC, T, article, assignable, join
from .values import DAY_NAMES, KEEP, UNKNOWN, DailyTime, Days, Num, parse_timestamp
from .writers import WHOLE_ONLY, WRITERS, Writer

KNOWN_DOMAINS = frozenset(
    """
    alarm_control_panel assist_satellite automation binary_sensor button calendar camera climate
    conversation counter cover date datetime device_tracker event fan group humidifier image
    input_boolean input_button input_datetime input_number input_select input_text lawn_mower
    light lock media_player notify number person remote scene schedule script select sensor siren
    stt sun switch text time timer todo tts update vacuum valve wake_word water_heater weather
    zone
    """.split()
)

BOOL_DOMAINS = frozenset(
    """
    automation binary_sensor calendar fan humidifier input_boolean light remote schedule script
    siren switch update
    """.split()
)
TIMESTAMP_DOMAINS = frozenset("event input_button button scene".split())
NUMBER_DOMAINS = frozenset("input_number number counter".split())

# Attributes that may be absent from the current state (e.g. a light that's off).
KNOWN_ATTRS: dict[tuple[str, str], T] = {
    ("light", "brightness"): T.NUMBER,
    ("light", "color_temp_kelvin"): T.NUMBER,
    ("fan", "percentage"): T.NUMBER,
    ("climate", "temperature"): T.NUMBER,
    ("climate", "current_temperature"): T.NUMBER,
    ("climate", "target_temp_high"): T.NUMBER,
    ("climate", "target_temp_low"): T.NUMBER,
    ("climate", "current_humidity"): T.NUMBER,
    ("climate", "hvac_action"): T.STRING,
    ("cover", "current_position"): T.NUMBER,
    ("cover", "current_tilt_position"): T.NUMBER,
    ("media_player", "volume_level"): T.NUMBER,
}

BUILTIN_FUNCS = ("age", "fresh", "previous", "toggle", "count", "manual", "available")
SOLAR = ("sunrise", "sunset", "dawn", "dusk")
DAY_CONSTS = {name: frozenset({i}) for i, name in enumerate(DAY_NAMES)} | {
    "weekday": frozenset(range(5)),
    "weekend": frozenset({5, 6}),
}
BUILTIN_NAMES = ("now", "today", *SOLAR, *DAY_CONSTS)
MIDNIGHT = DailyTime(0)


@dataclass
class Globals:
    """What other loaded files define, for cross-file rules."""

    targets: dict[str, str] = field(default_factory=dict)  # target key -> file
    bound_entities: dict[str, str] = field(default_factory=dict)  # entity id -> file
    action_entities: dict[str, str] = field(default_factory=dict)  # entity id -> file
    machines: dict[str, str] = field(default_factory=dict)  # name -> file
    # target key -> {(entity id, attr)} it reads, for cycles through several bindings
    binding_reads: dict[str, set[tuple[str, str | None]]] = field(default_factory=dict)

    def without_file(self, file: str) -> Globals:
        return Globals(
            {k: f for k, f in self.targets.items() if f != file},
            {k: f for k, f in self.bound_entities.items() if f != file},
            {k: f for k, f in self.action_entities.items() if f != file},
            {k: f for k, f in self.machines.items() if f != file},
            {k: r for k, r in self.binding_reads.items() if self.targets.get(k) != file},
        )

    def conflicts(self, program: ir.Program) -> list[str]:
        """What a (previously compiled) program defines that is already defined here."""
        out = [
            f"`{b.key}` is bound in {self.targets[b.key]}"
            for b in program.bindings
            if b.key in self.targets
        ]
        out += [
            f"state machine `{m.name}` is defined in {self.machines[m.name]}"
            for m in program.machines
            if m.name in self.machines
        ]
        return out

    def add(self, program: ir.Program, reads: dict[str, set[tuple[str, str | None]]]) -> None:
        for b in program.bindings:
            self.targets[b.key] = program.file
            self.bound_entities[b.entity_id] = program.file
        for a in program.actions:
            for c in a.calls:
                for eid in c.targets():
                    self.action_entities[eid] = program.file
        for m in program.machines:
            self.machines[m.name] = program.file
        self.binding_reads.update(reads)


@dataclass
class _Ctx:
    machine: ir.MachineCell | None = None  # inside this state machine's arms
    where: str = "expression"  # "binding", "let", "state", "action"


def infer_entity_type(entity_id: str, state: EntityState | None) -> T:
    """I14."""
    domain = entity_id.split(".", 1)[0]
    if domain in BOOL_DOMAINS:
        return T.BOOL
    if domain in TIMESTAMP_DOMAINS:
        return T.TIMESTAMP
    if domain in NUMBER_DOMAINS:
        return T.NUMBER
    if (
        (domain == "group" or domain not in KNOWN_DOMAINS)
        and state is not None
        and state.state in ("on", "off")
    ):
        # groups of on/off entities, and on/off entities of domains we don't know (custom
        # integrations). Known multi-state domains (media_player, climate, ...) can also be
        # "off", so they stay strings.
        return T.BOOL
    if domain == "sensor" and state is not None:
        attrs = state.attributes
        if attrs.get("device_class") == "timestamp":
            return T.TIMESTAMP
        if attrs.get("unit_of_measurement") is not None or attrs.get("state_class") is not None:
            return T.NUMBER
        try:
            float(state.state)
            return T.NUMBER
        except ValueError:
            pass
        if state.state in ("unknown", "unavailable"):
            return T.ANY
        return T.STRING
    if domain == "sensor":
        return T.ANY
    return T.STRING


def infer_attr_type(entity_id: str, attr: str, state: EntityState | None) -> T | None:
    domain = entity_id.split(".", 1)[0]
    if (domain, attr) in KNOWN_ATTRS:
        return KNOWN_ATTRS[(domain, attr)]
    if state is None or attr not in state.attributes:
        return None
    v = state.attributes[attr]
    if isinstance(v, bool):
        return T.BOOL
    if isinstance(v, (int, float)):
        return T.NUMBER
    if isinstance(v, str):
        return T.TIMESTAMP if parse_timestamp(v) is not None else T.STRING
    if isinstance(v, datetime):
        return T.TIMESTAMP
    if isinstance(v, (list, tuple)):
        return T.LIST
    return T.ANY


def _err_node(span: Span, text: str = "<error>") -> ir.Node:
    return ir.Const(T.ANY, span, text, False, UNKNOWN)


class _Fail(Exception):
    pass


class Checker:
    def __init__(
        self,
        file: A.File,
        catalog: EntityCatalog,
        globals_: Globals | None = None,
    ):
        self.file = file
        self.catalog = catalog
        self.globals = (globals_ or Globals()).without_file(file.name)
        self.sink = DiagnosticSink(file.name)
        self.program = ir.Program(file.name)
        self.lets: dict[str, A.Let] = {}
        self.let_values: dict[str, ir.Node] = {}
        self.resolving: list[str] = []
        self.machines: dict[str, ir.MachineCell] = {}
        self.machine_decls: dict[str, A.StateDecl] = {}
        self.domains = set(KNOWN_DOMAINS) | set(catalog.domains())
        self.binding_reads: dict[str, set[tuple[str, str | None]]] = {}
        # the same, also following state machines' arms (for the loop warning)
        self.binding_reads_via_machines: dict[str, set[tuple[str, str | None]]] = {}
        self.binding_machines: dict[str, set[str]] = {}  # target key -> machines it reads
        self._on_spans: dict[int, dict[str, Span]] = {}  # setting name spans of on(...)

    # ---------------------------------------------------------------- driver

    def check(self) -> ir.Program:
        mode = self.file.directives.get("mode")
        self.program.dry_run = bool(mode and mode[0] == "dry_run")
        self.collect_names()
        for stmt in self.file.statements:
            try:
                match stmt:
                    case A.Let():
                        self.let_value(stmt.name)
                    case A.Bind():
                        self.check_bind(stmt)
                    case A.StateDecl():
                        self.check_state(stmt)
                    case A.Action():
                        self.check_action(stmt)
            except _Fail:
                pass
        self.check_action_targets()
        self.check_binding_cycles()
        self.program.lets = dict(self.let_values)
        self.program.warnings = self.sink.warnings
        self.sink.raise_if_errors()
        return self.program

    def error(self, message: str, span: Span, hint: str | None = None) -> None:
        self.sink.error(message, span, hint)

    def fail(self, message: str, span: Span, hint: str | None = None):
        self.sink.error(message, span, hint)
        raise _Fail

    def cell(self, cell: ir.CellDef) -> ir.CellDef:
        """Get-or-create a cell by key."""
        existing = self.program.cells.get(cell.key)
        if existing is not None:
            return existing
        self.program.cells[cell.key] = cell
        return cell

    # ------------------------------------------------------------------ names

    def collect_names(self) -> None:
        for stmt in self.file.statements:
            if isinstance(stmt, A.Let):
                if stmt.name in self.lets or stmt.name in self.machine_decls:
                    self.error(f"`{stmt.name}` is already defined", stmt.name_span)
                    continue
                self.check_name_shadowing(stmt.name, stmt.name_span)
                self.lets[stmt.name] = stmt
            elif isinstance(stmt, A.StateDecl):
                if stmt.name in self.lets or stmt.name in self.machine_decls:
                    self.error(f"`{stmt.name}` is already defined", stmt.name_span)
                    continue
                if stmt.name in self.globals.machines:
                    self.error(
                        f"a state machine `{stmt.name}` is already defined in "
                        f"{self.globals.machines[stmt.name]}",
                        stmt.name_span,
                        "state machine names must be unique across files (they become "
                        f"`select.homeo_{stmt.name}`)",
                    )
                self.check_name_shadowing(stmt.name, stmt.name_span)
                self.machine_decls[stmt.name] = stmt
                self.machines[stmt.name] = self.make_machine(stmt)

    def check_name_shadowing(self, name: str, span: Span) -> None:
        if name in BUILTIN_NAMES or name in BUILTIN_FUNCS:
            self.sink.warning(f"`{name}` shadows a builtin", span)

    def make_machine(self, stmt: A.StateDecl) -> ir.MachineCell:
        states: list[str] = []
        if isinstance(stmt.initial, A.StringLit):
            states.append(stmt.initial.value)
        else:
            self.error(
                "a state machine's initial value must be a string",
                stmt.initial.span,
                'e.g. `state mode = "auto":`',
            )

        def collect(arms: list[A.Arm | A.Group]) -> None:
            for a in arms:
                if isinstance(a, A.Group):
                    collect(a.arms)
                elif isinstance(a.result, A.StringLit) and a.result.value not in states:
                    states.append(a.result.value)

        collect(stmt.arms)
        initial = states[0] if states else ""
        cell = ir.MachineCell(
            key=f"machine:{stmt.name}",
            type=T.STRING,
            text=stmt.name,
            span=stmt.name_span,
            name=stmt.name,
            file=self.file.name,
            initial=initial,
            states=states,
        )
        self.program.cells[cell.key] = cell
        return cell

    def let_value(self, name: str) -> ir.Node:
        if name in self.let_values:
            return self.let_values[name]
        if name in self.resolving:
            cycle = " → ".join(self.resolving[self.resolving.index(name) :] + [name])
            self.fail(f"definition cycle: {cycle}", self.lets[name].name_span)
        stmt = self.lets[name]
        self.resolving.append(name)
        try:
            node = self.resolve(stmt.value, _Ctx(where="let"))
        except _Fail:
            node = _err_node(stmt.value.span, name)
        finally:
            self.resolving.pop()
        self.let_values[name] = node
        return node

    # ---------------------------------------------------------------- entities

    def entity_state(self, entity_id: str) -> EntityState | None:
        return self.catalog.get_state(entity_id)

    def require_entity(self, entity_id: str, span: Span) -> None:
        if self.catalog.entity_exists(entity_id):
            return
        domain = entity_id.split(".", 1)[0]
        hint = did_you_mean(entity_id, self.catalog.entity_ids(domain))
        self.fail(f"unknown entity `{entity_id}`", span, hint)

    def entity_read(self, entity_id: str, span: Span) -> ir.Node:
        self.require_entity(entity_id, span)
        t = infer_entity_type(entity_id, self.entity_state(entity_id))
        cell = self.cell(
            ir.EntityCell(f"state:{entity_id}", t, entity_id, span, entity_id=entity_id)
        )
        return ir.Read(cell.type, span, entity_id, False, cell)

    def attr_read(self, entity_id: str, attr: str, quoted: bool, span: Span) -> ir.Node:
        domain = entity_id.split(".", 1)[0]
        state = self.entity_state(entity_id)
        if domain == "event" and not quoted:
            types = (state.attributes.get("event_types") if state else None) or []
            if types and attr not in types:
                self.fail(
                    f"`{entity_id}` has no event type `{attr}`",
                    span,
                    did_you_mean(attr, list(types))
                    or "event types: " + ", ".join(f"`{t}`" for t in types),
                )
            text = f"{entity_id}.{attr}"
            cell = self.cell(
                ir.EventTypeCell(
                    f"event:{text}", T.TIMESTAMP, text, span, entity_id=entity_id, event_type=attr
                )
            )
            return ir.Read(T.TIMESTAMP, span, text, False, cell)
        t = infer_attr_type(entity_id, attr, state)
        if t is None:
            hint = None
            if state is not None:
                hint = did_you_mean(attr, list(state.attributes)) or (
                    "current attributes: " + ", ".join(f"`{a}`" for a in sorted(state.attributes))
                    if state.attributes
                    else None
                )
            self.sink.warning(f"`{entity_id}` has no attribute `{attr}` right now", span, hint)
            t = T.ANY
        text = (
            f'{entity_id}["{attr}"]' if quoted or not attr.isidentifier() else f"{entity_id}.{attr}"
        )
        cell = self.cell(
            ir.EntityCell(f"attr:{entity_id}.{attr}", t, text, span, entity_id=entity_id, attr=attr)
        )
        return ir.Read(t, span, text, False, cell)

    def sun_cell(self, span: Span, *times: DailyTime) -> ir.CellDef | None:
        """The `sun.sun` cell, if any of `times` is solar (D4: solar times come from it)."""
        solar = [t.solar for t in times if t.solar is not None]
        if not solar:
            return None
        if not self.catalog.entity_exists(SUN_ENTITY):
            self.fail(
                f"`{solar[0]}` needs the `{SUN_ENTITY}` entity, which doesn't exist",
                span,
                "enable the Sun integration (Settings → Devices & services)",
            )
        return self.cell(ir.SunCell("sun", T.ANY, SUN_ENTITY, span, entity_id=SUN_ENTITY))

    # ------------------------------------------------------------------- paths

    @staticmethod
    def flatten(e: A.Expr) -> tuple[A.Name, list[A.Attr]] | None:
        segs: list[A.Attr] = []
        while isinstance(e, A.Attr):
            segs.append(e)
            e = e.base
        if not isinstance(e, A.Name):
            return None
        segs.reverse()
        return e, segs

    def resolve_path(self, e: A.Expr, ctx: _Ctx) -> ir.Node:
        flat = self.flatten(e)
        if flat is None:
            self.fail("expected a name", e.span)
        head, segs = flat
        name = head.id
        if name in self.lets:
            node = self.let_value(name)
            return self.apply_segments(node, segs, name, e.span)
        if name in self.machines:
            if segs:
                self.fail(f"state machine `{name}` has no attributes", segs[0].span)
            cell = self.machines[name]
            return ir.Read(T.STRING, e.span, name, False, cell)
        if not segs and name in BUILTIN_NAMES:
            return self.builtin_name(name, head.span)
        if name in self.domains and segs:
            first = segs[0]
            if first.quoted:
                self.fail("entity ids can't be quoted", first.span)
            entity_id = f"{name}.{first.name}"
            node = self.entity_read(entity_id, head.span.to(first.span))
            return self.apply_segments(node, segs[1:], entity_id, e.span)
        if name == "bus":
            self.fail(
                "`bus.<event>` needs filters in parentheses",
                e.span,
                'e.g. `bus.zha_event(command: "on")`',
            )
        if name in self.domains:
            self.fail(
                f"`{name}` is a domain; expected an entity id like `{name}.something`", head.span
            )
        candidates = list(self.lets) + list(self.machines) + list(BUILTIN_NAMES)
        self.fail(f"unknown name `{name}`", head.span, did_you_mean(name, candidates))
        raise AssertionError

    def apply_segments(self, node: ir.Node, segs: list[A.Attr], what: str, span: Span) -> ir.Node:
        if not segs:
            return node
        cell = node.cell if isinstance(node, ir.Read) else None
        if not isinstance(cell, ir.EntityCell) or cell.attr is not None:
            self.fail(
                f"`{what}` isn't an entity, so it has no attribute `{segs[0].name}`", segs[0].span
            )
        if len(segs) > 1:
            self.fail("too many `.` segments", segs[1].span)
        return self.attr_read(cell.entity_id, segs[0].name, segs[0].quoted, span)

    def builtin_name(self, name: str, span: Span) -> ir.Node:
        if name == "now":
            return ir.Now(T.TIMESTAMP, span, "now", True)
        if name == "today":
            return ir.Today(T.DAY, span, "today")
        if name in SOLAR:
            return ir.Const(T.TIMEOFDAY, span, name, False, DailyTime(solar=name))
        return ir.Const(T.DAY, span, name, False, Days(DAY_CONSTS[name]))

    # ----------------------------------------------------------- expressions

    def resolve(self, e: A.Expr, ctx: _Ctx) -> ir.Node:
        match e:
            case A.BoolLit(value=v, word=w):
                return ir.Const(T.BOOL, e.span, w, False, v)
            case A.NumberLit(value=v, percent=p):
                return ir.Const(
                    T.PERCENT if p else T.NUMBER, e.span, to_source(e), False, Num.of(v)
                )
            case A.DurationLit(seconds=s):
                return ir.Const(T.DURATION, e.span, to_source(e), False, Num.of(s))
            case A.TimeLit(seconds=s):
                return ir.Const(T.TIMEOFDAY, e.span, to_source(e), False, DailyTime(s))
            case A.StringLit(value=v):
                return ir.Const(T.STRING, e.span, to_source(e), False, v)
            case A.KeepLit():
                return ir.Const(T.KEEP, e.span, "keep", False, KEEP)
            case A.Name() | A.Attr():
                return self.resolve_path(e, ctx)
            case A.Unary(op="not", operand=o):
                x = self.want_bool(self.resolve(o, ctx), "`not`")
                return ir.Not(T.BOOL, e.span, f"not {x.text}", False, x)
            case A.Unary(op="-", operand=o):
                x = self.resolve(o, ctx)
                if x.type not in (T.NUMBER, T.PERCENT, T.DURATION, T.ANY):
                    self.fail(f"can't negate {article(x.type)}", e.span)
                if isinstance(x, ir.Const):
                    return ir.Const(x.type, e.span, f"-{x.text}", False, -x.value)
                return ir.Neg(x.type, e.span, f"-{x.text}", x.moving, x)
            case A.Binary(op="and" | "or" as op, left=l, right=r):
                a = self.want_bool(self.resolve(l, ctx), f"`{op}`")
                b = self.want_bool(self.resolve(r, ctx), f"`{op}`")
                return ir.BoolOp(
                    T.BOOL, e.span, f"{self.paren(a)} {op} {self.paren(b)}", False, op, a, b
                )
            case A.Binary(op="+" | "-" | "*" | "/" as op, left=l, right=r):
                return self.arith(op, self.resolve(l, ctx), self.resolve(r, ctx), e.span)
            case A.Binary(op="??", left=l, right=r):
                a, b = self.resolve(l, ctx), self.resolve(r, ctx)
                t = join(a.type, b.type)
                if t is None:
                    self.fail(
                        f"`??` needs both sides of the same type ({a.type} vs {b.type})", e.span
                    )
                return ir.Coalesce(t, e.span, f"{a.text} ?? {b.text}", a.moving or b.moving, a, b)
            case A.Binary(op="in" | "not in" as op, left=l, right=r):
                return self.membership(op == "not in", l, r, e.span, ctx)
            case A.Binary(op=op, left=l, right=r):
                return self.compare(op, self.resolve(l, ctx), self.resolve(r, ctx), e.span)
            case A.Range():
                self.fail("a range `a..b` can only follow `in`", e.span)
            case A.ListExpr():
                self.fail("a list can only follow `in`", e.span, 'e.g. `x in ["a", "b"]`')
            case A.Ternary(then=t, cond=c, otherwise=o):
                cond = self.want_bool(self.resolve(c, ctx), "`if`")
                a, b = self.resolve(t, ctx), self.resolve(o, ctx)
                ty = join(a.type, b.type)
                if ty is None:
                    self.fail(
                        f"both branches must have the same type ({a.type} vs {b.type})", e.span
                    )
                return ir.IfElse(
                    ty,
                    e.span,
                    f"{a.text} if {cond.text} else {b.text}",
                    a.moving or b.moving,
                    cond,
                    a,
                    b,
                )
            case A.For(operand=o, duration=d) | A.Within(operand=o, duration=d):
                # D11: `x for D` = `x and age(x) >= D`; `x within D` = `x or age(x) < D`
                within = isinstance(e, A.Within)
                word = "within" if within else "for"
                x = self.want_bool(
                    self.resolve(o, ctx), f"`{word}`", self.sugar_timestamp_hint(o, d, within)
                )
                dur = self.const_duration(self.resolve(d, ctx), f"`{word}`")
                cell = self.history_cell(x, f"`{word}`")
                read = ir.Read(T.BOOL, o.span, cell.text, False, cell)
                age = ir.Age(T.DURATION, e.span, f"age({cell.text})", True, cell)
                op = "<" if within else ">="
                cmp = ir.Compare(
                    T.BOOL, e.span, f"age({cell.text}) {op} {dur.text}", False, op, age, dur
                )
                return ir.BoolOp(
                    T.BOOL,
                    e.span,
                    f"{self.paren(x)} {word} {dur.text}",
                    False,
                    "or" if within else "and",
                    read,
                    cmp,
                )
            case A.Every(period=p):
                dur = self.const_duration(self.resolve(p, ctx), "`every`")
                if dur.value.lo <= 0:
                    self.fail("`every` needs a positive duration", p.span)
                if 86400 % dur.value.lo != 0:
                    self.sink.warning(
                        f"`every {dur.text}` doesn't divide a day, so ticks aren't aligned to midnight",
                        p.span,
                    )
                text = f"every {dur.text}"
                cell = self.cell(
                    ir.EveryCell(
                        f"every:{dur.value.lo:g}", T.TIMESTAMP, text, e.span, period=dur.value.lo
                    )
                )
                return ir.Read(T.TIMESTAMP, e.span, text, False, cell)
            case A.Call():
                return self.call(e, ctx)
            case A.OnCall(kwargs=kwargs):
                params = []
                for k in kwargs:
                    params.append((k.name, self.resolve(k.value, ctx)))
                text = "on(" + ", ".join(f"{n}: {v.text}" for n, v in params) + ")"
                moving = any(v.moving for _, v in params)  # a behavior (D23)
                node = ir.OnValue(T.ONSPEC, e.span, text, moving, params)
                self._on_spans[id(node)] = {k.name: k.span for k in kwargs}
                return node
            case A.Cascade(arms=arms):
                return self.cascade(arms, e.span, ctx)
            case A.Latest(arms=arms):
                return self.latest(arms, e.span, ctx)
        self.fail(f"unsupported expression {type(e).__name__}", e.span)
        raise AssertionError

    @staticmethod
    def paren(n: ir.Node) -> str:
        if isinstance(n, (ir.BoolOp, ir.IfElse)):
            return f"({n.text})"
        return n.text

    def want_bool(self, n: ir.Node, what: str, timestamp_hint: str | None = None) -> ir.Node:
        if n.type in (T.BOOL, T.ANY):
            return n
        if n.type == T.TIMESTAMP:
            self.fail(
                f"`{n.text}` is a timestamp, but {what} needs a condition",
                n.span,
                timestamp_hint or f"did you mean `age({n.text}) < 30min`?",
            )
        if n.type == T.TIMEOFDAY:
            self.fail(
                f"`{n.text}` is a time of day, but {what} needs a condition",
                n.span,
                f"did you mean `now >= {n.text}`, or `fresh({n.text})` in a state machine?",
            )
        if n.type == T.DURATION:
            self.fail(
                f"`{n.text}` is a duration, but {what} needs a condition",
                n.span,
                f"compare it, e.g. `{n.text} > 10min`",
            )
        self.fail(f"{what} needs a condition, but `{n.text}` is {article(n.type)}", n.span)
        raise AssertionError

    @staticmethod
    def sugar_timestamp_hint(operand: A.Expr, duration: A.Expr, within: bool) -> str:
        """`for`/`within` take conditions; for a timestamp (a button, `manual()`), say what to
        write instead, with the duration as written."""
        x, d = to_source(operand), to_source(duration)
        recent = f"`age({x}) < {d}` for 'happened in the last {d}'"
        if within:
            return f"use {recent}"
        return f"use {recent}, or `age({x}) >= {d}` for 'hasn't happened for {d}'"

    def const_duration(self, n: ir.Node, what: str) -> ir.Const:
        if not isinstance(n, ir.Const) or n.type != T.DURATION:
            self.fail(f"{what} needs a fixed duration like `5min`", n.span)
        return n

    def history_cell(self, n: ir.Node, what: str) -> ir.CellDef:
        """The cell tracking `n`'s history (for age, for, latest, on, previous, ...)."""
        if isinstance(n, ir.Read):
            return n.cell
        if n.moving:
            self.fail(
                f"`{n.text}` changes continuously, so {what} can't track when it changed",
                n.span,
                "compare it first, e.g. `age(x) > 10min`",
            )
        if n.type == T.KEEP:
            self.fail("`keep` is only allowed as a binding result", n.span)
        return self.cell(ir.ExprCell(f"expr:{n.text}", n.type, n.text, n.span, expr=n))

    def occurrence_cell(self, e: A.Expr, ctx: _Ctx, what: str) -> ir.CellDef:
        """A bool or timestamp signal used as an occurrence (§3); bare times become time points."""
        n = self.resolve(e, ctx)
        if isinstance(n, ir.Const) and n.type == T.TIMEOFDAY:
            return self.cell(
                ir.TimePointCell(
                    f"timepoint:{n.text}",
                    T.TIMESTAMP,
                    n.text,
                    n.span,
                    daily=n.value,
                    sun=self.sun_cell(n.span, n.value),
                )
            )
        if n.type not in (T.BOOL, T.TIMESTAMP, T.ANY):
            self.fail(
                f"{what} needs a bool or a timestamp, but `{n.text}` is {article(n.type)}", n.span
            )
        return self.history_cell(n, what)

    def arith(self, op: str, a: ir.Node, b: ir.Node, span: Span) -> ir.Node:
        ta, tb = a.type, b.type
        num = lambda t: t in NUMERIC or t == T.ANY  # noqa: E731
        result: T | None = None
        if ta == T.TIMEOFDAY and op in "+-" and tb == T.DURATION:
            if isinstance(a, ir.Const) and isinstance(b, ir.Const):
                delta = b.value.lo if op == "+" else -b.value.lo
                text = f"{a.text} {op} {b.text}"
                return ir.Const(T.TIMEOFDAY, span, text, False, a.value.shifted(delta))
            self.fail("a time of day can only be shifted by a fixed duration", span)
        if num(ta) and num(tb):
            if ta == T.PERCENT and (tb == T.PERCENT or op in "*/"):
                result = T.PERCENT
            else:
                result = T.NUMBER
        elif ta == T.DURATION and tb == T.DURATION:
            result = T.DURATION if op in "+-" else (T.NUMBER if op == "/" else None)
        elif ta == T.DURATION and num(tb) and op in "*/":
            result = T.DURATION
        elif num(ta) and tb == T.DURATION and op == "*":
            result = T.DURATION
        elif ta == T.TIMESTAMP and tb == T.TIMESTAMP and op == "-":
            result = T.DURATION
        elif ta == T.TIMESTAMP and tb == T.DURATION and op in "+-":
            result = T.TIMESTAMP
        elif ta == T.DURATION and tb == T.TIMESTAMP and op == "+":
            result = T.TIMESTAMP
        if result is None:
            self.fail(f"can't apply `{op}` to {article(ta)} and {article(tb)}", span)
        if op in "*/" and a.moving and b.moving:
            self.fail(f"can't apply `{op}` to two values that both change with the clock", span)
        if op == "/" and b.moving:
            self.fail("can't divide by a value that changes with the clock", span)
        text = f"{self.paren_arith(a)} {op} {self.paren_arith(b)}"
        if isinstance(a, ir.Const) and isinstance(b, ir.Const) and isinstance(a.value, Num):
            from .values import num_div, num_mul

            value = {
                "+": lambda: a.value + b.value,
                "-": lambda: a.value - b.value,
                "*": lambda: num_mul(a.value, b.value),
                "/": lambda: num_div(a.value, b.value),
            }[op]()
            return ir.Const(result, span, text, False, value)
        return ir.Arith(result, span, text, a.moving or b.moving, op, a, b)

    @staticmethod
    def paren_arith(n: ir.Node) -> str:
        if isinstance(n, (ir.BoolOp, ir.IfElse, ir.Compare, ir.Arith)):
            return f"({n.text})"
        return n.text

    def compare(self, op: str, a: ir.Node, b: ir.Node, span: Span) -> ir.Node:
        text = f"{self.paren_arith(a)} {op} {self.paren_arith(b)}"
        # now vs a time of day → a time range (I3)
        if (
            isinstance(a, ir.Now)
            and b.type == T.TIMEOFDAY
            or isinstance(b, ir.Now)
            and a.type == T.TIMEOFDAY
        ):
            if isinstance(b, ir.Now):
                a, b = b, a
                op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
            if not isinstance(b, ir.Const):
                self.fail("compare `now` with a fixed time of day", span)
            if op in (">", ">=") and b.value == MIDNIGHT:
                # every time of day is at or after midnight (the range 00:00..00:00 is empty)
                return ir.Const(T.BOOL, span, text, False, True)
            if op in (">", ">="):
                return ir.InTimeRange(
                    T.BOOL, span, text, False, b.value, MIDNIGHT, sun=self.sun_cell(span, b.value)
                )
            if op in ("<", "<="):
                return ir.InTimeRange(
                    T.BOOL, span, text, False, MIDNIGHT, b.value, sun=self.sun_cell(span, b.value)
                )
            self.fail(
                f"`now {op} {b.text}` is never exactly true",
                span,
                f"use a range: `now in {b.text}..…`",
            )
        if isinstance(a, ir.Today) or isinstance(b, ir.Today):
            other = b if isinstance(a, ir.Today) else a
            if op not in ("==", "!=") or not isinstance(other, ir.Const) or other.type != T.DAY:
                self.fail("`today` can only be compared with days, e.g. `today == sat`", span)
            return ir.DayIn(T.BOOL, span, text, False, other.value, op == "!=")
        for n in (a, b):
            if n.type == T.KEEP:
                self.fail("`keep` can't be compared", n.span)
            if n.type in (T.ONSPEC, T.LIST):
                self.fail(f"{article(n.type)} can't be compared", n.span)
        if op in ("==", "!="):
            if join(a.type, b.type) is None:
                self.fail(
                    f"can't compare {article(a.type)} with {article(b.type)}",
                    span,
                    self.compare_hint(a, b),
                )
            self.check_string_literal(a, b)
            self.check_string_literal(b, a)
        else:
            ordered = NUMERIC | {T.DURATION, T.TIMESTAMP, T.ANY}
            if a.type not in ordered or b.type not in ordered or join(a.type, b.type) is None:
                self.fail(
                    f"can't order {article(a.type)} and {article(b.type)} with `{op}`",
                    span,
                    self.compare_hint(a, b),
                )
        a, b = self.percent_scale(a, b), self.percent_scale(b, a)
        return ir.Compare(T.BOOL, span, text, False, op, a, b)

    def compare_hint(self, a: ir.Node, b: ir.Node) -> str | None:
        for x, y in ((a, b), (b, a)):
            if (
                x.type == T.BOOL
                and y.type == T.STRING
                and isinstance(y, ir.Const)
                and y.value in ("on", "off")
            ):
                return f"`{x.text}` is on/off: write `{x.text}` or `not {x.text}`, or compare with `{y.value}` without quotes"
            if x.type == T.TIMESTAMP and y.type == T.DURATION:
                return f"did you mean `age({x.text})`?"
        return None

    def check_string_literal(self, x: ir.Node, lit: ir.Node) -> None:
        if not (isinstance(lit, ir.Const) and lit.type == T.STRING):
            return
        value = lit.value
        if isinstance(x, ir.Read) and isinstance(x.cell, ir.MachineCell):
            states = x.cell.states
            if value not in states:
                self.fail(
                    f'`{x.cell.name}` has no state "{value}"',
                    lit.span,
                    did_you_mean(value, states) or "states: " + ", ".join(f'"{s}"' for s in states),
                )
        elif isinstance(x, ir.Read) and isinstance(x.cell, ir.EntityCell) and x.cell.attr is None:
            st = self.entity_state(x.cell.entity_id)
            options = st.attributes.get("options") if st else None
            if isinstance(options, list) and options and value not in options:
                self.sink.warning(
                    f'`{x.cell.entity_id}` has no option "{value}"',
                    lit.span,
                    did_you_mean(value, [str(o) for o in options]),
                )

    def percent_scale(self, p: ir.Node, other: ir.Node) -> ir.Node:
        """`%` against a light's 0–255 brightness is scaled; against other units it's an error."""
        if p.type != T.PERCENT or not isinstance(other, ir.Read):
            return p
        cell = other.cell
        if not isinstance(cell, ir.EntityCell):
            return p
        if cell.attr == "brightness" and cell.entity_id.startswith("light."):
            if isinstance(p, ir.Const):
                return ir.Const(T.NUMBER, p.span, p.text, False, p.value.scale(2.55))
            return ir.Scale(T.NUMBER, p.span, p.text, p.moving, p, 2.55)
        if cell.attr is None:
            st = self.entity_state(cell.entity_id)
            unit = st.attributes.get("unit_of_measurement") if st else None
            if unit is not None and unit != "%":
                self.fail(
                    f"`{cell.entity_id}` is measured in {unit}, not %",
                    p.span,
                    f"compare it with a plain number, e.g. `{cell.entity_id} > 20`",
                )
        return p

    def membership(self, negate: bool, lhs: A.Expr, r: A.Expr, span: Span, ctx: _Ctx) -> ir.Node:
        x = self.resolve(lhs, ctx)
        text_op = "not in" if negate else "in"
        if isinstance(r, A.Range):
            lo, hi = self.resolve(r.lo, ctx), self.resolve(r.hi, ctx)
            text = f"{x.text} {text_op} {lo.text}..{hi.text}"
            if isinstance(x, ir.Now):
                for b in (lo, hi):
                    if not (isinstance(b, ir.Const) and b.type == T.TIMEOFDAY):
                        self.fail("a time range needs times of day, e.g. `22:00..06:00`", b.span)
                sun = self.sun_cell(span, lo.value, hi.value)
                return ir.InTimeRange(
                    T.BOOL, span, text, False, lo.value, hi.value, negate, sun=sun
                )
            if isinstance(x, ir.Today):
                for b in (lo, hi):
                    if not (isinstance(b, ir.Const) and b.type == T.DAY and len(b.value.days) == 1):
                        self.fail("a day range needs single days, e.g. `mon..fri`", b.span)
                (a,) = lo.value.days
                (z,) = hi.value.days
                days = frozenset((a + i) % 7 for i in range(((z - a) % 7) + 1))
                return ir.DayIn(T.BOOL, span, text, False, Days(days), negate)
            self.fail(
                "ranges are for times of day and days",
                span,
                "e.g. `now in 22:00..06:00` or `today in mon..fri`",
            )
        if isinstance(x, ir.Today):
            rhs = [r] if not isinstance(r, A.ListExpr) else r.items
            days: set[int] = set()
            for item in rhs:
                n = self.resolve(item, ctx)
                if not (isinstance(n, ir.Const) and n.type == T.DAY):
                    self.fail("`today in …` needs days, e.g. `today in [sat, sun]`", item.span)
                days |= n.value.days
            return ir.DayIn(
                T.BOOL,
                span,
                f"today {text_op} {to_source(r)}",
                False,
                Days(frozenset(days)),
                negate,
            )
        if not isinstance(r, A.ListExpr):
            self.fail("`in` needs a list or a range", r.span, 'e.g. `x in ["heating", "idle"]`')
        items = [self.resolve(i, ctx) for i in r.items]
        for it in items:
            if join(x.type, it.type) is None or it.type == T.KEEP:
                self.fail(f"can't compare {article(x.type)} with {article(it.type)}", it.span)
            self.check_string_literal(x, it)
        text = f"{x.text} {text_op} [" + ", ".join(i.text for i in items) + "]"
        return ir.InList(T.BOOL, span, text, False, x, items, negate)

    # ------------------------------------------------------------- functions

    def call(self, e: A.Call, ctx: _Ctx) -> ir.Node:
        flat = self.flatten(e.func)
        if flat is None:
            self.fail("only names can be called", e.func.span)
        head, segs = flat
        if not segs and head.id in BUILTIN_FUNCS and head.id not in self.lets:
            return self.builtin_call(head.id, e, ctx)
        if head.id == "bus" and len(segs) == 1:
            return self.bus_event(segs[0].name, e)
        if not segs:
            self.fail(
                f"unknown function `{head.id}`",
                head.span,
                did_you_mean(head.id, list(BUILTIN_FUNCS)),
            )
        self.fail(
            "service calls are only allowed in `on` action bodies",
            e.span,
            "bindings (`:=`) describe values; put effects in a top-level `on x:` action",
        )
        raise AssertionError

    def nargs(self, e: A.Call, name: str, n: int, kwargs: tuple[str, ...] = ()) -> None:
        if len(e.args) != n:
            self.fail(f"`{name}` takes {n} argument{'s' if n != 1 else ''}", e.span)
        for k in e.kwargs:
            if k.name not in kwargs:
                self.fail(f"`{name}` has no argument `{k.name}`", k.span)

    def builtin_call(self, name: str, e: A.Call, ctx: _Ctx) -> ir.Node:
        if name == "age":
            self.nargs(e, name, 1)
            arg = e.args[0]
            n = self.resolve(arg, ctx)
            if isinstance(n, ir.Const) and n.type == T.TIMEOFDAY:
                cell = self.occurrence_cell(arg, ctx, "`age`")
            else:
                cell = self.history_cell(n, "`age`")
            return ir.Age(T.DURATION, e.span, f"age({cell.text})", True, cell)
        if name == "fresh":
            self.nargs(e, name, 1)
            if ctx.machine is None:
                self.fail(
                    "`fresh()` is only allowed inside a `state` block",
                    e.span,
                    "outside a state machine, use `age(x) < D` for 'happened recently'",
                )
            cell = self.occurrence_cell(e.args[0], ctx, "`fresh`")
            return ir.Fresh(T.BOOL, e.span, f"fresh({cell.text})", False, cell, ctx.machine)
        if name == "previous":
            self.nargs(e, name, 1)
            n = self.resolve(e.args[0], ctx)
            cell = self.history_cell(n, "`previous`")
            return ir.Previous(cell.type, e.span, f"previous({cell.text})", False, cell)
        if name == "available":
            self.nargs(e, name, 1)
            n = self.resolve(e.args[0], ctx)
            cell = self.history_cell(n, "`available`")
            return ir.Available(T.BOOL, e.span, f"available({cell.text})", False, cell)
        if name == "manual":
            self.nargs(e, name, 1)
            n = self.resolve(e.args[0], ctx)
            if not (
                isinstance(n, ir.Read) and isinstance(n.cell, ir.EntityCell) and n.cell.attr is None
            ):
                self.fail(
                    "`manual()` takes an entity", e.args[0].span, "e.g. `manual(fan.bathroom)`"
                )
            eid = n.cell.entity_id
            text = f"manual({eid})"
            cell = self.cell(
                ir.ManualCell(f"manual:{eid}", T.TIMESTAMP, text, e.span, entity_id=eid)
            )
            return ir.Read(T.TIMESTAMP, e.span, text, False, cell)
        if name == "toggle":
            self.nargs(e, name, 1, ("initial",))
            cell = self.occurrence_cell(e.args[0], ctx, "`toggle`")
            initial = False
            for k in e.kwargs:
                v = self.resolve(k.value, ctx)
                if not (isinstance(v, ir.Const) and v.type == T.BOOL):
                    self.fail("`initial` must be `on` or `off`", k.value.span)
                initial = v.value
            text = f"toggle({cell.text}, initial: {'on' if initial else 'off'})"
            tcell = self.cell(
                ir.ToggleCell(
                    f"toggle:{cell.key}:{initial}",
                    T.BOOL,
                    text,
                    e.span,
                    source=cell,
                    initial=initial,
                )
            )
            return ir.Read(T.BOOL, e.span, text, False, tcell)
        if name == "count":
            self.nargs(e, name, 2)
            cell = self.occurrence_cell(e.args[0], ctx, "`count`")
            dur = self.const_duration(self.resolve(e.args[1], ctx), "`count`")
            text = f"count({cell.text}, {dur.text})"
            ccell = self.cell(
                ir.CountCell(
                    f"count:{cell.key}:{dur.value.lo:g}",
                    T.NUMBER,
                    text,
                    e.span,
                    source=cell,
                    window=dur.value.lo,
                )
            )
            return ir.Read(T.NUMBER, e.span, text, False, ccell)
        raise AssertionError(name)

    def bus_event(self, event_type: str, e: A.Call) -> ir.Node:
        if e.args:
            self.fail('bus event filters are named, e.g. `command: "on"`', e.args[0].span)
        filters: dict[str, Any] = {}
        for k in e.kwargs:
            v = self.resolve(k.value, _Ctx())
            if not isinstance(v, ir.Const) or v.type not in (T.STRING, T.NUMBER, T.BOOL):
                self.fail("bus event filters must be constants", k.value.span)
            filters[k.name] = v.value.lo if isinstance(v.value, Num) else v.value
        text = (
            f"bus.{event_type}("
            + ", ".join(f"{k}: {v!r}" for k, v in sorted(filters.items()))
            + ")"
        )
        cell = self.cell(
            ir.BusCell(
                f"bus:{text}", T.TIMESTAMP, text, e.span, event_type=event_type, filters=filters
            )
        )
        return ir.Read(T.TIMESTAMP, e.span, text, False, cell)

    # ---------------------------------------------------------------- blocks

    def cascade(self, arms: list[A.Arm | A.Group], span: Span, ctx: _Ctx) -> ir.Node:
        out = [self.arm(a, ctx, latest=False) for a in arms]
        t = self.join_results(out, span)
        return ir.Cascade(t, span, "cascade", self.any_moving(out), out)

    def latest(self, arms: list[A.Arm | A.Group], span: Span, ctx: _Ctx) -> ir.Node:
        out = [self.arm(a, ctx, latest=True) for a in arms]
        t = self.join_results(out, span)
        return ir.Latest(t, span, "latest", self.any_moving(out), out)

    def any_moving(self, arms: list[ir.ArmIR | ir.GroupIR]) -> bool:
        """Whether a block's result can be a behavior (D23): some arm's result moves."""
        return any(r.moving for r in self.results(arms))

    def arm(self, a: A.Arm | A.Group, ctx: _Ctx, latest: bool) -> ir.ArmIR | ir.GroupIR:
        if isinstance(a, A.Group):
            guard = self.arm_cond(a.guard, ctx, latest)
            subs = [self.arm(s, ctx, latest=False) for s in a.arms]
            return ir.GroupIR(guard, subs, a.span, guard.text, to_source(a.guard))
        cond = None if a.cond is None else self.arm_cond(a.cond, ctx, latest)
        result = self.resolve(a.result, ctx)  # may be a behavior (D23); bindings check it
        text = f"{'else' if cond is None else cond.text} -> {result.text}"
        cond_source = "else" if a.cond is None else to_source(a.cond)
        source = f"{cond_source} -> {to_source(a.result)}"
        return ir.ArmIR(cond, result, a.span, text, source, cond_source)

    def arm_cond(self, e: A.Expr, ctx: _Ctx, latest: bool) -> ir.Node:
        if latest:
            cell = self.occurrence_cell(e, ctx, "a `latest` condition")
            return ir.Read(cell.type, e.span, cell.text, False, cell)
        return self.want_bool(self.resolve(e, ctx), "an arm")

    def join_results(self, arms: list[ir.ArmIR | ir.GroupIR], span: Span) -> T:
        t: T = T.KEEP
        for r in self.results(arms):
            j = join(t, r.type)
            if j is None:
                self.fail(f"arms have different types ({t} and {r.type})", r.span)
            t = j
        return t

    def results(self, arms: list[ir.ArmIR | ir.GroupIR]) -> list[ir.Node]:
        out: list[ir.Node] = []
        for a in arms:
            if isinstance(a, ir.GroupIR):
                out.extend(self.results(a.arms))
            else:
                out.extend(self.leaf_results(a.result))
        return out

    def leaf_results(self, n: ir.Node) -> list[ir.Node]:
        if isinstance(n, ir.IfElse):
            return self.leaf_results(n.then) + self.leaf_results(n.otherwise)
        if isinstance(n, (ir.Cascade, ir.Latest)):
            return self.results(n.arms)
        if isinstance(n, ir.Coalesce):
            return self.leaf_results(n.left) + self.leaf_results(n.right)
        return [n]

    # -------------------------------------------------------------- bindings

    def check_bind(self, stmt: A.Bind) -> None:
        flat = self.flatten(stmt.target)
        if flat is None:
            self.fail("the left side of `:=` must be an entity id", stmt.target.span)
        head, segs = flat
        if head.id in self.lets or head.id in self.machines:
            kind = "`let` name" if head.id in self.lets else "state machine"
            hint = None
            if head.id in self.lets:
                v = self.let_value(head.id)
                if isinstance(v, ir.Read) and isinstance(v.cell, ir.EntityCell):
                    hint = f"write the full entity id: `{v.cell.text} := …`"
            self.fail(
                f"`{head.id}` is a {kind}; bindings need the full entity id",
                stmt.target.span,
                hint or "every driven device is written by its real entity id",
            )
        if head.id not in self.domains or not segs:
            self.fail(
                "the left side of `:=` must be an entity id like `light.hall`", stmt.target.span
            )
        domain = head.id
        if segs[0].quoted:
            self.fail("entity ids can't be quoted", segs[0].span)
        entity_id = f"{domain}.{segs[0].name}"
        attr = segs[1].name if len(segs) > 1 else None
        if len(segs) > 2:
            self.fail("too many `.` segments", segs[2].span)
        key = entity_id if attr is None else f"{entity_id}.{attr}"
        if domain == "notify":
            self.fail(
                "notifications can't be bound in v1",
                stmt.target.span,
                'use an action: `on x:\n    notify.…(message: "…")`',
            )
        self.require_entity(entity_id, stmt.target.span)
        writer = WRITERS.get((domain, attr))
        if writer is None:
            self.no_writer(domain, entity_id, attr, stmt.target.span)
        # one writer per target, across files
        if key in self.globals.targets:
            self.fail(
                f"`{key}` is already bound in {self.globals.targets[key]}",
                stmt.target.span,
                "combine the rules into one cascade",
            )
        if any(b.key == key for b in self.program.bindings):
            self.fail(
                f"`{key}` is bound twice", stmt.target.span, "combine the rules into one cascade"
            )
        if domain == "cover":
            other = f"{entity_id}.position" if attr is None else entity_id
            elsewhere = self.globals.targets.get(other) or (
                self.file.name if any(b.key == other for b in self.program.bindings) else None
            )
            if elsewhere:
                self.fail(
                    f"`{entity_id}` is bound both by state and by position",
                    stmt.target.span,
                    'bind a cover either by "open"/"closed" or by `.position`, not both',
                )
        value = self.resolve(stmt.value, _Ctx(where="binding"))
        if isinstance(stmt.value, (A.Cascade, A.Latest)) and isinstance(
            value, (ir.Cascade, ir.Latest)
        ):
            last = value.arms[-1] if value.arms else None
            if isinstance(last, ir.ArmIR) and last.cond is None:
                pass
            elif isinstance(value, ir.Latest):
                # Until one of the conditions has happened, leave the device alone.
                keep = ir.Const(T.KEEP, stmt.value.span, "keep", False, KEEP)
                value.arms.append(
                    ir.ArmIR(
                        None, keep, stmt.value.span, "else -> keep", "else -> keep", implicit=True
                    )
                )
            else:
                self.error(
                    "a binding's arms must end with `else -> …`",
                    stmt.value.span,
                    "use `else -> keep` to leave the device alone otherwise",
                )
        if value.moving and not writer.follows:
            self.fail(
                f"`{value.text}` changes continuously, so it can't be bound to `{key}`",
                value.span,
                "compare it first, e.g. `age(x) > 10min`",
            )
        for r in self.leaf_results(value):
            self.check_result(r, writer, key)
        reads = self.reads(value)
        self.binding_reads[key] = reads
        machines: set[str] = set()
        self.binding_reads_via_machines[key] = self.reads(value, machines)
        self.binding_machines[key] = machines
        for eid, a in reads:
            if self.affects(entity_id, writer, eid, a):
                self.fail(
                    f"the rule for `{key}` reads `{key}` itself",
                    stmt.value.span,
                    "a binding can't depend on its own target (only `manual()` may refer to it)",
                )
        self.program.bindings.append(
            ir.BindingDef(
                key,
                entity_id,
                attr,
                writer,
                value,
                stmt.span,
                self.file.name,
                f"{key} := {value.text}",
            )
        )

    def no_writer(self, domain: str, entity_id: str, attr: str | None, span: Span) -> None:
        if attr is not None and domain in WHOLE_ONLY:
            kind = {"light": "a light", "fan": "a fan"}[domain]
            example = "on(brightness: 50%)" if domain == "light" else "on(percentage: 60%)"
            self.fail(
                f"{kind}'s attributes can't be bound separately",
                span,
                f"bind {kind} as a whole: `{entity_id} := {example}`",
            )
        bindable = sorted(a for d, a in WRITERS if d == domain and a)
        if attr is not None:
            hint = (
                f"bindable attributes of {domain}: " + ", ".join(f"`{a}`" for a in bindable)
                if bindable
                else None
            )
            if (domain, None) in WRITERS:
                hint = (hint + "; " if hint else "") + f"or bind `{entity_id}` itself"
            self.fail(f"`{entity_id}.{attr}` can't be bound", span, hint)
        domains = sorted({d for d, _ in WRITERS})
        hint = "homeo can drive: " + ", ".join(domains)
        if bindable:
            hint = "bind an attribute instead: " + ", ".join(f"`{entity_id}.{a}`" for a in bindable)
        else:
            hint += "; use an `on` action for anything else"
        self.fail(f"`{entity_id}` can't be driven", span, hint)

    def check_result(self, r: ir.Node, writer: Writer, key: str) -> None:
        if r.type in (T.KEEP, T.ANY):
            return
        if T.BOOL in writer.accepts and r.type in (T.TIMESTAMP, T.TIMEOFDAY, T.DURATION):
            self.want_bool(r, f"`{key}`")
        if not any(assignable(r.type, t) for t in writer.accepts):
            self.fail(
                f"`{key}` takes {writer.description}, but `{r.text}` is {article(r.type)}", r.span
            )
        if isinstance(r, ir.OnValue):
            spans = self._on_spans.get(id(r), {})
            for name, v in r.params:
                p = writer.on_params.get(name)
                sp = spans.get(name, r.span)
                if p is None:
                    self.fail(
                        f"`{key}` has no setting `{name}`",
                        sp,
                        did_you_mean(name, list(writer.on_params))
                        or "settings: " + ", ".join(f"`{n}`" for n in writer.on_params),
                    )
                for leaf in self.leaf_results(v):
                    if leaf.type == T.ANY:
                        continue
                    if p.type == T.PERCENT and leaf.type != T.PERCENT:
                        self.fail(f"`{name}` takes a percentage, e.g. `{name}: 50%`", leaf.span)
                    if p.type == T.NUMBER and leaf.type not in NUMERIC:
                        self.fail(f"`{name}` takes a number", leaf.span)
        if isinstance(r, ir.Const) and r.type == T.STRING:
            allowed = writer.literal_values
            if allowed is None and writer.domain in ("input_select", "select"):
                st = self.entity_state(key)
                options = st.attributes.get("options") if st else None
                allowed = tuple(options) if isinstance(options, list) else None
            if allowed is not None and r.value not in allowed:
                self.fail(
                    f'`{key}` can\'t be set to "{r.value}"',
                    r.span,
                    did_you_mean(r.value, list(allowed))
                    or "options: " + ", ".join(f'"{a}"' for a in allowed),
                )

    @staticmethod
    def affects(entity_id: str, writer: Writer, read_eid: str, read_attr: str | None) -> bool:
        """Whether reading (read_eid, read_attr) reads the target this writer drives."""
        return read_eid == entity_id and read_attr in writer.read_attrs

    def reads(self, n: ir.Node, machines: set[str] | None = None) -> set[tuple[str, str | None]]:
        """Entity reads a value depends on (through cells, not through manual()). Machines
        break cycles (I17), so their arms are followed only when `machines` is given; it
        collects the names of the machines read."""
        out: set[tuple[str, str | None]] = set()
        seen: set[int] = set()

        def cell(c: ir.CellDef) -> None:
            if id(c) in seen:
                return
            seen.add(id(c))
            if isinstance(c, ir.EntityCell):
                out.add((c.entity_id, c.attr))
            elif isinstance(c, ir.EventTypeCell):
                out.add((c.entity_id, None))
            elif isinstance(c, ir.ManualCell):
                return
            elif isinstance(c, ir.MachineCell):
                if machines is None:
                    return
                machines.add(c.name)
            for d in c.deps():
                cell(d)

        for c in ir.node_cells(n):
            cell(c)
        return out

    def check_binding_cycles(self) -> None:
        cycle = self.binding_cycle(self.binding_reads, min_len=2)
        if cycle is not None:
            b, path = cycle
            self.error(
                "bindings drive each other in a loop: " + " → ".join(path),
                b.span,
                "break the loop, e.g. with a state machine or `manual()`",
            )
            return
        # Through a state machine, a loop is allowed (I17) but the runtime can't stop it if
        # it goes round through a device: binding → device → machine → binding.
        cycle = self.binding_cycle(self.binding_reads_via_machines, min_len=1)
        if cycle is not None:
            b, path = cycle
            names = sorted(
                m
                for m in {m for k in path for m in self.binding_machines.get(k, ())}
                if self.machine_reads_any(m, path)
            )
            what = ("state machine " if len(names) == 1 else "state machines ") + ", ".join(
                f"`{m}`" for m in names
            )
            if len(path) == 2:
                how = f"its rule reads {what}, which reads `{b.key}`"
            else:
                loop = " → ".join(f"`{k}`" for k in path)
                how = f"these bindings and {what} drive each other in a loop: {loop}"
            self.sink.warning(
                f"`{b.key}` may switch back and forth forever: {how}",
                b.span,
                "make sure a change these rules make can't move the machine to a state that "
                "undoes it; homeo doesn't stop such a loop",
            )

    def machine_reads_any(self, name: str, keys: list[str]) -> bool:
        """Whether a machine's arms read (through anything but manual()) one of these
        binding targets."""
        m = self.machines[name]
        reads = self.reads(ir.Read(T.STRING, m.span, name, False, m), set())
        for k in keys:
            parts = k.split(".")
            w = WRITERS.get((parts[0], parts[2] if len(parts) > 2 else None))
            eid = ".".join(parts[:2])
            if w is not None and any(self.affects(eid, w, e, a) for e, a in reads):
                return True
        return False

    def binding_cycle(
        self, own_reads: dict[str, set[tuple[str, str | None]]], min_len: int
    ) -> tuple[ir.BindingDef, list[str]] | None:
        """A binding in this file that drives itself through other bindings' reads: the
        binding and the loop's path of target keys."""
        all_reads = dict(self.globals.binding_reads)
        all_reads.update(own_reads)
        targets: dict[str, tuple[str, Writer | None]] = {}
        for k in all_reads:
            if k in self.globals.targets or any(b.key == k for b in self.program.bindings):
                parts = k.split(".")
                eid = ".".join(parts[:2])
                attr = parts[2] if len(parts) > 2 else None
                targets[k] = (eid, WRITERS.get((parts[0], attr)))

        def edges(k: str) -> list[str]:
            out = []
            for eid, a in all_reads.get(k, ()):
                for t, (teid, w) in targets.items():
                    if w is not None and self.affects(teid, w, eid, a):
                        out.append(t)
            return out

        for b in self.program.bindings:
            stack = [(b.key, [b.key])]
            visited: set[str] = set()
            while stack:
                k, path = stack.pop()
                for nxt in edges(k):
                    if nxt == b.key and len(path) >= min_len:
                        return b, path + [b.key]
                    if nxt not in visited:
                        visited.add(nxt)
                        stack.append((nxt, path + [nxt]))
        return None

    def reads_for_globals(self) -> dict[str, set[tuple[str, str | None]]]:
        return dict(self.binding_reads)

    # ---------------------------------------------------------------- machines

    def check_state(self, stmt: A.StateDecl) -> None:
        cell = self.machines.get(stmt.name)
        if cell is None:
            return
        ctx = _Ctx(machine=cell, where="state")
        arms: list[ir.ArmIR | ir.GroupIR] = []
        for a in stmt.arms:
            try:
                arm = self.arm(a, ctx, latest=False)
                self.check_machine_results(arm, cell)
                arms.append(arm)
            except _Fail:
                pass
        cell.arms = arms
        self.program.machines.append(cell)

    def check_machine_results(self, arm: ir.ArmIR | ir.GroupIR, cell: ir.MachineCell) -> None:
        """Results are state names or `previous(self)`, so the states are known (I8)."""
        if isinstance(arm, ir.GroupIR):
            for sub in arm.arms:
                self.check_machine_results(sub, cell)
            return
        r = arm.result
        if isinstance(r, ir.Const) and r.type == T.STRING and r.value in cell.states:
            return
        if isinstance(r, ir.Previous) and r.cell is cell:
            return
        self.fail(
            f"a state machine's results must be state names (strings) or `previous({cell.name})`",
            r.span,
            'e.g. `-> "boost"`',
        )

    # ----------------------------------------------------------------- actions

    def check_action(self, stmt: A.Action) -> None:
        ctx = _Ctx(where="action")
        trigger = self.occurrence_cell(stmt.trigger, ctx, "`on`")
        guard = None
        if stmt.guard is not None:
            guard = self.want_bool(self.resolve(stmt.guard, ctx), "the `if` guard")
        calls = [self.service_call(c, ctx) for c in stmt.body]
        header = f"on {trigger.text}" + (f" if {guard.text}" if guard else "")
        key = f"action:{self.file.name}:{header}"
        n = sum(1 for a in self.program.actions if a.key.startswith(key))
        if n:
            key += f"#{n + 1}"
        self.program.actions.append(
            ir.ActionDef(key, trigger, guard, calls, stmt.span, self.file.name, header)
        )

    def service_call(self, c: A.Call, ctx: _Ctx) -> ir.ServiceCallIR:
        flat = self.flatten(c.func)
        if flat is None:
            self.fail("expected a service call", c.span)
        head, segs = flat
        if c.args:
            self.fail('service data is named, e.g. `message: "…"`', c.args[0].span)
        entity_id: str | None = None
        if head.id in self.lets:
            node = self.let_value(head.id)
            if (
                not (
                    isinstance(node, ir.Read)
                    and isinstance(node.cell, ir.EntityCell)
                    and node.cell.attr is None
                )
                or len(segs) != 1
            ):
                self.fail("expected `domain.service(…)` or `entity.service(…)`", c.func.span)
            entity_id = node.cell.entity_id
            domain, service = entity_id.split(".")[0], segs[0].name
        elif len(segs) == 1:
            # Any domain: many have services but no entities (persistent_notification,
            # shell_command, tts, …). A typo is caught by the service check below.
            domain, service = head.id, segs[0].name
        elif head.id in self.domains and len(segs) == 2:
            entity_id = f"{head.id}.{segs[0].name}"
            self.require_entity(entity_id, c.func.span)
            domain, service = head.id, segs[1].name
        else:
            self.fail("expected `domain.service(…)` or `entity.service(…)`", c.func.span)
        known = self.catalog.has_service(domain, service)
        if known is False:
            self.sink.warning(f"service `{domain}.{service}` doesn't exist (yet)", c.func.span)
        data = []
        for k in c.kwargs:
            v = self.resolve(k.value, ctx)
            if v.type in (T.KEEP, T.ONSPEC):
                self.fail(f"`{v.text}` can't be sent as service data", k.value.span)
            data.append((k.name, v))
        text = (
            (f"{entity_id}.{service}" if entity_id else f"{domain}.{service}")
            + "("
            + ", ".join(f"{k}: {v.text}" for k, v in data)
            + ")"
        )
        return ir.ServiceCallIR(domain, service, entity_id, data, c.span, text)

    def check_action_targets(self) -> None:
        bound = dict(self.globals.bound_entities)
        for b in self.program.bindings:
            bound[b.entity_id] = self.file.name
        for a in self.program.actions:
            for c in a.calls:
                for eid in c.targets():
                    if eid not in bound:
                        continue
                    where = bound[eid]
                    self.error(
                        f"`{eid}` is driven by a binding"
                        + (f" in {where}" if where != self.file.name else "")
                        + ", so actions can't call services on it",
                        c.span,
                        "change the binding's rule instead, so they don't fight",
                    )
        # bindings in this file on entities targeted by actions in other files
        for b in self.program.bindings:
            other = self.globals.action_entities.get(b.entity_id)
            if other:
                self.error(
                    f"`{b.entity_id}` is the target of an action in {other}, so it can't be bound",
                    b.span,
                )


def check(
    file: A.File,
    catalog: EntityCatalog,
    globals_: Globals | None = None,
) -> tuple[ir.Program, dict[str, set[tuple[str, str | None]]]]:
    """Check a parsed file. Returns the program and its binding reads (for Globals.add)."""
    c = Checker(file, catalog, globals_)
    program = c.check()
    return program, c.reads_for_globals()


def compile_source(
    source: str,
    file: str,
    catalog: EntityCatalog,
    globals_: Globals | None = None,
) -> tuple[ir.Program, dict[str, set[tuple[str, str | None]]]]:
    from .parser import parse

    return check(parse(source, file), catalog, globals_)


__all__ = ["check", "compile_source", "Globals", "Checker", "fmt_duration"]
