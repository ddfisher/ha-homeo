"""Resolved, typed program representation produced by the checker and run by the runtime.

Expressions (`Node`) are pure functions of cells and the clock. **Cells** (`CellDef`) are the
things whose history the runtime tracks (implementation plan §2.2). Every node and cell has a
canonical `text`, printed from the tree with resolved names (entity ids instead of `let`
aliases), for messages and `homeo.explain`: two nodes with the same structure print alike. A
cell's `key` identifies it, and carries its state across reloads; an expression's cell is keyed
by `expr_key(expr)`, a digest of its structure, never by its text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any

from .errors import Span
from .printer import PREC, fmt_duration, fmt_number, fmt_string
from .types import T
from .values import DAY_CONSTS, DAY_NAMES, MIDNIGHT, UNKNOWN, DailyTime, Num

# ------------------------------------------------------------------ expressions


@dataclass(eq=False, slots=True)
class Node:
    type: T
    span: Span
    moving: bool = False  # value changes continuously with the clock

    @property
    def text(self) -> str:
        """Canonical source text (`node_text`)."""
        return node_text(self)

    @property
    def brief(self) -> str:
        """`text` with block bodies left out (`cascade{…}`), for messages."""
        return node_text(self, brief=True)

    @property
    def prec(self) -> int:
        """How tightly `text` binds (printer.PREC)."""
        return node_prec(self)


@dataclass(eq=False, slots=True)
class Error(Node):
    """Stands in for a `let` that failed to check, so its uses can still be checked."""

    label: str = "<error>"


@dataclass(eq=False, slots=True)
class Const(Node):
    value: Any = None


@dataclass(eq=False, slots=True)
class Now(Node):
    pass


@dataclass(eq=False, slots=True)
class Today(Node):
    """The current local weekday (`today`)."""


@dataclass(eq=False, slots=True)
class Read(Node):
    """The current value of a cell."""

    cell: CellDef = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Not(Node):
    operand: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Neg(Node):
    operand: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class BoolOp(Node):
    op: str = "and"  # "and" | "or"
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Arith(Node):
    op: str = "+"
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Compare(Node):
    op: str = "=="
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Scale(Node):
    """Multiply a number by a constant factor (e.g. `%` against a 0–255 brightness)."""

    operand: Node = None  # type: ignore[assignment]
    factor: float = 1.0


@dataclass(eq=False, slots=True)
class Coalesce(Node):
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class IfElse(Node):
    cond: Node = None  # type: ignore[assignment]
    then: Node = None  # type: ignore[assignment]
    otherwise: Node = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class InList(Node):
    operand: Node = None  # type: ignore[assignment]
    items: list[Node] = field(default_factory=list)
    negate: bool = False


@dataclass(eq=False, slots=True)
class InTimeRange(Node):
    """`now in start..end` (I3): start occurred more recently than end."""

    start: Any = None  # DailyTime
    end: Any = None  # DailyTime
    negate: bool = False
    sun: CellDef | None = None  # set when an end is a solar time


@dataclass(eq=False, slots=True)
class DayIn(Node):
    days: Any = None  # Days
    negate: bool = False


@dataclass(eq=False, slots=True)
class Age(Node):
    cell: CellDef = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Fresh(Node):
    cell: CellDef = None  # type: ignore[assignment]
    machine: MachineCell = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Previous(Node):
    cell: CellDef = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class Available(Node):
    cell: CellDef = None  # type: ignore[assignment]


@dataclass(eq=False, slots=True)
class OnValue(Node):
    params: list[tuple[str, Node]] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class ArmIR:
    cond: Node | None  # None for else
    result: Node
    span: Span
    source: str = ""  # as written (with `let` names), for homeo.explain
    cond_source: str = "else"
    implicit: bool = False  # not written: `else -> keep` added to a `latest` binding

    @property
    def text(self) -> str:
        return arm_text(self)


@dataclass(eq=False, slots=True)
class GroupIR:
    guard: Node
    arms: list[ArmIR | GroupIR]
    span: Span
    source: str = ""  # the guard as written

    @property
    def text(self) -> str:
        """The guard's text."""
        return self.guard.text


@dataclass(eq=False, slots=True)
class Cascade(Node):
    arms: list[ArmIR | GroupIR] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class Latest(Node):
    """Arm conditions (and group guards) are `Read`s of occurrence cells."""

    arms: list[ArmIR | GroupIR] = field(default_factory=list)


# ------------------------------------------------------------------------ cells


@dataclass(eq=False, slots=True)
class CellDef:
    key: str
    type: T
    text: str
    span: Span | None = None

    def deps(self) -> list[CellDef]:
        return []


@dataclass(eq=False, slots=True)
class EntityCell(CellDef):
    """An entity's state (attr None) or one attribute."""

    entity_id: str = ""
    attr: str | None = None


@dataclass(eq=False, slots=True)
class EventTypeCell(CellDef):
    """`event.x.<type>`: time of the last event of that type (D8)."""

    entity_id: str = ""
    event_type: str = ""


@dataclass(eq=False, slots=True)
class ManualCell(CellDef):
    entity_id: str = ""


@dataclass(eq=False, slots=True)
class BusCell(CellDef):
    event_type: str = ""
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(eq=False, slots=True)
class EveryCell(CellDef):
    period: float = 60.0


@dataclass(eq=False, slots=True)
class TimePointCell(CellDef):
    daily: Any = None  # DailyTime
    sun: CellDef | None = None  # set for solar times

    def deps(self) -> list[CellDef]:
        return [self.sun] if self.sun is not None else []


@dataclass(eq=False, slots=True)
class SunCell(CellDef):
    """Solar times from `sun.sun` (core/sun.py). Its value changes only when they do."""

    entity_id: str = "sun.sun"


@dataclass(eq=False, slots=True)
class ExprCell(CellDef):
    expr: Node = None  # type: ignore[assignment]

    def deps(self) -> list[CellDef]:
        return node_cells(self.expr)


@dataclass(eq=False, slots=True)
class ToggleCell(CellDef):
    source: CellDef = None  # type: ignore[assignment]
    initial: bool = False

    def deps(self) -> list[CellDef]:
        return [self.source]


@dataclass(eq=False, slots=True)
class CountCell(CellDef):
    source: CellDef = None  # type: ignore[assignment]
    window: float = 60.0

    def deps(self) -> list[CellDef]:
        return [self.source]


@dataclass(eq=False, slots=True)
class MachineCell(CellDef):
    name: str = ""
    file: str = ""
    initial: str = ""
    states: list[str] = field(default_factory=list)
    arms: list[ArmIR | GroupIR] = field(default_factory=list)

    def deps(self) -> list[CellDef]:
        # Arm inputs. Machines are sources for ordering (they read their own previous value).
        out: list[CellDef] = []
        for a in self.arms:
            out.extend(arm_cells(a))
        return _unique(out)


# ------------------------------------------------------------- bindings, actions


@dataclass(eq=False, slots=True)
class ServiceCallIR:
    domain: str
    service: str
    entity_id: str | None
    data: list[tuple[str, Node]]
    span: Span
    text: str

    def targets(self) -> list[str]:
        """Entities this call acts on: `entity.service()`, plus constant `entity_id` data
        (a string, possibly comma-separated, as HA accepts)."""
        out = [self.entity_id] if self.entity_id else []
        for key, node in self.data:
            if key == "entity_id" and isinstance(node, Const) and isinstance(node.value, str):
                out += [e.strip() for e in node.value.split(",") if e.strip()]
        return out


@dataclass(eq=False, slots=True)
class BindingDef:
    key: str  # target key: "light.hall" or "climate.x.temperature"
    entity_id: str
    attr: str | None
    writer: Any  # writers.Writer
    expr: Node
    span: Span
    file: str
    text: str

    def deps(self) -> list[CellDef]:
        return node_cells(self.expr)


@dataclass(eq=False, slots=True)
class ActionDef:
    key: str
    trigger: CellDef
    guard: Node | None
    calls: list[ServiceCallIR]
    span: Span
    file: str
    text: str

    def deps(self) -> list[CellDef]:
        out = [self.trigger]
        if self.guard is not None:
            out += node_cells(self.guard)
        for c in self.calls:
            for _, n in c.data:
                out += node_cells(n)
        return _unique(out)


@dataclass(slots=True)
class Program:
    file: str
    cells: dict[str, CellDef] = field(default_factory=dict)
    bindings: list[BindingDef] = field(default_factory=list)
    actions: list[ActionDef] = field(default_factory=list)
    machines: list[MachineCell] = field(default_factory=list)
    lets: dict[str, Node] = field(default_factory=dict)  # name → value (for homeo.explain)
    dry_run: bool = False
    warnings: list[Any] = field(default_factory=list)


# ----------------------------------------------------------------------- helpers

# Fields that only describe how a node was written or displayed, or that follow from its
# other fields, not what it computes. (An `Error`'s `label` isn't one: it's the failed `let`
# it stands for, so two failed lets don't share a cell and its messages.)
_DISPLAY_ONLY = frozenset({"span", "moving", "source", "cond_source", "implicit"})


def structure(x: Any) -> str:
    """A fully bracketed prefix form of an expression tree, used to key its history cell.

    Two expressions get the same string exactly when they are the same tree: the same node
    kinds with the same operators and constants, over the same cells. The form is bracketed,
    constants are `repr`s and cell keys are length-prefixed (so a nested key needs no
    escaping), so no two different trees print alike, whatever their display text looks like.
    It's generic over dataclass fields, so a new node kind is covered without a matching rule
    here.

    Only constants keep their type (`5` vs `5%`, `300` vs `5min`). Any other node's type
    follows from its children and cells, and can change with an entity's state when the rules
    are checked (an `unavailable` sensor has no unit, so it reads as `any`), which mustn't
    change the key and lose the cell's history on reload.
    """
    match x:
        case CellDef():
            return f"(cell {len(x.key)}:{x.key})"
        case Enum():
            return x.name
        case list() | tuple():
            return "[" + " ".join(structure(i) for i in x) + "]"
        case frozenset() | set():
            return "{" + " ".join(sorted(structure(i) for i in x)) + "}"
        case dict():
            return "{" + " ".join(f"{k!r}:{structure(v)}" for k, v in sorted(x.items())) + "}"
        case _ if is_dataclass(x) and not isinstance(x, type):
            parts = [
                structure(getattr(x, f.name))
                for f in fields(x)
                if f.name not in _DISPLAY_ONLY and (f.name != "type" or isinstance(x, Const))
            ]
            return "(" + " ".join([type(x).__name__, *parts]) + ")"
    return repr(x)


def expr_key(n: Node) -> str:
    """The key of `n`'s history cell. A digest, so keys stay short however deeply cells nest
    (a cell's structure includes the keys of the cells it reads)."""
    return "expr:" + hashlib.sha256(structure(n).encode()).hexdigest()[:24]


# ------------------------------------------------------------------------ printing

_DAY_SET_NAMES = {days: name for name, days in DAY_CONSTS.items()}


def node_text(n: Node, brief: bool = False) -> str:
    """Canonical source text of `n`, minimally parenthesised (printer.PREC), that parses back
    to the same structure. It depends only on `structure(n)`, so expressions that share a
    cell print alike: constants print from their values, `now >= 22:00` as a time range,
    `today == sat` as a set of days. `brief` leaves out block bodies (`cascade{…}`)."""

    def sub(c: Node, min_prec: int) -> str:
        t = node_text(c, brief)
        return f"({t})" if node_prec(c) < min_prec else t

    def infix(a: Node, op: str, b: Node, p: int) -> str:
        # left-associative: `a - (b - c)` keeps its parentheses
        return f"{sub(a, p)} {op} {sub(b, p + 1)}"

    cmp = PREC["cmp"]
    match n:
        case Error(label=label):
            return label
        case Const(type=t, value=v):
            return const_text(t, v)
        case Now():
            return "now"
        case Today():
            return "today"
        case Read(cell=c):
            return cell_text(c, brief)
        case Not(operand=o):
            return f"not {sub(o, PREC['not'])}"
        case Neg(operand=o):
            return f"-{sub(o, PREC['neg'])}"
        case Scale(operand=o):
            return node_text(o, brief)  # `%` against a brightness: shown as written
        case BoolOp(op=op, left=a, right=b):
            if (sugar := _sugar(n)) is not None:
                word, x, d = sugar
                return f"{sub(x, cmp)} {word} {sub(d, PREC['??'])}"
            return infix(a, op, b, PREC[op])
        case Arith(op=op, left=a, right=b):
            return infix(a, op, b, PREC[op])
        case Coalesce(left=a, right=b):
            return infix(a, "??", b, PREC["??"])
        case Compare(op=op, left=a, right=b):
            # comparisons don't chain, so neither side may be a bare comparison
            return f"{sub(a, cmp + 1)} {op} {sub(b, cmp + 1)}"
        case IfElse(cond=c, then=a, otherwise=b):
            # right-associative: only the else branch may be a bare ternary
            p = PREC["ternary"]
            return f"{sub(a, p + 1)} if {sub(c, p + 1)} else {sub(b, p)}"
        case InList(operand=o, items=items, negate=neg):
            listed = ", ".join(node_text(i, brief) for i in items)
            return f"{sub(o, cmp + 1)} {'not in' if neg else 'in'} [{listed}]"
        case InTimeRange(start=lo, end=hi, negate=neg):
            if not neg and hi == MIDNIGHT and lo != MIDNIGHT:
                return f"now >= {lo!r}"
            if not neg and lo == MIDNIGHT:
                return f"now < {hi!r}"
            return f"now {'not in' if neg else 'in'} {lo!r}..{hi!r}"
        case DayIn(days=days, negate=neg):
            name = _DAY_SET_NAMES.get(days.days)
            if name is not None:
                return f"today {'!=' if neg else '=='} {name}"
            listed = ", ".join(DAY_NAMES[d] for d in sorted(days.days))
            return f"today {'not in' if neg else 'in'} [{listed}]"
        case Age(cell=c):
            return f"age({cell_text(c, brief)})"
        case Fresh(cell=c):
            return f"fresh({cell_text(c, brief)})"
        case Previous(cell=c):
            return f"previous({cell_text(c, brief)})"
        case Available(cell=c):
            return f"available({cell_text(c, brief)})"
        case OnValue(params=ps):
            return "on(" + ", ".join(f"{k}: {node_text(v, brief)}" for k, v in ps) + ")"
        case Cascade(arms=arms):
            return _block("cascade", arms, brief)
        case Latest(arms=arms):
            return _block("latest", arms, brief)
    raise TypeError(f"can't print {type(n).__name__}")


def node_prec(n: Node) -> int:
    """How tightly `node_text(n)` binds (printer.PREC)."""
    match n:
        case Const(type=t, value=v):
            if isinstance(v, DailyTime):
                return _time_prec(v)
            if isinstance(v, Num) and t != T.STRING and v.lo < 0:
                return PREC["neg"]
        case Read(cell=ExprCell(expr=e)):
            return node_prec(e)
        case Read(cell=TimePointCell(daily=d)):
            return _time_prec(d)
        case Not():
            return PREC["not"]
        case Neg():
            return PREC["neg"]
        case Scale(operand=o):
            return node_prec(o)
        case BoolOp(op=op):
            return PREC["for"] if _sugar(n) is not None else PREC[op]
        case Arith(op=op):
            return PREC[op]
        case Coalesce():
            return PREC["??"]
        case Compare() | InList() | InTimeRange() | DayIn():
            return PREC["cmp"]
        case IfElse():
            return PREC["ternary"]
    return PREC["postfix"]


def const_text(t: T, v: Any) -> str:
    if v is UNKNOWN:
        return "unknown"
    if isinstance(v, Num) and v.exact and not v.moving:
        match t:
            case T.NUMBER:
                return fmt_number(v.lo)
            case T.PERCENT:
                return fmt_number(v.lo) + "%"
            case T.DURATION:
                return ("-" if v.lo < 0 else "") + fmt_duration(abs(v.lo))
    match t:
        case T.BOOL:
            return "on" if v else "off"
        case T.STRING:
            return fmt_string(v)
        case T.TIMEOFDAY:
            return repr(v)
        case T.DAY:
            name = _DAY_SET_NAMES.get(v.days)
            return name if name is not None else repr(v)
        case T.KEEP:
            return "keep"
    return repr(v)


def cell_text(c: CellDef, brief: bool = False) -> str:
    if brief and isinstance(c, ExprCell):
        return node_text(c.expr, brief)
    return c.text


def arm_text(a: ArmIR | GroupIR, brief: bool = False) -> str:
    """`cond -> result`; a group prints as `guard | arm | arm`, as printer.arm_source does."""
    if isinstance(a, GroupIR):
        return f"{node_text(a.guard, brief)} | " + " | ".join(arm_text(s, brief) for s in a.arms)
    cond = "else" if a.cond is None else node_text(a.cond, brief)
    return f"{cond} -> {node_text(a.result, brief)}"


def _block(word: str, arms: list[ArmIR | GroupIR], brief: bool) -> str:
    if brief:
        return word + "{…}"
    # the `else -> keep` added to a `latest` binding wasn't written, so it isn't printed
    written = [a for a in arms if not (isinstance(a, ArmIR) and a.implicit)]
    return word + "{" + "; ".join(arm_text(a) for a in written) + "}"


def _time_prec(t: DailyTime) -> int:
    return PREC["+"] if t.offset else PREC["postfix"]


def _sugar(n: BoolOp) -> tuple[str, Node, Const] | None:
    """`x for D` and `x within D` (D11), recognised from their expansions, `x and age(x) >= D`
    and `x or age(x) < D`, which print as the sugar however they were written."""
    x, c = n.left, n.right
    if not (isinstance(x, Read) and isinstance(c, Compare) and isinstance(c.left, Age)):
        return None
    d = c.right
    if c.left.cell is not x.cell or not (isinstance(d, Const) and d.type == T.DURATION):
        return None
    if not (isinstance(d.value, Num) and d.value.lo > 0):
        return None
    if n.op == "and" and c.op == ">=":
        return "for", x, d
    if n.op == "or" and c.op == "<":
        return "within", x, d
    return None


def _unique(cells: list[CellDef]) -> list[CellDef]:
    seen: set[int] = set()
    out = []
    for c in cells:
        if id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def children(n: Node) -> list[Node]:
    match n:
        case Not(operand=o) | Neg(operand=o) | Scale(operand=o):
            return [o]
        case BoolOp(left=a, right=b) | Arith(left=a, right=b) | Compare(left=a, right=b):
            return [a, b]
        case Coalesce(left=a, right=b):
            return [a, b]
        case IfElse(cond=c, then=t, otherwise=o):
            return [c, t, o]
        case InList(operand=o, items=items):
            return [o, *items]
        case OnValue(params=ps):
            return [v for _, v in ps]
        case Cascade(arms=arms) | Latest(arms=arms):
            out: list[Node] = []
            for a in arms:
                out.extend(arm_nodes(a))
            return out
    return []


def arm_nodes(a: ArmIR | GroupIR) -> list[Node]:
    if isinstance(a, GroupIR):
        out = [a.guard]
        for s in a.arms:
            out.extend(arm_nodes(s))
        return out
    return ([a.cond] if a.cond is not None else []) + [a.result]


def node_cells(n: Node) -> list[CellDef]:
    """Cells read directly by an expression (not looking inside other cells)."""
    out: list[CellDef] = []

    def visit(x: Node) -> None:
        match x:
            case Read(cell=c) | Age(cell=c) | Previous(cell=c) | Available(cell=c):
                out.append(c)
            case Fresh(cell=c, machine=m):
                out.append(c)
                out.append(m)
            case InTimeRange(sun=sun) if sun is not None:
                out.append(sun)
        for ch in children(x):
            visit(ch)

    visit(n)
    return _unique(out)


def arm_cells(a: ArmIR | GroupIR) -> list[CellDef]:
    out: list[CellDef] = []
    for n in arm_nodes(a):
        out.extend(node_cells(n))
    return out


def is_constant(n: Node) -> bool:
    """A constant, or a constant scaled (`50%` against a brightness), so it has no inputs."""
    return isinstance(n, Const) or (isinstance(n, Scale) and is_constant(n.operand))


def uses_now(n: Node) -> bool:
    """Whether evaluating `n` depends on the clock (so it needs wake-ups)."""
    if isinstance(n, (Now, Today, InTimeRange, DayIn, Age, Fresh)):
        return True
    return any(uses_now(c) for c in children(n))
