# Feasibility: reactive DSL → Home Assistant

**Verdict: feasible.** The core idea (`fan.bath = sensor.co2 > 800 or sensor.humidity > 50`,
kept in sync on every transition) compiles to a small amount of pyscript. The parts that take
real work are the semantics around the core: unknown states, manual overrides, hysteresis, time,
and events. Parsing and code generation are not where the effort goes.

## 1. Syntax: reuse Python's parser

`ast.parse` already accepts the important forms:

| Snippet | Parses? |
|---|---|
| `fan.bath = sensor.co2 > 800 or sensor.humidity > 50` | ✅ entity refs are plain attribute access |
| `light.hall.brightness = 255 if binary_sensor.motion else 0` | ✅ attribute targets |
| `fan.x = sensor.t > 25 and not input_boolean.guest` | ✅ |
| `light.hall = held(binary_sensor.motion, minutes=5)` | ✅ function form |
| `sensor.humidity > 50%`, `5min` | ❌ needs a small token pre-pass for unit suffixes |
| `light.hall = motion for 5 min`, `22:00 < now < 06:00` | ❌ use the function form (`held(...)`, `between(...)`) or a pre-pass |

**Why a transpiler rather than an embedded Python DSL?** Python cannot overload `and`, `or`,
`not` or `x if c else y`. That makes `co2 > 800 or humidity > 50` impossible to capture as a
lazy expression with operator overloading. An AST rewrite gets it with no extra syntax.

## 2. Runtime target: pyscript

A subagent compared pyscript and AppDaemon in detail. Its recommendation is **pyscript**, with
**thin generated code over a hand-written runtime library**:

- **pyscript runs inside HA.** State reads are synchronous, `Context` propagates correctly (needed
  for override detection), there is no long-lived token or extra container, and it installs via
  HACS on every HA install type.
- **Its features cover v1.** Dynamic "trigger closures", `state_hold`, `@time_trigger("startup")`,
  `@time_active` and automatic hot-reload are all there. It has active 2026 releases
  (2.1.0, Aug 2026, HA 2026.5+).
- **AppDaemon's advantages.** It runs real CPython. Running as a dedicated HA user makes
  "the DSL did this" trivial to detect from `context.user_id`. **Its drawbacks:** it runs in a
  separate process, so its state cache lags its own writes. It also restarts every app on a
  reconnect, and releases are slower (last release Jan 2026).
- **pyscript risks.**
  - It runs a Python subset (no generators, no `match`), interpreted and slower than CPython.
  - A blocking call freezes all of HA.
  - The maintainer team is small, and the 2.x decorator rewrite brought regressions. Pin versions.
  - Its test fixture is not a public API.
- **Long term.** A custom HA integration ("pyscript without the interpreter", using HA's own
  `async_track_template_result`, `RestoreEntity` and the official pytest harness) is likely the
  best eventual home. **So keep the runtime's core host-agnostic.**
- **Native YAML automations** work for the trivial case but turn into helper-entity sprawl once
  overrides and cross-rule state appear.

## 3. Spike (`spike/`)

`spike/compile.py` handles `entity = expr` bindings:

- It rewrites entity refs to `_val("...")` calls.
- It extracts the dependencies statically.
- It rejects double bindings and self-dependency.
- It emits one pyscript rule per binding: an any-change `@state_trigger` on the inputs, plus
  `@time_trigger("startup")`.

`spike/simulate.py` runs the output in plain Python against a fake HA. Run it with
`python3 spike/simulate.py`:

```
      sensor.co2 = unavailable  -> (nothing)
      sensor.co2 = 600          -> (nothing)
 sensor.humidity = 40           -> turn_off(fan.bathroom)
      sensor.co2 = 900          -> turn_on(fan.bathroom)
    fan.bathroom = off          -> (nothing)        # human override respected
      sensor.co2 = 950          -> (nothing)        # result unchanged, no write
      sensor.co2 = 700          -> turn_off(fan.bathroom)
 sensor.humidity = 65           -> turn_on(fan.bathroom)
```

## 4. Design questions to settle before building for real

1. **Level vs edge semantics.** The spike is edge-triggered: it writes only when the expression's
   result flips. A manual override therefore lasts until the next flip. Alternatives:
   - back off for a fixed time after a human change;
   - re-assert continuously (level-triggered).

   Override detection needs `Context` id tracking (pyscript) or a dedicated user (AppDaemon).
2. **Unknown/unavailable inputs.** The spike gets partial Kleene logic for free: `True or <unknown>`
   short-circuits. However, `<unknown> or True` raises and holds the target. The runtime should do
   proper three-valued evaluation, either by wrapping `and`/`or`/comparisons during the AST rewrite
   or by evaluating in the runtime.
3. **Startup.** Should rules assert their value at boot? The spike does, which can clobber a
   pre-restart manual state.
4. **Time.**
   - Debounce (`held(x, minutes=5)`) and hysteresis (`latch(on=co2 > 800, off=co2 < 700)`)
     are stateful operators and need runtime support.
   - Time windows (`between("22:00", "06:00")`) need scheduled wake-ups at the boundaries, not
     just state triggers.
5. **Non-boolean targets.** Brightness, setpoints and covers need per-domain writers that map a
   value to a service call. Read-only entities (sensors) as targets could become virtual entities
   via `state.set`.
6. **Events and the TEA part.** Button presses and scene triggers are not derived state. They need
   an imperative or `update(msg, model)` escape hatch, or first-class modes/state machines whose
   current state is itself an input to bindings.
7. **Static checks.**
   - Cycles across rules.
   - Multiple writers to one entity.
   - Unknown entity ids, checked against an exported entity list.
   - Map errors back to source lines via `# source line N` comments.

## 5. Suggested next step

Write the runtime as plain Python with a host adapter (pyscript first) and test it with a
simulated clock and state store. Settle questions 1–3 there, then grow the compiler alongside it.
