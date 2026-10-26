"""AST for .homeo files. Every node has a `span` for error messages."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .errors import Span

# ---------------------------------------------------------------- expressions


@dataclass(slots=True)
class Expr:
    span: Span


@dataclass(slots=True)
class Name(Expr):
    id: str


@dataclass(slots=True)
class Attr(Expr):
    """`base.name`, or `base["name"]` when `quoted`."""

    base: Expr
    name: str
    quoted: bool = False


@dataclass(slots=True)
class BoolLit(Expr):
    value: bool
    word: str  # "on", "off", "true", "false"


@dataclass(slots=True)
class NumberLit(Expr):
    value: float
    percent: bool = False


@dataclass(slots=True)
class DurationLit(Expr):
    seconds: float


@dataclass(slots=True)
class TimeLit(Expr):
    seconds: int  # since local midnight


@dataclass(slots=True)
class StringLit(Expr):
    value: str


@dataclass(slots=True)
class KeepLit(Expr):
    pass


@dataclass(slots=True)
class ListExpr(Expr):
    items: list[Expr]


@dataclass(slots=True)
class Unary(Expr):
    op: str  # "not", "-"
    operand: Expr


@dataclass(slots=True)
class Binary(Expr):
    op: str  # and or + - * / == != < > <= >= ?? in "not in"
    left: Expr
    right: Expr


@dataclass(slots=True)
class Range(Expr):
    """`lo..hi`; only valid as the right side of `in`."""

    lo: Expr
    hi: Expr


@dataclass(slots=True)
class Ternary(Expr):
    then: Expr
    cond: Expr
    otherwise: Expr


@dataclass(slots=True)
class For(Expr):
    """`x for D` (sugar for `x and age(x) >= D`, D11)."""

    operand: Expr
    duration: Expr


@dataclass(slots=True)
class Within(Expr):
    """`x within D` (sugar for `x or age(x) < D`, D11): true now, or until less than D ago."""

    operand: Expr
    duration: Expr


@dataclass(slots=True)
class Every(Expr):
    period: Expr


@dataclass(slots=True)
class Kwarg:
    name: str
    value: Expr
    span: Span


@dataclass(slots=True)
class Call(Expr):
    func: Expr  # Name, Attr chain
    args: list[Expr]
    kwargs: list[Kwarg]


@dataclass(slots=True)
class OnCall(Expr):
    """`on(brightness: 80%, kelvin: 2700)`."""

    kwargs: list[Kwarg]


# ------------------------------------------------------------------- arm blocks


@dataclass(slots=True)
class Arm:
    """`cond -> result`; `cond is None` for `else`."""

    cond: Expr | None
    result: Expr
    span: Span


@dataclass(slots=True)
class Group:
    """A guard with `|` sub-arms."""

    guard: Expr
    arms: list[Arm | Group]
    span: Span


@dataclass(slots=True)
class Cascade(Expr):
    arms: list[Arm | Group]


@dataclass(slots=True)
class Latest(Expr):
    arms: list[Arm | Group]


# ------------------------------------------------------------------- statements


@dataclass(slots=True)
class Stmt:
    span: Span


@dataclass(slots=True)
class Let(Stmt):
    name: str
    name_span: Span
    value: Expr


@dataclass(slots=True)
class Bind(Stmt):
    target: Expr
    value: Expr


@dataclass(slots=True)
class StateDecl(Stmt):
    name: str
    name_span: Span
    initial: Expr
    arms: list[Arm | Group]


@dataclass(slots=True)
class Action(Stmt):
    trigger: Expr
    guard: Expr | None
    body: list[Call]


@dataclass(slots=True)
class File:
    name: str
    statements: list[Stmt]
    directives: dict[str, tuple[str, Span]] = field(default_factory=dict)


def walk(node: object):
    """Yield all Expr nodes under `node` (pre-order), including inside arms and kwargs."""
    if isinstance(node, list):
        for x in node:
            yield from walk(x)
        return
    if isinstance(node, Expr):
        yield node
    if not dataclasses.is_dataclass(node):
        return
    for f in dataclasses.fields(node):
        if f.name == "span":
            continue
        child = getattr(node, f.name)
        if isinstance(child, (Expr, Arm, Group, Kwarg, list)):
            yield from walk(child)
