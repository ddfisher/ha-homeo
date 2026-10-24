"""Resolved, typed program representation produced by the checker and run by the runtime.

Expressions (`Node`) are pure functions of cells and the clock. **Cells** (`CellDef`) are the
things whose history the runtime tracks (implementation plan §2.2). Every node and cell has a
canonical `text`, built from resolved names (entity ids instead of `let` aliases), which is
used for messages, `homeo.explain`, and as the key that carries cell state across reloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import Span
from .types import T

# ------------------------------------------------------------------ expressions


@dataclass(eq=False, slots=True)
class Node:
    type: T
    span: Span
    text: str
    moving: bool = False  # value changes continuously with the clock


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
    """`now in start..end` (I3): start occurred more recently than end. A bound is a fixed
    DailyTime, or a node giving one at run time (a time-of-day entity, maybe shifted; I24)."""

    start: Any = None  # DailyTime | Node
    end: Any = None  # DailyTime | Node
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
    text: str
    source: str = ""  # as written (with `let` names), for homeo.explain
    cond_source: str = "else"
    implicit: bool = False  # not written: `else -> keep` added to a `latest` binding


@dataclass(eq=False, slots=True)
class GroupIR:
    guard: Node
    arms: list[ArmIR | GroupIR]
    span: Span
    text: str
    source: str = ""  # the guard as written


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
    daily: Any = None  # DailyTime, or a node giving one (a time-of-day entity; I24)
    sun: CellDef | None = None  # set for solar times

    def deps(self) -> list[CellDef]:
        out = [self.sun] if self.sun is not None else []
        if isinstance(self.daily, Node):
            out += node_cells(self.daily)
        return out


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
        case InTimeRange(start=a, end=b):
            return [x for x in (a, b) if isinstance(x, Node)]
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


def uses_now(n: Node) -> bool:
    """Whether evaluating `n` depends on the clock (so it needs wake-ups)."""
    if isinstance(n, (Now, Today, InTimeRange, DayIn, Age, Fresh)):
        return True
    return any(uses_now(c) for c in children(n))
