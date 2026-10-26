"""Turn AST back into text.

- `to_source` gives canonical, minimally parenthesised source text, for messages and
  `homeo.explain`. Runtime state is keyed by structure instead (`ir.structure`).
- `sexpr` gives a fully parenthesised form, handy for tests.

`PREC` is the grammar's precedence levels (higher binds tighter). The checker builds IR display
text with the same table, so both print the way the parser reads.
"""

from __future__ import annotations

from . import ast as A

PREC = {
    "ternary": 1,
    "or": 2,
    "and": 3,
    "not": 4,
    "for": 5,
    "cmp": 6,
    "??": 7,
    "+": 8,
    "-": 8,
    "*": 9,
    "/": 9,
    "neg": 10,
    "postfix": 11,
}
_CMP = {"==", "!=", "<", ">", "<=", ">=", "in", "not in"}


def fmt_duration(seconds: float) -> str:
    if seconds == 0:
        return "0s"
    parts = []
    rest = seconds
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if rest >= size:
            n = int(rest // size)
            parts.append(f"{n}{unit}")
            rest -= n * size
    if rest or not parts:
        r = round(rest, 3)
        parts.append(f"{int(r) if r == int(r) else r}s")
    return "".join(parts)


def fmt_time(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}" + (f":{s:02d}" if s else "")


def fmt_number(v: float) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _prec(e: A.Expr) -> int:
    if isinstance(e, A.Ternary):
        return PREC["ternary"]
    if isinstance(e, A.Binary):
        if e.op in _CMP:
            return PREC["cmp"]
        return PREC[e.op]
    if isinstance(e, A.Unary):
        return PREC["not"] if e.op == "not" else PREC["neg"]
    if isinstance(e, (A.For, A.Within)):
        return PREC["for"]
    if isinstance(e, A.Range):
        return PREC["cmp"]
    return PREC["postfix"]


def to_source(e: A.Expr) -> str:
    def sub(child: A.Expr, min_prec: int) -> str:
        text = to_source(child)
        return f"({text})" if _prec(child) < min_prec else text

    match e:
        case A.Name(id=i):
            return i
        case A.Attr(base=b, name=n, quoted=q):
            return f'{sub(b, PREC["postfix"])}["{n}"]' if q else f"{sub(b, PREC['postfix'])}.{n}"
        case A.BoolLit(word=w):
            return w
        case A.NumberLit(value=v, percent=p):
            return fmt_number(v) + ("%" if p else "")
        case A.DurationLit(seconds=s):
            return fmt_duration(s)
        case A.TimeLit(seconds=s):
            return fmt_time(s)
        case A.StringLit(value=v):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        case A.KeepLit():
            return "keep"
        case A.ListExpr(items=items):
            return "[" + ", ".join(to_source(i) for i in items) + "]"
        case A.Unary(op="not", operand=o):
            return "not " + sub(o, PREC["not"])
        case A.Unary(op=op, operand=o):
            return op + sub(o, PREC["neg"])
        case A.Binary(op=op, left=l, right=r):
            p = _prec(e)
            if op in ("in", "not in") and isinstance(r, A.Range):
                return f"{sub(l, p + 1)} {op} {to_source(r)}"
            # left-associative: the right operand needs strictly higher precedence;
            # comparisons don't chain, so the left one does too
            return f"{sub(l, p + 1 if op in _CMP else p)} {op} {sub(r, p + 1)}"
        case A.Range(lo=lo, hi=hi):
            return f"{sub(lo, PREC['??'])}..{sub(hi, PREC['??'])}"
        case A.Ternary(then=t, cond=c, otherwise=o):
            return f"{sub(t, 2)} if {sub(c, 2)} else {sub(o, 1)}"
        case A.For(operand=o, duration=d):
            return f"{sub(o, PREC['cmp'])} for {sub(d, PREC['??'])}"
        case A.Within(operand=o, duration=d):
            return f"{sub(o, PREC['cmp'])} within {sub(d, PREC['??'])}"
        case A.Every(period=p):
            return f"every {sub(p, PREC['postfix'])}"
        case A.Call(func=f, args=args, kwargs=kwargs):
            parts = [to_source(a) for a in args] + [
                f"{k.name}: {to_source(k.value)}" for k in kwargs
            ]
            return f"{to_source(f)}({', '.join(parts)})"
        case A.OnCall(kwargs=kwargs):
            if not kwargs:
                return "on()"
            return "on(" + ", ".join(f"{k.name}: {to_source(k.value)}" for k in kwargs) + ")"
        case A.Cascade(arms=arms):
            return "cascade{" + "; ".join(arm_source(a) for a in arms) + "}"
        case A.Latest(arms=arms):
            return "latest{" + "; ".join(arm_source(a) for a in arms) + "}"
    raise TypeError(f"can't print {type(e).__name__}")


def arm_source(a: A.Arm | A.Group) -> str:
    if isinstance(a, A.Group):
        return f"{to_source(a.guard)} | " + " | ".join(arm_source(s) for s in a.arms)
    cond = "else" if a.cond is None else to_source(a.cond)
    return f"{cond} -> {to_source(a.result)}"


def sexpr(e: A.Expr | A.Arm | A.Group) -> str:
    """Fully parenthesised form: `(or (> co2 800) (> humidity 60%))`."""
    match e:
        case A.Arm(cond=c, result=r):
            return f"(arm {'else' if c is None else sexpr(c)} {sexpr(r)})"
        case A.Group(guard=g, arms=arms):
            return f"(group {sexpr(g)} " + " ".join(sexpr(a) for a in arms) + ")"
        case A.Unary(op=op, operand=o):
            return f"({op} {sexpr(o)})"
        case A.Binary(op=op, left=l, right=r):
            return f"({op} {sexpr(l)} {sexpr(r)})"
        case A.Range(lo=lo, hi=hi):
            return f"(.. {sexpr(lo)} {sexpr(hi)})"
        case A.Ternary(then=t, cond=c, otherwise=o):
            return f"(if {sexpr(c)} {sexpr(t)} {sexpr(o)})"
        case A.For(operand=o, duration=d):
            return f"(for {sexpr(o)} {sexpr(d)})"
        case A.Within(operand=o, duration=d):
            return f"(within {sexpr(o)} {sexpr(d)})"
        case A.Every(period=p):
            return f"(every {sexpr(p)})"
        case A.Call(func=f, args=args, kwargs=kwargs):
            parts = [sexpr(a) for a in args] + [f"{k.name}:{sexpr(k.value)}" for k in kwargs]
            return f"(call {to_source(f)}" + "".join(" " + p for p in parts) + ")"
        case A.OnCall(kwargs=kwargs):
            return "(on" + "".join(f" {k.name}:{sexpr(k.value)}" for k in kwargs) + ")"
        case A.ListExpr(items=items):
            return "[" + " ".join(sexpr(i) for i in items) + "]"
        case A.Cascade(arms=arms):
            return "(cascade " + " ".join(sexpr(a) for a in arms) + ")"
        case A.Latest(arms=arms):
            return "(latest " + " ".join(sexpr(a) for a in arms) + ")"
    return to_source(e)
