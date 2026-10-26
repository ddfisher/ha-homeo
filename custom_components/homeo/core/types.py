"""Static types (§3, I14)."""

from __future__ import annotations

from enum import Enum


class T(Enum):
    BOOL = "bool"
    NUMBER = "number"
    PERCENT = "percentage"  # a unit of its own, like a duration (D4); `50%` is 50 of it
    STRING = "string"
    DURATION = "duration"
    TIMESTAMP = "timestamp"
    TIMEOFDAY = "time of day"
    DAY = "day"
    ONSPEC = "on(...)"
    LIST = "list"
    KEEP = "keep"
    ANY = "any"  # not known at compile time; converted at run time

    def __str__(self) -> str:
        return self.value


NUMERIC = {T.NUMBER, T.PERCENT}


def assignable(src: T, dst: T) -> bool:
    """Can a value of type `src` be used where `dst` is expected?"""
    if src == dst or src == T.ANY or dst == T.ANY:
        return True
    if src == T.BOOL and dst == T.ONSPEC:  # `off`/`on` where on(...) is accepted
        return True
    return False


# Arithmetic on numbers and percentages. Like durations, a percentage is a unit: it can be
# scaled by a number, and two of them divide to a plain number, but it never mixes with a plain
# number in `+`/`-`, and there's no percentage of a percentage (`50% * 50%`).
_NUMERIC_OPS: dict[tuple[str, T, T], T] = {
    ("+", T.NUMBER, T.NUMBER): T.NUMBER,
    ("-", T.NUMBER, T.NUMBER): T.NUMBER,
    ("*", T.NUMBER, T.NUMBER): T.NUMBER,
    ("/", T.NUMBER, T.NUMBER): T.NUMBER,
    ("+", T.PERCENT, T.PERCENT): T.PERCENT,
    ("-", T.PERCENT, T.PERCENT): T.PERCENT,
    ("*", T.PERCENT, T.NUMBER): T.PERCENT,
    ("*", T.NUMBER, T.PERCENT): T.PERCENT,
    ("/", T.PERCENT, T.NUMBER): T.PERCENT,
    ("/", T.PERCENT, T.PERCENT): T.NUMBER,
}


def numeric_result(op: str, a: T, b: T) -> T | None:
    """The type of `a op b` for numbers and percentages, or None if they don't combine. A value
    typed at run time (`any`) may be either; if that leaves more than one answer, it's `any`."""
    options = {T.ANY: (T.NUMBER, T.PERCENT)}
    results = {
        _NUMERIC_OPS.get((op, x, y)) for x in options.get(a, (a,)) for y in options.get(b, (b,))
    } - {None}
    if not results:
        return None
    return results.pop() if len(results) == 1 else T.ANY


def join(a: T, b: T) -> T | None:
    """The common type of two branches (ternary, cascade arms), or None if incompatible."""
    if a == b:
        return a
    if a == T.KEEP:
        return b
    if b == T.KEEP:
        return a
    if a == T.ANY:
        return b
    if b == T.ANY:
        return a
    if {a, b} == {T.BOOL, T.ONSPEC}:
        return T.ONSPEC
    return None


def article(t: T) -> str:
    s = str(t)
    return ("an " if s[0] in "aeiou" else "a ") + s
