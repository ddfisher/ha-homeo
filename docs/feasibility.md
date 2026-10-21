# Feasibility: reactive DSL → Home Assistant

> **Superseded direction:** we've since decided on a custom integration (installed via HACS) that
> watches rule files and compiles a custom DSL on the fly. See [`design.md`](design.md). This doc
> remains as the record of the feasibility work and the pyscript/AppDaemon/library comparison.

**Verdict: feasible, and a plain Python library works. No transpiler is needed.** The core idea
(`fan.bath = (sensor.co2 > 800) | (sensor.humidity > 50)`, kept in sync on every transition) runs
as an embedded DSL using operator overloading. The library approach changes which runtime to host
it on: pyscript can't evaluate it (see §3).

## 1. Library approach (current direction): `spike/library/`

```python
from ha_dsl import *

fan.bathroom = (sensor.co2 > 800) | (sensor.humidity > 50 * percent)
fan.bedroom  = (sensor.co2 > 800).until(sensor.co2 < 700)      # deadband / hysteresis
light.porch  = (sensor.lux < 20).for_(5 * minutes)             # must hold for 5 min
with debounce(2 * minutes):                                    # applies to every binding inside
    switch.dehumidifier = sensor.humidity > 60
light.hall.brightness = when(binary_sensor.motion, 255, 0)     # attribute target
```

How the spike works:

- **Bindings.** `fan`, `sensor`, … are domain proxies. `fan.bathroom = expr` goes through
  `__setattr__` and registers a binding. Expressions build a signal graph at import time.
- **Runtime.** A host-agnostic `Runtime` re-evaluates a binding when one of its input entities
  changes or a timer it asked for fires. It writes only when the result changes (edge-triggered,
  so manual overrides persist).
- **Unknown inputs.** It uses three-valued logic: `True | UNKNOWN` is `True`, while
  `UNKNOWN & True` holds the target.

`python3 spike/library/demo.py` runs these rules against a fake HA with a simulated clock:

```
12:00  sensor.co2 = unavailable     -> (nothing)
12:00  sensor.humidity = 65         -> turn_on(fan.bathroom)       # True | UNKNOWN
12:00  sensor.co2 = 750             -> turn_off(fan.bedroom)
12:00  sensor.co2 = 850             -> turn_on(fan.bedroom)
12:00  fan.bathroom = off           -> (nothing)                   # manual override kept
12:00  sensor.co2 = 750             -> (nothing)                   # bedroom: inside deadband
12:00  sensor.co2 = 650             -> turn_off(fan.bedroom)
12:00  sensor.lux = 10              -> turn_off(light.porch)
12:03  +0:03:00                     -> turn_on(switch.dehumidifier)  # debounced 2 min
12:06  +0:03:00                     -> turn_on(light.porch)          # held 5 min
12:06  binary_sensor.motion = on    -> set(light.hall.brightness=255)
```

### Syntax: what Python allows

| Wanted | Works? | Use instead |
|---|---|---|
| `a \| b`, `a & b`, `~a` | ✅ | |
| `5.minutes`, `750.debounce(50)` | ❌ `5.m` is a malformed float literal, and `int` can't be monkeypatched | `5 * minutes`. `minutes` is a `timedelta`, so this is real duration arithmetic. |
| `50%` / `50.percent` | ❌ | `50 * percent`, or just `50` |
| `and` / `or` / `not` / `x if c else y` | ❌ not overloadable | `&`, `\|`, `~`, `when(c, x, y)` |
| `sensor.mode in ("a", "b")` | ❌ `in` always returns a plain bool | a method, e.g. `sensor.mode.is_in("a", "b")` |
| `with debounce(...)` / `with min_on(...)` | ✅ | |

On debounce syntax, two different things are usually meant, and both read well as methods on the
condition rather than on the number:

- **Time:** `(sensor.co2 > 750).for_(5 * minutes)` means "true once it has been true for 5 min".
  Use `with debounce(...)` for symmetric settling of a binding's output.
- **Value (deadband):** `(sensor.co2 > 800).until(sensor.co2 < 700)` means "on above 800,
  off below 700". A more compact alternative that is also possible:
  `sensor.co2 > deadband(750, 50)`, where `Signal.__gt__` recognises the deadband object.

### The big hazard: precedence, and how the library catches it

`&` and `|` bind tighter than `>`. So `sensor.co2 > 800 | sensor.humidity > 50` silently means
`co2 > (800 | humidity) > 50`, and every comparison needs parentheses (the same gotcha pandas has).
The spike catches every variant it tried at load time:

- **`__bool__` raises with a helpful message.** That catches `and`, `or`, `not`, `if` and
  chained comparisons.
- **`&`, `|`, `~` reject numeric operands.** That catches `binary_sensor.motion | sensor.lux < 20`,
  which isn't a chained comparison, so it would otherwise be silently wrong.
- **Bindings are checked.** Self-dependency and double binding are rejected.

### Trade-offs vs a transpiler

Library pros:
- No compiler to build.
- Real tracebacks pointing at the user's own lines.
- Ordinary Python for abstraction, e.g. loops and functions that generate rules per room.
- Entity typos can be caught by a type checker or IDE, using a stub generated from HA's entity list.

Library cons:
- Mandatory parentheses, `~`/`when()` instead of `not`/`if`, `5 * minutes`.
- Magic proxies: any attribute assignment on an entity is a binding. The spike hit this once
  internally (an innocent `x.kind = …` created a stray rule). Keep library internals off those
  objects.

## 2. Alternative considered: transpiler (`spike/transpiler/`)

`ast.parse` accepts `fan.bath = sensor.co2 > 800 or sensor.humidity > 50` directly. Rewriting the
AST gives you `and`/`or`/`not`/`if` without parentheses, but you have to build a compiler and map
errors back to source lines. `spike/transpiler/compile.py` emits one pyscript `@state_trigger`
rule per binding, and `simulate.py` runs it against a fake HA. The library spike now covers
everything this one does.

## 3. Runtime host

A subagent compared pyscript and AppDaemon, assuming generated code. It preferred **pyscript**:
- it runs in-process, with synchronous reads and `Context` propagation for override detection;
- it installs from HACS and reloads automatically;
- it has active 2026 releases (2.1.0, Aug 2026).

AppDaemon runs real CPython, but in a separate process whose state cache lags its own writes, and
it restarts every app on a reconnect.

**The library approach changes this.** pyscript runs `.py` files through its own AST interpreter,
and that interpreter collapses every comparison to a plain bool. `ast_compare` in
`custom_components/pyscript/eval.py` does `if not val: return False … return True`. So
`sensor.co2 > 800` can never yield a signal object inside a pyscript file; with this library it
raises the `__bool__` error. (`|`, `&` and attribute assignment do go through normal Python
semantics when `fan`/`sensor` are bound names.)

Options, in order of preference:

1. **Custom HA integration.**
   - Runs real CPython inside HA's event loop.
   - Can use HA's own `async_track_state_change_event` / `async_track_point_in_time` for the host
     adapter, plus native `Context`, `RestoreEntity` (to persist latch/override state) and the
     official pytest harness.
   - Cost: loading and reloading the rules file and reporting errors are ours to build.
   - Recommended target.
2. **AppDaemon app.** Real CPython; the rules file is just an app. It's the fastest way to try the
   library against a real HA, with the process-separation caveats above.
3. **pyscript with the rules in a native module.** The rules go in a real Python module imported
   with `allow_all_imports`, and the pyscript file is a thin host adapter. This works, but it
   gives up most of what made pyscript attractive.

## 4. Open design questions

1. **Overrides.** The spike is edge-triggered, so a manual change lasts until the next flip.
   Alternatives: back off for a fixed time after a human change, or re-assert continuously.
   Detection needs `Context` id tracking.
2. **Startup.** The spike writes the first known value at boot, which can undo a manual change
   made before the restart. Persisting `last` (`RestoreEntity`) would avoid that.
3. **Time windows.** `between("22:00", "06:00")` and sun events need wake-ups at the boundaries.
   The `wake_at` mechanism used by `.for_()` already supports this.
4. **Non-boolean targets.** Brightness, setpoints and covers need a writer per domain that maps a
   value to a service call.
5. **Events / TEA part.** Button presses aren't derived state. They need an `on(event)` handler
   escape hatch, or first-class modes/state machines whose state is itself a signal.
6. **Static checks.** Cycles across bindings, and unknown entity ids checked against HA's entity
   registry at load.

## 5. Suggested next step

Turn `spike/library/ha_dsl.py` into a real package with unit tests (fake host plus simulated
clock). Then write a minimal custom-integration host adapter, or an AppDaemon one for a quicker
first test on real hardware.
