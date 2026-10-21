# ha-dsl: language design

Status: **draft, in discussion.** Every choice has an ID (`D1`, `D2`, …) so we can refer to it.
Each is marked **Decided**, **Proposed** (my recommendation, not yet agreed) or **Open**.
Examples use the recommended option unless they say otherwise.

File extension placeholder: `.hdsl` (name TBD, see D0).

---

## 1. Goals

- **Declarative state.** Write "what each device should be, given the world",
  e.g. `fan.bathroom = co2 > 800 or humidity > 60%`. The system keeps it true across transitions.
- **Events are first-class.** Button presses, arrivals and schedules can be turned into state
  ("was the button pressed in the last hour?"). That makes temporary overrides a one-liner,
  with no imperative timers.
- **Readable by someone who knows a little Python.** Python-flavoured, indentation-based,
  `and`/`or`/`not`, `x if c else y`.
- **Mistakes caught at load time:** unknown entities, type errors, conflicting writers, cycles.

Non-goals for v1: replacing HA's UI automation editor; dashboards; arbitrary Python inside rules.

---

## 2. Architecture

| | Status |
|---|---|
| Ships as a **custom integration**, installable via **HACS** (`hacs.json`) | **Decided** |
| Watches `<config>/ha_dsl/*.hdsl` and recompiles on save | **Decided** |
| The DSL has its own syntax, compiled inside HA at load time (not a Python library) | **Decided** |
| Compile straight to an in-memory signal graph, not to Python source | **Proposed** |
| Reloads are atomic: if a file has errors, keep the old rules running and report | **Proposed** |
| The runtime persists its own state (event timestamps, latches, state machines, last-written values) with HA's `Store` | **Proposed** |

**Why compile to a graph rather than generate Python?** The compiler's output would only be
runtime objects anyway (signals, bindings, timers, as in `spike/library/`). Building them
directly means no code generation, no `exec`, and errors that carry the user's file and line
numbers for free.

**Why persist our own timestamps?** After an HA restart, `last_changed` is reset to the boot
time for most entities. That would make `since(motion) < 1h` or `door off for 10min` wrong right
after a restart. `input_button.*` and `event.*` entities keep their timestamps in their state, but
nothing else does reliably. So the runtime records the transition times it cares about and
persists them.

Pipeline: `file → parse → AST → resolve names + type-check → graph → Runtime ↔ HassHost`.

---

## 3. Core model

The DSL is classic FRP with two kinds of values:

| Kind | Meaning | Examples |
|---|---|---|
| **Signal** | a value at every moment | `sensor.co2`, `co2 > 800`, `now in 22:00..06:00` |
| **Event** | discrete occurrences, no value between them | a button press, `motion turns on`, `at 07:00` |

And three kinds of top-level statements:

| Statement | Meaning |
|---|---|
| **Binding** `fan.x = <signal>` | keep this entity in sync with the signal |
| **Definition** `stuffy = <signal or event>` | a named local value, not an HA entity |
| **Action** `on <event>: <service calls>` | imperative escape hatch (notifications etc.) |

Plus **state machines** (`state x = …`, §11), which are named signals updated by events.

Value types: `bool`, `number`, `string`, `duration`, `time-of-day`, `event`. Every type except
`event` can also be `unknown`. There are no implicit conversions between signals and events.
Using an event where a signal is expected is a compile error with a hint, e.g.
*"`input_button.boost` is an event; did you mean `since(input_button.boost) < 30min`?"*

---

## 4. Grammar base: **D1 (Decided)**

A **custom, Python-flavoured grammar** with our own parser: a Pratt parser for expressions plus an
indentation-aware lexer. It looks like Python (`and`/`or`/`not`, `x if c else y`, indentation)
but adds what Python can't express: `5min`, `50%`, `22:00`, `for`, `until`, `->`, `turns on`.

Rejected: strict Python syntax parsed with `ast` plus a token pre-pass. Every temporal form would
need a pre-pass hack or turn into a function call. A hand-written parser also gives better error
messages. We can ship a VS Code / TextMate grammar later.

---

## 5. Names, entities and strings

### D2. Entity references (Proposed)

- Full entity ids are always valid: `sensor.bathroom_co2`.
  Known HA domains are recognised lexically, so `sensor.x` is never ambiguous.
- Definitions double as aliases: `co2 = sensor.bathroom_co2`.
- A name with no dot is local; a dotted name is an HA entity. So the form of a name tells you
  whether assigning to it is a definition or a binding.
- **Open idea (v2):** area-scoped blocks (`in area bathroom: fan = co2 > 800`), resolved through
  HA's area registry.

Unknown entity ids are errors, with "did you mean". The check runs against HA's entity registry
when the rules load.

### D3. Attributes (Proposed)

`light.hall.brightness`: the third segment is an attribute. The one exception is the `event`
domain, where the third segment filters by event type (see D8).
Attribute names that aren't identifiers use `light.hall["rgb color"]`.

### D3b. String states (Proposed)

- `on`/`off` and `true`/`false` are keywords.
- Other states are quoted strings: `person.ann == "home"`, `climate.lr.hvac_action in ["heating", "idle"]`.
- Alternative: allow bare words checked against the entity's known states
  (`person.ann == home`). It's nicer to read, but it collides with local names. Not recommended.

---

## 6. Literals & units: D4 (Proposed)

| Literal | Meaning |
|---|---|
| `30s`, `5min`, `2h`, `1d`, `1h30min` | durations. `m` is deliberately **not** minutes (it reads like metres). |
| `50%` | percent. Compared with a `%` sensor, it means 50. Written to `light.*.brightness`, it's scaled to 0–255 (or sent as `brightness_pct`). |
| `07:00`, `22:30` | time of day |
| `sunrise`, `sunset`, `sunset - 30min`, `dawn`, `dusk` | solar times (from `sun.sun`) |
| `mon`…`sun`, `weekday`, `weekend` | days |
| `"text"` | string |

Numbers are compared with sensor values after parsing the state as a float. A non-numeric state
is `unknown`, not an error.

---

## 7. Bindings and targets: D5 (Proposed)

```python
fan.bathroom = co2 > 800 or humidity > 60%          # bool → turn_on / turn_off
light.hall.brightness = 80% if motion else 0         # attribute → light.turn_on(brightness=…)
climate.living.temperature = 21 if home else 17      # → climate.set_temperature
cover.blinds.position = 0 if now in sunset..sunrise else 100
```

Several attributes in one service call use a **state literal**:

```python
light.hall = on(brightness: 80%, kelvin: 2700) if motion else off
```

- **Writers per domain.** A table maps `(domain, attribute)` to a service call:
  `light` takes `on`/`off`/brightness/kelvin/rgb, `climate` setpoints and `hvac_mode`,
  `cover` position, `media_player` volume, `input_*` helpers, `select`, `number`.
- **One writer per target.** Two bindings for the same entity (or the same attribute) are a
  compile error, across all files. Combine them with a cascade (§8).
- **`keep`** is a special value meaning "don't drive this right now". The entity is left as it is.
  When the expression stops being `keep`, the binding writes again. This is the basis of
  override handling (§12).

Alternatives for the binding operator: `<-`, `:=` or `follows`. `=` is recommended because it's
what everyone writes first, and the local/entity distinction already comes from the name's form.

---

## 8. Conditionals and priority cascades: D6 (Decided: A, condition first)

### What a cascade is

Most real rules are a **priority list**: several reasons a device might be on or off, where some
reasons beat others. "If the boost button was pressed recently, the fan is on, no matter what.
Otherwise, if the window is open, it's off. Otherwise, it follows the air quality."

Semantically this is just nested `if`/`else`, so Python's `x if c else y` already expresses it.
The question is only whether to add **dedicated layout** for it, because it's the shape almost
every non-trivial rule takes. Whatever layout we choose:

- **First match wins.** Lines are checked top to bottom.
- **There must be a final fallback** (`else`), or it's a compile error.
- **Values can be anything:** `on`/`off`, numbers, `on(brightness: …)`, `keep`, or another
  expression or cascade.
- **The cascade is one expression,** re-evaluated whenever any input changes. The binding writes
  when the result changes. That includes moving from one line to another, e.g. when a
  higher-priority condition stops being true.
- **Temporary overrides fall out naturally.** An override is a high-priority line whose
  condition turns false on its own over time, like `since(remote.press) < 30min`. When it
  expires, the next line takes over and the device re-syncs. No timers, no "restore previous
  state" logic.
- An `unknown` condition falls through to the next line (D16).

### The options, on three real rules

**Rule 1: bathroom fan.** On/off, with an override at the top.

Option A, condition first (`cond -> value`):
```python
fan.bathroom =
    since(remote.press) < 30min    -> on
    window                         -> off
    else                           -> stuffy
```
Option B, value first (`value when cond`):
```python
fan.bathroom =
    on       when since(remote.press) < 30min
    off      when window
    stuffy
```
Option C, plain Python ternaries (no new syntax):
```python
fan.bathroom = (on if since(remote.press) < 30min
                else off if window
                else stuffy)
```

**Rule 2: thermostat setpoint.** Number output, five levels.

Option A:
```python
climate.living.temperature =
    window.living                        -> 7          # frost protection only
    not anyone_home for 2h               -> 15
    since(input_button.warm_up) < 2h     -> 22
    now in 23:00..06:30                  -> 17
    else                                 -> 20.5
```
Option B:
```python
climate.living.temperature =
    7       when window.living
    15      when not anyone_home for 2h
    22      when since(input_button.warm_up) < 2h
    17      when now in 23:00..06:30
    20.5
```
Option C:
```python
climate.living.temperature = (7 if window.living
                              else 15 if not anyone_home for 2h
                              else 22 if since(input_button.warm_up) < 2h
                              else 17 if now in 23:00..06:30
                              else 20.5)
```

**Rule 3: hall light.** Long values, `keep`, and a nested decision.

Option A (nesting is natural, since a value can itself be a cascade):
```python
light.hall =
    since(manual(light.hall)) < 1h    -> keep
    since(motion) < 5min              ->
        night                         -> on(brightness: 10%, kelvin: 2200)
        else                          -> on(brightness: 100%)
    else                              -> off
```
Option B (long values push the conditions far right, and nesting reads awkwardly):
```python
light.hall =
    keep                                when since(manual(light.hall)) < 1h
    on(brightness: 10%, kelvin: 2200)   when since(motion) < 5min and night
    on(brightness: 100%)                when since(motion) < 5min
    off
```
Option C: nesting needs parentheses and gets hard to read quickly.

### Trade-offs

| | A: `cond -> value` | B: `value when cond` | C: ternaries only |
|---|---|---|---|
| Reads as | "if X then Y" (like `match`, Haskell guards) | "Y if X" (like Python's ternary, SQL `CASE`) | Python |
| Scanning **outputs** | values in a ragged right column | values aligned on the left, easy to scan | hard |
| Long values (`on(brightness: …)`) | fine | push the conditions off to the right | fine |
| Nesting | natural | awkward | parentheses |
| Reused elsewhere | same `cond -> value` arms as state machines (§13) and `latest()` | `when` could double as the action syntax | — |
| New syntax | `->`, `else` | `when` | none |

**Decided: A** (`cond -> value`). It handles long values and nesting best, and gives one arm shape
across the language. Python's `x if c else y` remains available for short one-liners.

---

## 9. Events

### D7. Event sources (Proposed)

| Source | Syntax | Notes |
|---|---|---|
| Event entity (HA 2023.8+: remotes, doorbells, buttons) | `event.hall_remote` | Any event from it. Filter by type with D8. |
| Button helpers / button entities | `input_button.boost`, `button.x` | Fires when pressed. Their state is the press timestamp. |
| Signal edges | `motion turns on`, `motion turns off`, `person.ann becomes "home"`, `person.ann leaves "home"`, `changes(sensor.x)` | Derived from any signal, including expressions: `co2 > 800 turns on`. |
| Schedules | `at 07:00`, `at sunset - 30min`, `at 07:00 on weekdays`, `every 10min` | |
| Raw bus events | `bus.zha_event(device_ieee: "…", command: "on")` | For integrations without event entities. Filters on event data fields. |
| Manual control | `manual(fan.bathroom)` | Changes to that entity that weren't made by this integration (detected through HA `Context` ids). See §12. |
| Combining | `event.a or event.b` (merge), `E if cond` (gate) | |

### D8. Event-type filtering for `event.*` entities (Open)

| Option | Example |
|---|---|
| **A. Third segment** (recommended) | `event.hall_remote.double_press` |
| B. Call | `event.hall_remote("double_press")` |
| C. Comparison | `event.hall_remote == "double_press"` (reads like a state test, which is confusing) |

A is consistent with attribute syntax. It's unambiguous because event entities rarely need
attribute access; if one does, use `event.x["attr"]`.

### D9. Edge syntax (Proposed)

`turns on`/`turns off` for bools, `becomes X`/`leaves X` for other values, and `changes(x)` for
any change. Rejected alternatives: `rises(x)`/`falls(x)` (jargon), `↑x` (can't type it).

---

## 10. Events → signals: recency (D10, Decided: age comparison)

This is the "was the button pressed within the last hour" feature. The primitive is
**`since(x)`, a duration signal**:

| `since(…)` of | Value |
|---|---|
| an event `E` | time since E last happened. Infinite if it never has, so `since(E) < D` is false. |
| a bool signal `S` | time since S was last true. `0` while S is true. Infinite if never true. |

```python
boost_active = since(input_button.boost) < 30min      # pressed in the last 30 min
light.hall   = since(motion) < 10min                  # motion now, or in the last 10 min
stale        = since(changes(sensor.co2)) > 1h        # sensor stopped reporting
long_open    = since(door turns on) > 2h and door     # door opened >2h ago and still open
```

Because `since(S)` is `0` while `S` is true, the classic "on during motion and for 10 minutes
after" is just `since(motion) < 10min`.

**D10b (Open): also allow a `.ago` suffix?** `input_button.boost.ago < 30min` reads nicely, but:
- it collides with attribute syntax: `light.hall.ago` looks like an attribute called `ago`;
- expressions need parentheses: `(door turns on).ago`.

Recommendation: `since()` only, at least for v1.

**D10c (Open): spell `for` the same way?** The mirror image of `since` is `held(S)`: how long S
has been continuously true (`0` while false). Then `co2 > 800 for 5min` could be written
`held(co2 > 800) >= 5min`. Recommendation: keep `for` as the everyday form, since it's very
common and reads well, and offer `held()` for unusual comparisons.

The timestamps are tracked and persisted by the runtime (§2), so they survive restarts.

Other event→signal combinators:

| | Meaning |
|---|---|
| `toggle(E, initial: off)` | flips on each occurrence |
| `latest(E1 -> v1, E2 -> v2, initial: v0)` | value from whichever event happened most recently |
| `count(E, 10min)` | number of occurrences in the last 10 min, e.g. triple-press detection |
| `hold(E)` | the last event's payload, e.g. its `event_type` |

All of these persist across restarts.

---

## 11. Temporal operators on signals (D11, Proposed)

```python
light.porch = lux < 20 for 5min                        # true only after 5 min continuous
fan.bedroom = co2 > 800 until co2 < 700                # latch: hysteresis / deadband
light.hall  = since(motion) < 10min                    # true now or recently (§10)
```

- **Debounce** (the output must be stable for D before it's written):
  - block form:
    ```python
    with debounce 2min:
        switch.dehumidifier = humidity > 60%
    ```
  - or per binding: `switch.dehumidifier = humidity > 60% debounced 2min`
- **Equipment protection:**
  ```python
  with min_on 5min, min_off 3min:
      switch.heat_pump = …
  ```
- Deadband alternatives considered:
  - `co2 > 750 ± 50` (can't type `±`);
  - `co2 > 800 / < 700` (too terse);
  - `hysteresis(co2, on: 800, off: 700)` (fine as a function form, could also exist).

---

## 12. Time

Signals:
```python
night   = now in 22:00..06:00              # wraps midnight
evening = now in sunset - 30min..23:00
workday = today in mon..fri and not binary_sensor.holiday
```

Events: `at 07:00`, `at sunrise + 15min`, `every 5min` (§9).

Alternatives: `between(22:00, 06:00)` (function form), or `after sunset and before 23:00`
(reads well, but breaks across midnight). The recommendation is the `..` range with `in`.
**D12: Proposed.**

---

## 13. State machines (TEA `update`): D13 (Open, in discussion)

### When you need one

Derived values (`=` bindings) can't remember *how they got here*. `since()`, `toggle()` and
`latest()` cover simple memory ("pressed in the last 30 min", "last button wins"). A state
machine is for anything with **more than one step of history**:

- **Appliance cycles.** The washer drew power for a while, has now stopped, and nobody has opened
  the door yet, so it's "done".
- **One button cycling modes,** e.g. auto → boost → off → auto, where pressing again cancels.
- **Modes that end themselves,** e.g. night mode until 07:00 on workdays, or until kitchen
  motion after 05:00.
- **Alarm-style sequences:** arming → armed → entry delay → triggered.

In TEA terms, a state machine is the **model + `update`** and bindings are the **`view`**. Mode
logic lives in the machine; what each device does lives in ordinary cascades that read it:

```python
fan.bathroom =
    bath_mode == "boost"   -> on
    bath_mode == "off"     -> off
    else                   -> stuffy
```

### D13a. Overall shape

**Option A: per-state blocks** (recommended). Each state lists its outgoing transitions as
`trigger -> target` arms, the same shape as cascades (D6). The first listed state is the initial
state.

```python
state washer:
    idle:
        washer_power > 10 for 2min           -> running
    running:
        washer_power < 3 for 5min            -> done
    done:
        on enter: notify.mobile_app_phone(message: "Washing is done")
        binary_sensor.washer_door turns on   -> idle
        washer_power > 10 for 2min           -> running
```

**Option B: one transition table.** Compact for small machines, but it repeats state names and
splits each state's behaviour across lines.

```python
state washer = idle:
    idle    -> running   when washer_power > 10 for 2min
    running -> done      when washer_power < 3 for 5min
    done    -> idle      when binary_sensor.washer_door turns on
    done    -> running   when washer_power > 10 for 2min
```

**Option C: TEA-style reducer.** A flat list of `trigger -> new value` arms that guard on the
current state explicitly. It's the most general (it's what the other two compile to), but the
current-state guards make it noisy.

```python
state washer = "idle":
    washer == "idle" and washer_power > 10 for 2min           -> "running"
    washer == "running" and washer_power < 3 for 5min         -> "done"
    washer == "done" and binary_sensor.washer_door turns on   -> "idle"
    washer == "done" and washer_power > 10 for 2min           -> "running"
```

### D13b. What can trigger a transition

| Trigger | Example | Semantics |
|---|---|---|
| Event | `remote.press`, `door turns on`, `at 07:00` | fires when the event happens while in this state |
| Event + guard | `remote.press if night` | as above, only if the guard is true at that moment |
| Condition (bool signal) | `washer_power < 3 for 5min` | **see below** |
| Timeout | `after 1h` | time spent in the current state ≥ 1h (resets on every entry) |

Condition triggers can be **level-triggered** (recommended) or **edge-triggered**:

- **Level:** checked on entry to the state and whenever an input changes. It fires whenever the
  condition is true while in that state. This is how SCXML/statecharts treat "eventless"
  transitions. It's robust: if you enter `away` while someone is already home, `anyone_home ->
  home` fires straight away.
- **Edge:** fires only when the condition *becomes* true while in the state. This is simpler to
  explain, but it misses conditions that were already true on entry or across a restart. Use
  `turns on` explicitly when you mean an edge.

`for` vs `after`: `washer_power < 3 for 5min` counts from when the power dropped, which may be
before the state was entered. `after 5min` counts from entering the state.

### D13c. Referring to states from outside

| Option | Example |
|---|---|
| **A. String comparison** (recommended), checked at compile time against the declared states | `bath_mode == "boost"`, `bath_mode in ["boost", "off"]` |
| B. A boolean per state | `bath_mode.boost` |
| C. `is` with bare names | `bath_mode is boost` |

A is consistent with how other string states are written (D3b). A typo like `"boots"` is still a
compile error, because the machine's state set is known.

The usual operators apply: `since(bath_mode becomes "boost")`, `held(bath_mode == "off")`,
`bath_mode turns …` is an error (it's not a bool), `bath_mode becomes "done"` is an event.

### D13d. Visibility in HA

**Recommended: expose each machine as a `select` entity** (`select.ha_dsl_washer`). Its history
graph then shows the mode over time, it can go on dashboards, and you can set it by hand from the
UI. A manual selection counts as a transition, so it runs entry actions and restarts `after`
timers. The machine's state is restored from this entity across restarts.

Alternatives: a read-only `sensor`, or not exposed at all.

### Proposed details (no decision needed unless you disagree)

- **Shared transitions.** Arms written before the first state block apply in every state, and
  are checked before the state's own arms:
  ```python
  state bath_mode:
      remote.long_press       -> off          # from any state
      auto:
          remote.press        -> boost
      boost:
          remote.press        -> auto
          after 1h            -> auto
      off:
          remote.press        -> auto
          after 2h            -> auto
  ```
- **First match wins** when several transitions are enabled at once, as in cascades.
- **Entry and exit actions:** `on enter:` / `on exit:` blocks inside a state, using the action
  syntax (D14).
- **`-> back`** returns to the previous state. That's useful for interruptions like "movie mode".
- **Chains:** after a transition, level conditions are re-checked in the new state. A chain of
  more than 10 transitions in one step is a runtime error, and the machine stops. A level
  condition that targets its own state is a compile error, because it would loop. Events may
  re-enter the same state, which restarts its `after` timers.
- **Reload:** when a rules file is edited, the machine keeps its current state if that state
  still exists. Otherwise it resets to the initial state, with a log message.
- **Deferred to v2:**
  - nested (hierarchical) states;
  - extended state, i.e. variables stored alongside the mode, like a counter or remembered
    brightness.

### Larger example

```python
state house:
    home:
        not anyone_home for 10min                 -> away
        input_button.goodnight                    -> night
        input_button.movie                        -> movie
    away:
        anyone_home                               -> home
    night:
        at 07:00 if workday                       -> home
        at 09:00                                  -> home
        kitchen_motion if now in 05:00..07:00     -> home
    movie:
        input_button.movie                        -> back
        after 4h                                  -> back

light.living =
    house == "movie"   -> on(brightness: 5%)
    house == "night"   -> off
    else               -> since(living_motion) < 10min
```

---

## 14. Actions: D14 (Proposed)

For things that aren't state: notifications, TTS, one-off scripts.

```python
on input_button.doorbell:
    notify.mobile_app_phone(message: "Someone's at the door")

on person.ann becomes "home" if night:
    light.porch.flash()            # entity-scoped service call: light.flash on light.porch
```

Service calls are written `domain.service(arg: value)`, or `entity.service(…)` to set the
target. Actions can read signals but can't assign to entities that have bindings (compile
error), which prevents fights. There's no `sleep`/`wait` in v1: if you need delays, use state.

---

## 15. Manual control and overrides: D15 (Decided: edge-triggered default)

When a human changes a device that a binding drives, **the change sticks until the binding's
computed value next changes.** The binding writes only on changes to its own result, so it never
fights a manual change in between. This is simple and predictable, but "the next change" might be
seconds or days away.

For a time limit, the override is written with events and `keep` (no new concepts):

```python
fan.bathroom =
    since(manual(fan.bathroom)) < 1h  -> keep      # yield for an hour after a manual change
    else                              -> stuffy
```

…with sugar for the common cases:
```python
with override 1h:            # same as above, for every binding in the block
    fan.bathroom = stuffy
with override never:         # re-assert immediately (e.g. pumps, safety rules)
    switch.pump = pressure < 2
```

When the hour is up, the `keep` line stops matching. The binding's value changes, so it writes
and the device re-syncs.

`manual(target)` is the one allowed self-reference, so it's exempt from cycle detection. Manual
changes are detected with HA `Context`: any change to the entity whose context wasn't created by
ha_dsl counts as manual.

Rejected alternatives:
- a timed back-off by default, because it surprises you when you forget it's there;
- always enforcing by default, because it's wrong for lights and fans.

---

## 16. Unknown / unavailable: D16 (Proposed)

- Three-valued logic: `on or unknown` = `on`, `off and unknown` = `off`, otherwise `unknown`.
- If a binding's result is `unknown`, the target is left alone (an implicit `keep`).
- Fallbacks: `co2 ?? 0`, or `available(sensor.co2)` as an explicit test.
- In cascades, an `unknown` condition falls through to the next branch. (Proposed. The
  alternative is to make the whole result `unknown`, which is safer but brittle.)
- `S for D` treats an `unknown` period as breaking the run. `since(S)` ignores it.

---

## 17. Startup and restarts: D17 (Proposed)

- Wait for HA's `started` event, then evaluate everything.
- Write only where the result differs from the **persisted last-written value**. A restart is
  then a no-op unless the inputs changed while HA was down, and it doesn't undo manual changes
  made before the restart.
- Timers (`for`, `since`, debounce, state timeouts) resume from persisted timestamps.

---

## 18. Abstraction: D18 (Open, can defer to v2)

Repeated patterns, like one fan rule per bathroom:

```python
def bath_fan(fan, co2, humidity, remote):
    fan = …                       # a template: expands at compile time
bath_fan(fan.main_bath, sensor.main_co2, sensor.main_humidity, event.main_remote)
bath_fan(fan.guest_bath, …)
```

Or pure functions over signals: `def stale(s, d) = since(changes(s)) > d`. Both are
compile-time macros, with no runtime cost. Leaving this out of v1 is fine.

---

## 19. Errors, tooling, diagnostics (Proposed)

- Compile errors appear as an HA **repairs issue / persistent notification**, with file, line,
  column and a caret snippet. Old rules keep running until the file compiles.
- **Dry-run mode** (per file: `# mode: dry_run`, or an integration option) logs the writes it
  would make instead of making them.
- **`ha_dsl.explain`** service: given a target, it reports the current value of each
  subexpression and which cascade branch is active ("fan.bathroom is ON because
  `since(remote.press) < 30min` (pressed 12 min ago)").
- Optional diagnostic sensor per binding, whose attributes hold the active branch and reason.
- Service calls carry a `Context` that HA's logbook attributes to ha_dsl and the rule's
  file:line.

---

## 20. Operator precedence (loosest to tightest)

| | |
|---|---|
| `->` (cascade / transition arm) | |
| `x if c else y` | |
| `or` | |
| `and` | |
| `not` | |
| `for`, `until`, `debounced` (temporal postfix) | `not motion for 5min` = `not (motion for 5min)` |
| `turns on`, `becomes`, `leaves` (edges) | `co2 > 800 turns on` = `(co2 > 800) turns on` |
| `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `??` | |
| `+`, `-` | |
| `*`, `/` | |
| unit literals, `.attr`, calls | |

So `co2 > 800 for 5min or humidity > 60%` means `(co2 > 800 for 5min) or (humidity > 60%)`,
which is what you'd expect.

---

## 21. Full example (recommended options)

```python
# bathroom.hdsl
co2      = sensor.bathroom_co2
humidity = sensor.bathroom_humidity
remote   = event.bathroom_remote
window   = binary_sensor.bathroom_window

stuffy = (co2 > 800 until co2 < 700) or humidity > 70% for 2min
night  = now in 22:00..07:00

with override 1h:
    fan.bathroom =
        since(remote.press) < 30min      -> on          # temporary boost
        since(remote.long_press) < 2h    -> off         # temporary silence
        window                           -> off
        night and not humidity > 85%     -> off
        else                             -> stuffy

light.bathroom =
    since(binary_sensor.bathroom_motion) < 5min -> on(brightness: 10% if night else 100%)
    else                                        -> off

on humidity > 90% for 30min turns on:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

---

## 22. Decision log

| ID | Topic | Status | Decision / current recommendation |
|---|---|---|---|
| D0 | Name / file extension | Open | — |
| D1 | Grammar base | **Decided** | custom Python-flavoured grammar, own parser |
| D2 | Entity refs & aliases | Proposed | full ids + definitions as aliases; dotted = entity |
| D3 | Attributes / string states | Proposed | `entity.attr`; quoted strings, `on`/`off` keywords |
| D4 | Units & literals | Proposed | `5min`, `50%`, `22:00`, `sunset - 30min` |
| D5 | Binding operator & targets | Proposed | `=`, per-domain writers, `on(attr: …)`, `keep` |
| D6 | Priority cascades | **Decided** | condition-first `cond -> value` |
| D7 | Event sources | Proposed | table in §9 |
| D8 | Event-type filter | Open | `event.remote.double_press` |
| D9 | Edge syntax | Proposed | `turns on`, `becomes`, `leaves`, `changes()` |
| D10 | Recency syntax | **Decided** | age comparison: `since(E) < 30min`; `since(S)` for signals |
| D10b | `.ago` alias | Open | not in v1 |
| D10c | `held(S)` alongside `for` | Open | keep `for`, add `held()` |
| D11 | Temporal ops | Proposed | `for`, `until`, `debounce`, `min_on` |
| D12 | Time windows | Proposed | `now in 22:00..06:00` |
| D13 | State machines | **Open (in discussion)** | per-state blocks, level-triggered conditions, `after D`, exposed as `select` |
| D14 | Actions | Proposed | `on E:` + service calls, no sleeps |
| D15 | Manual-override default | **Decided** | edge-triggered; `with override D` / `since(manual(x)) < D -> keep` |
| D16 | Unknown handling | Proposed | Kleene logic, unknown → keep |
| D17 | Startup | Proposed | write only if different from persisted last value |
| D18 | Abstraction | Open (v2) | compile-time templates |
