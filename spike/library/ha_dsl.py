"""Feasibility spike: the DSL as a plain Python library (no transpiler).

    from ha_dsl import *

    fan.bathroom = (sensor.co2 > 800) | (sensor.humidity > 50 * percent)

    with debounce(2 * minutes):
        light.porch = (sensor.lux < 20).for_(5 * minutes)

    fan.bedroom = (sensor.co2 > 800).until(sensor.co2 < 700)   # deadband

Expressions build a signal graph at import time. A host-independent Runtime
re-evaluates a binding whenever one of its input entities changes (or a timer
it asked for fires) and writes to the target only when the result changes.
"""

from datetime import timedelta

# ---------------------------------------------------------------- units

seconds = timedelta(seconds=1)
minutes = timedelta(minutes=1)
hours = timedelta(hours=1)
percent = 1  # cosmetic: `50 * percent` == 50, matching how HA reports humidity


class _Unknown:
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()  # an input is unknown/unavailable

BOOL_DOMAINS = {"binary_sensor", "input_boolean", "switch", "light", "fan", "lock"}
NUM_DOMAINS = {"sensor", "input_number"}


# ---------------------------------------------------------------- signals

class Signal:
    """A value derived from entity states. `kind` is 'bool', 'num' or 'any'."""

    kind = "any"

    def deps(self):
        return set()

    def eval(self, ctx):
        raise NotImplementedError

    # `and`, `or`, `not`, `if` and chained comparisons all call __bool__.
    # Refuse loudly at load time instead of silently picking a branch.
    def __bool__(self):
        raise TypeError(
            "A DSL expression was used where Python needs a plain True/False. "
            "Use `&`, `|`, `~` instead of `and`, `or`, `not`; when(c, a, b) "
            "instead of `a if c else b`; and parenthesise comparisons: "
            "`(a > 1) | (b > 2)`, since `a > 1 | b > 2` means `a > (1 | b) > 2`."
        )

    def _cmp(op, sym):
        def method(self, other):
            return Op(sym, op, [self, lift(other)], kind="bool")
        return method

    __gt__ = _cmp(lambda a, b: a > b, ">")
    __ge__ = _cmp(lambda a, b: a >= b, ">=")
    __lt__ = _cmp(lambda a, b: a < b, "<")
    __le__ = _cmp(lambda a, b: a <= b, "<=")
    __eq__ = _cmp(lambda a, b: a == b, "==")
    __ne__ = _cmp(lambda a, b: a != b, "!=")
    __hash__ = object.__hash__

    def _arith(op, sym):
        def method(self, other):
            return Op(sym, op, [self, lift(other)], kind="num")
        def rmethod(self, other):
            return Op(sym, op, [lift(other), self], kind="num")
        return method, rmethod

    __add__, __radd__ = _arith(lambda a, b: a + b, "+")
    __sub__, __rsub__ = _arith(lambda a, b: a - b, "-")
    __mul__, __rmul__ = _arith(lambda a, b: a * b, "*")
    __truediv__, __rtruediv__ = _arith(lambda a, b: a / b, "/")

    def __and__(self, other):
        return Logic("&", [self, _need_bool(other, "&")], all)

    def __or__(self, other):
        return Logic("|", [self, _need_bool(other, "|")], any)

    __rand__, __ror__ = __and__, __or__

    def __invert__(self):
        return Not(_need_bool(self, "~"))

    # stateful operators
    def for_(self, duration):
        """True once this has been continuously true for `duration`."""
        return HeldFor(_need_bool(self, ".for_()"), duration)

    def until(self, off):
        """Latch: becomes true when self is true, stays true until `off` is true."""
        return Latch(_need_bool(self, ".until()"), _need_bool(off, ".until()"))


def lift(x):
    return x if isinstance(x, Signal) else Const(x)


def _need_bool(x, op):
    x = lift(x)
    if x.kind == "num":
        raise TypeError(
            f"`{op}` needs a true/false operand but got numeric `{x!r}`. "
            "Probably missing parentheses: `a | b < 20` means `(a | b) < 20`."
        )
    return x


class Const(Signal):
    def __init__(self, v):
        self.v = v
        self.kind = "bool" if isinstance(v, bool) else "num" if isinstance(v, (int, float)) else "any"

    def eval(self, ctx):
        return self.v

    def __repr__(self):
        return repr(self.v)


class Op(Signal):
    def __init__(self, sym, fn, args, kind):
        self.sym, self.fn, self.args, self.kind = sym, fn, args, kind

    def deps(self):
        return set().union(*(a.deps() for a in self.args))

    def eval(self, ctx):
        vals = [a.eval(ctx) for a in self.args]
        if any(v is UNKNOWN for v in vals):
            return UNKNOWN
        return self.fn(*vals)

    def __repr__(self):
        return f"({self.args[0]!r} {self.sym} {self.args[1]!r})"


class Logic(Signal):
    """Three-valued (Kleene) and/or: `False & UNKNOWN` is False, `True | UNKNOWN` is True."""

    kind = "bool"

    def __init__(self, sym, args, combine):
        self.sym, self.args, self.combine = sym, args, combine

    def deps(self):
        return set().union(*(a.deps() for a in self.args))

    def eval(self, ctx):
        vals = [a.eval(ctx) for a in self.args]
        decisive = self.combine is any  # True decides `|`, False decides `&`
        if any(v is not UNKNOWN and bool(v) == decisive for v in vals):
            return decisive
        if any(v is UNKNOWN for v in vals):
            return UNKNOWN
        return not decisive

    def __repr__(self):
        return f"({self.args[0]!r} {self.sym} {self.args[1]!r})"


class Not(Signal):
    kind = "bool"

    def __init__(self, a):
        self.a = a

    def deps(self):
        return self.a.deps()

    def eval(self, ctx):
        v = self.a.eval(ctx)
        return UNKNOWN if v is UNKNOWN else not v


def when(cond, then, else_):
    """`when(c, a, b)` is the DSL's `a if c else b`."""
    cond, then, else_ = _need_bool(cond, "when()"), lift(then), lift(else_)

    def pick(c, a, b):
        return a if c else b
    return Op("when", pick, [cond, then, else_], kind=then.kind)


class HeldFor(Signal):
    kind = "bool"

    def __init__(self, a, duration):
        self.a, self.duration, self.since = a, duration, None

    def deps(self):
        return self.a.deps()

    def eval(self, ctx):
        v = self.a.eval(ctx)
        if v is not True:
            self.since = None
            return v  # False or UNKNOWN
        if self.since is None:
            self.since = ctx.now
        if ctx.now - self.since >= self.duration:
            return True
        ctx.wake_at(self.since + self.duration)
        return False


class Latch(Signal):
    kind = "bool"

    def __init__(self, on, off):
        self.on, self.off, self.state = on, off, UNKNOWN

    def deps(self):
        return self.on.deps() | self.off.deps()

    def eval(self, ctx):
        on, off = self.on.eval(ctx), self.off.eval(ctx)
        if on is True:
            self.state = True
        elif off is True:
            self.state = False
        elif self.state is UNKNOWN and on is False and off is False:
            self.state = False  # inside the deadband at startup: default off
        return self.state


# ---------------------------------------------------------------- entities

class Entity(Signal):
    def __init__(self, entity_id, attr=None):
        domain = entity_id.split(".")[0]
        d = self.__dict__
        d["entity_id"], d["attr"] = entity_id, attr
        d["kind"] = "any" if attr else "bool" if domain in BOOL_DOMAINS else "num" if domain in NUM_DOMAINS else "any"

    def deps(self):
        return {self.entity_id}

    def eval(self, ctx):
        return ctx.read(self.entity_id, self.attr)

    def __getattr__(self, attr):
        return Entity(self.entity_id, attr)  # light.hall.brightness

    def __setattr__(self, attr, expr):
        _bind(Entity(self.entity_id, attr), expr)  # light.hall.brightness = ...

    def __repr__(self):
        return self.entity_id + (f".{self.attr}" if self.attr else "")


class Domain:
    def __init__(self, name):
        self.__dict__["_name"] = name

    def __getattr__(self, obj):
        return Entity(f"{self._name}.{obj}")

    def __setattr__(self, obj, expr):
        _bind(Entity(f"{self._name}.{obj}"), expr)  # fan.bathroom = ...


binary_sensor, sensor, input_boolean, input_number = map(
    Domain, ["binary_sensor", "sensor", "input_boolean", "input_number"])
switch, light, fan, lock, climate, cover = map(
    Domain, ["switch", "light", "fan", "lock", "climate", "cover"])


# ---------------------------------------------------------------- bindings

class Binding:
    def __init__(self, target, expr, debounce):
        self.target, self.expr, self.debounce = target, expr, debounce
        self.last = UNKNOWN          # last value written
        self.pending = None          # (value, since) while debouncing

    def __repr__(self):
        return f"{self.target!r} = {self.expr!r}"


BINDINGS = []
_options = [{"debounce": None}]


def _bind(target, expr):
    if any(b.target.entity_id == target.entity_id and b.target.attr == target.attr for b in BINDINGS):
        raise ValueError(f"{target!r} is bound twice")
    expr = lift(expr)
    if target.entity_id in expr.deps():
        raise ValueError(f"{target!r} depends on itself")
    BINDINGS.append(Binding(target, expr, _options[-1]["debounce"]))


class debounce:
    """`with debounce(30 * seconds):` a binding's result must hold this long before it is written."""

    def __init__(self, duration):
        self.duration = duration

    def __enter__(self):
        _options.append({**_options[-1], "debounce": self.duration})

    def __exit__(self, *exc):
        _options.pop()


# ---------------------------------------------------------------- runtime

class Runtime:
    """Host-agnostic engine. A host adapter (pyscript, AppDaemon, a custom
    integration, or a test fake) provides read/write/now/schedule and calls
    on_change() when an entity changes."""

    def __init__(self, host, bindings=None):
        self.host = host
        self.bindings = list(BINDINGS if bindings is None else bindings)
        self.by_dep = {}
        for b in self.bindings:
            for dep in b.expr.deps():
                self.by_dep.setdefault(dep, []).append(b)

    def start(self):
        for b in self.bindings:
            self.evaluate(b)

    def on_change(self, entity_id):
        for b in self.by_dep.get(entity_id, []):
            self.evaluate(b)

    def evaluate(self, b):
        ctx = _Ctx(self, b)
        value = b.expr.eval(ctx)
        now = self.host.now()
        if value is UNKNOWN:
            return  # hold
        if value == b.last:
            b.pending = None
            return  # result unchanged: don't fight manual overrides
        if b.debounce:
            if b.pending is None or b.pending[0] != value:
                b.pending = (value, now)
            if now - b.pending[1] < b.debounce:
                self.host.schedule(b.pending[1] + b.debounce, lambda: self.evaluate(b))
                return
        b.pending = None
        b.last = value
        self.host.write(b.target.entity_id, b.target.attr, value)


class _Ctx:
    def __init__(self, rt, binding):
        self.rt, self.binding, self.now = rt, binding, rt.host.now()

    def read(self, entity_id, attr):
        return self.rt.host.read(entity_id, attr)

    def wake_at(self, when):
        b = self.binding
        self.rt.host.schedule(when, lambda: self.rt.evaluate(b))


__all__ = [
    "seconds", "minutes", "hours", "percent", "when", "debounce", "UNKNOWN",
    "binary_sensor", "sensor", "input_boolean", "input_number",
    "switch", "light", "fan", "lock", "climate", "cover",
]
