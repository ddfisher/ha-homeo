"""Static types (§3, I14)."""

from __future__ import annotations

from enum import Enum


class T(Enum):
    BOOL = "bool"
    NUMBER = "number"
    PERCENT = "percentage"  # a number written with `%`; usable wherever a number is
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
    if src == T.PERCENT and dst == T.NUMBER:
        return True
    if src == T.BOOL and dst == T.ONSPEC:  # `off`/`on` where on(...) is accepted
        return True
    return False


def numeric_result(op: str, a: T, b: T) -> T:
    """The type of `a op b` for two numbers: a percentage stays one if it comes first in `*`
    or `/` (`50% * 2`), or meets another in `+`/`-`. Anything else is a plain number."""
    return T.PERCENT if a == T.PERCENT and (b == T.PERCENT or op in "*/") else T.NUMBER


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
    if {a, b} == {T.NUMBER, T.PERCENT}:
        return T.NUMBER
    if {a, b} == {T.BOOL, T.ONSPEC}:
        return T.ONSPEC
    return None


def article(t: T) -> str:
    s = str(t)
    return ("an " if s[0] in "aeiou" else "a ") + s
