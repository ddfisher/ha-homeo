# ha-dsl: language design

Status: **draft, in discussion.** Every choice has an ID (`D1`, `D2`, …) so we can refer to it.
Each is marked **Decided**, **Proposed** (my recommendation, not yet agreed) or **Open**.
Examples use decided syntax, or the recommended option where nothing is decided yet.
Alternatives we compared in detail are kept in the appendix.

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
| The runtime persists its own state (event timestamps, latches, state machines, last-written values, last condition values) with HA's `Store` | **Proposed** |

**Why compile to a graph rather than generate Python?** The compiler's output would only be
runtime objects anyway (signals, bindings, timers, as in `spike/library/`). Building them
directly means no code generation, no `exec`, and errors that carry the user's file and line
numbers for free.

**Why persist our own timestamps?** After an HA restart, `last_changed` is reset to the boot
time for most entities. That would make `since(motion) < 1h` or `door for 10min` wrong right
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
| **Event** | instants, with no value between them | `input_button.boost`, `event.remote.press`, `on motion`, `on 07:00` |

**`on` marks every place where something happens at an instant** (D9): action headers, the
triggers of state-machine arms, and turning a condition into the event "it just became true".

Top-level statements:

| Statement | Meaning |
|---|---|
| **Binding** `fan.x = <signal>` | keep this entity in sync with the signal |
| **Definition** `stuffy = <signal or event>` | a named local value, not an HA entity |
| **State machine** `state mode = "init": …` | a named signal updated by events (§13) |
| **Action** `on <event>: <service calls>` | imperative escape hatch (notifications etc., §14) |

Value types: `bool`, `number`, `string`, `duration`, `time-of-day`, `event`. Every type except
`event` can also be `unknown`. There are no implicit conversions between signals and events.
Using one where the other is expected is a compile error with a hint, e.g.
- *"`input_button.boost` is an event; did you mean `since(input_button.boost) < 30min`?"*
- *"bindings need a value, but `on door` is an event; did you mean `since(on door) < 5min`?"*

---

## 4. Grammar base: D1 (Decided)

A **custom, Python-flavoured grammar** with our own parser: a Pratt parser for expressions plus an
indentation-aware lexer. It looks like Python (`and`/`or`/`not`, `x if c else y`, indentation)
but adds what Python can't express: `5min`, `50%`, `22:00`, `for`, `until`, `->`, `on`.

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

- `on`/`off` and `true`/`false` are keywords. Using `on` both as a value and as the event marker
  is unambiguous: as a value it only appears where a value is expected (`-> on`, `== on`), and
  as a marker only at the start of a trigger.
- Other states are quoted strings: `person.ann == "home"`, `climate.lr.hvac_action in ["heating", "idle"]`.
- Rejected: bare words checked against the entity's known states (`person.ann == home`). They
  collide with local names.

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
  override handling (§15).

Alternatives for the binding operator: `<-`, `:=` or `follows`. `=` is recommended because it's
what everyone writes first, and the local/entity distinction already comes from the name's form.

---

## 8. Priority cascades: D6 (Decided: condition first, `cond -> value`)

Most real rules are a **priority list**: several reasons a device might be on or off, where some
reasons beat others. Cascades are the dedicated layout for that:

```python
fan.bathroom =
    since(remote.press) < 30min          -> on          # temporary boost
    window                               -> off
    else                                 -> stuffy

climate.living.temperature =
    window.living                        -> 7           # frost protection only
    not anyone_home for 2h               -> 15
    since(input_button.warm_up) < 2h     -> 22
    now in 23:00..06:30                  -> 17
    else                                 -> 20.5

light.hall =
    since(manual(light.hall)) < 1h       -> keep
    since(motion) < 5min                 ->             # a result can be another cascade
        night                            -> on(brightness: 10%, kelvin: 2200)
        else                             -> on(brightness: 100%)
    else                                 -> off
```

- **First match wins.** Lines are checked top to bottom.
- **A final `else` is required in bindings** (compile error otherwise). State machines differ
  (§13).
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
- Python's `x if c else y` remains available for short one-liners.

The rejected alternatives were value-first `on when cond` and chained ternaries only; see the
appendix.

---

## 9. Events

### D7. Event sources (Proposed)

These expressions *are* events:

| Source | Syntax | Notes |
|---|---|---|
| Event entity (HA 2023.8+: remotes, doorbells, buttons) | `event.hall_remote` | Any event from it. Filter by type with D8: `event.hall_remote.double_press`. |
| Button helpers / button entities | `input_button.boost`, `button.x` | Fires when pressed. Their state is the press timestamp. |
| Any change of a value | `changes(sensor.x)` | For non-bool values. For bools, use `on x` / `on not x`. |
| Manual control | `manual(fan.bathroom)` | Changes to that entity that weren't made by this integration (detected through HA `Context` ids). See §15. |
| Periodic | `every 10min` | |
| Raw bus events | `bus.zha_event(device_ieee: "…", command: "on")` | For integrations without event entities. Filters on event data fields. |
| Merge | `remote.press or input_button.boost` | `or` between two events is "either happens". |

A definition can name an event, e.g. `opened = on door`, and be used wherever an event can.

### D9. `on` and edges (Decided)

`on X` is the one way to write "the moment something happens". What it means depends on `X`:

| `X` is | `on X` means | Example |
|---|---|---|
| an event | that event | `on remote.press`, `on manual(fan.x)` |
| a bool condition | the moment it **becomes true** | `on door`, `on not door`, `on co2 > 800 for 5min` |
| a comparison | the moment it becomes true | `on person.ann == "home"` (arrives), `on person.ann != "home"` (leaves) |
| a time of day | daily at that time | `on 07:00`, `on sunset - 30min` |

- A gate is added with `if`: `on remote.press if night`, `on 07:00 if weekday`. This is an
  `if` without `else`. A ternary inside an `on` expression needs parentheses.
- `on` applies to the whole expression after it: `on door or window` is the moment
  `(door or window)` becomes true. To merge two events, write `on door or on window`.

**Where `on` is required:**
1. **Action headers:** `on <event>: …` (§14).
2. **State-machine triggers:** every arm path has exactly one `on` (§13, D13e).

**Inside event-taking functions** (`since`, `count`, `toggle`, `latest`, `hold`), event sources
are written as they are, and a condition edge needs `on`. Notice the difference:

```python
since(door) < 5min       # door is open now, or was open in the last 5 min (signal form, §10)
since(on door) < 5min    # door was *opened* in the last 5 min, whether or not it's open now
since(remote.press)      # event sources need no `on` (writing it is allowed and changes nothing)
```

This replaces the earlier edge words `turns on`/`turns off`/`becomes`/`leaves` and the `at`
keyword. One concept, `on`, covers all of them.

### D8. Event-type filtering for `event.*` entities (Open)

| Option | Example |
|---|---|
| **A. Third segment** (recommended) | `event.hall_remote.double_press` |
| B. Call | `event.hall_remote("double_press")` |
| C. Comparison | `event.hall_remote == "double_press"` (reads like a state test, which is confusing) |

A is consistent with attribute syntax. It's unambiguous because event entities rarely need
attribute access; if one does, use `event.x["attr"]`.

---

## 10. Events → signals: recency (D10, Decided: age comparison)

The primitive is **`since(x)`, a duration signal**:

| `since(…)` of | Value |
|---|---|
| an event `E` | time since E last happened. Infinite if it never has, so `since(E) < D` is false. |
| a bool signal `S` | time since S was last true. `0` while S is true. Infinite if never true. |

```python
boost_active    = since(input_button.boost) < 30min   # pressed in the last 30 min
light.hall      = since(motion) < 10min               # motion now, or in the last 10 min
recently_opened = since(on door) < 5min               # opened in the last 5 min
```

Because `since(S)` is `0` while `S` is true, the classic "on during motion and for 10 minutes
after" is just `since(motion) < 10min`.

Related general functions:

| | Meaning |
|---|---|
| `age(x)` | time since `x` last changed (HA's `last_changed`, persisted). `age(sensor.co2) > 1h` means the sensor is stale. |
| `previous(x)` | the value before `x` last changed |
| `held(S)` | how long S has been continuously true (`0` while false). The function form of `for` (D10c). |
| `toggle(E, initial: off)` | flips on each occurrence |
| `latest(E1 -> v1, E2 -> v2, initial: v0)` | value from whichever event happened most recently |
| `count(E, 10min)` | number of occurrences in the last 10 min, e.g. triple-press detection |
| `hold(E)` | the last event's payload, e.g. its `event_type` |

All of these are persisted across restarts (§2).

**D10b (Open): also allow a `.ago` suffix?** `input_button.boost.ago < 30min` reads nicely, but
it collides with attribute syntax (`light.hall.ago` looks like an attribute called `ago`), and
expressions need parentheses: `(on door).ago`. Recommendation: `since()` only, at least for v1.

**D10c (Open): `held()` alongside `for`?** `co2 > 800 for 5min` could also be written
`held(co2 > 800) >= 5min`. Recommendation: keep `for` as the everyday form and offer `held()` for
unusual comparisons.

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

## 12. Time: D12 (Proposed)

Signals:
```python
night   = now in 22:00..06:00              # wraps midnight
evening = now in sunset - 30min..23:00
workday = today in mon..fri and not binary_sensor.holiday
```

Events: `on 07:00`, `on sunrise + 15min`, `on 07:00 if weekday`, `every 5min` (§9).

Alternatives: `between(22:00, 06:00)` (function form), or `after sunset and before 23:00`
(reads well, but breaks across midnight). The recommendation is the `..` range with `in`.

---

## 13. State machines (TEA `update`): D13

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

### D13a. Shape: a TEA reducer with nested arms (Decided, provisionally; revisit after real use)

A `state` block is a cascade of `trigger -> new value` arms. Arms can nest, as in D6, so they can
be grouped by state, by trigger, or not at all:

```python
state bath_mode = "auto":
    on remote.long_press                         -> "off"
    on remote.press ->                                          # grouped by trigger
        bath_mode == "auto"                      -> "boost"
        else                                     -> "auto"
    on bath_mode == "boost" for 1h               -> "auto"
    on bath_mode == "off" for 2h                 -> "auto"

state washer = "idle":                                          # flat, with `if` gates
    on washer_power > 10 for 2min if washer != "running"   -> "running"
    on washer_power < 3 for 5min if washer == "running"    -> "done"
    on binary_sensor.washer_door if washer == "done"       -> "idle"

on washer == "done":                                            # entry action = ordinary action
    notify.phone(message: "Washing is done")

state house = "home":
    house == "home" ->                                          # grouped by state
        on not anyone_home for 10min             -> "away"
        on input_button.goodnight                -> "night"
        on input_button.movie                    -> "movie"
    house == "away" ->
        on anyone_home                           -> "home"
    house == "night" ->
        on 07:00 if workday                      -> "home"
        on 09:00                                 -> "home"
        on kitchen_motion and now in 05:00..07:00 -> "home"
    house == "movie" ->
        on input_button.movie                    -> previous(house)
        on age(house) > 4h                       -> previous(house)
```

Rules specific to `state` blocks:
1. **The declaration** `state name = initial:`.
2. **Arms read the value from before the update,** as in TEA. So `bath_mode == "auto"` inside
   `bath_mode`'s own arms is allowed, and isn't a cycle.
3. **If no arm fires, the value is unchanged.** There's no required `else`, unlike binding
   cascades.
4. **First match wins** when several arms fire at once.
5. **Triggers and gates** (D13e).

Everything else uses general features: entry/exit actions are `on m == "x":` / `on m != "x":`
actions; timeouts are `on age(m) > 1h` or `on m == "x" for 1h`; "any of these states" is
`m in [...]`; "return to the previous state" is `previous(m)`.

The compiler infers the state set from the initial value, the literal results and `previous()`.
So typos in state names are caught. Unreachable states can only be detected when guards have the
simple form `m == "literal"`.

### D13b. Condition triggers (Decided)

- **`on <condition>` fires when the condition *becomes* true while its gates hold.** A
  condition that is already true when the state is entered doesn't fire. Note the condition is the
  *whole* expression: `on kitchen_motion and now in 05:00..07:00` still fires at 05:00 if the
  motion is active then.
- **On startup and on rules reload,** each `on <condition>` (in state machines and actions) is
  compared with its value persisted from before the shutdown. If it went false → true while HA
  was down, it fires once. **Proposed:** if there's no persisted value (first start), treat
  "true now" as an edge.
- **Missed times:** `on <time>` triggers whose time passed while HA was down fire once on
  startup, so a `night → home on 07:00` isn't skipped by a 06:58 restart. **Proposed:** for
  actions, only catch up if the delay is short (default 1h, configurable). A notification six
  hours late is usually noise.
- `on washer_power < 3 for 5min` counts from when the power dropped, which may be before the
  state was entered. To count from entering the state, use `on age(washer) > 5min`.

### D13c. Referring to states (Decided: string comparison)

`bath_mode == "boost"`, `bath_mode in ["boost", "off"]`. These are checked at compile time
against the inferred states, so a typo like `"boots"` is an error. The usual tools apply:
`on bath_mode == "done"`, `since(on bath_mode == "boost")`, `held(bath_mode == "off")`,
`age(bath_mode)`.

### D13d. Visibility in HA (Decided: writable `select` entity)

Each machine is exposed as `select.ha_dsl_<name>`. It shows in history and on dashboards, and can
be set from the UI. A manual selection counts as a transition: entry and exit actions fire, and
`age()` resets. The entity also persists the machine's state across restarts (restore state),
along with the persisted condition values from D13b.

### D13e. Triggers and gates (Proposed; revises the earlier provisional rule)

**Every arm path in a `state` block contains exactly one `on`. That `on` is the trigger.** Every
other condition on the path is a gate, checked at the moment of the trigger: outer grouping arms,
`if` clauses, and inner arms under an `on`. A path with no `on`, or with two, is a compile error.

The earlier rule ("an event triggers; otherwise the innermost condition's edge triggers") needed
explaining. With `on` it's visible on the page, and edge vs level is something you write
explicitly:

```python
house == "away" ->
    on anyone_home                    -> "home"   # only if someone arrives while away
on house == "away" and anyone_home    -> "home"   # also fires on entering away with someone home
```

Because the second form can fire on entry, transitions can chain. A chain of more than 10
transitions in one step is an error, and the machine stops.

### Proposed details

- **Reload:** when a rules file is edited, the machine keeps its current state if that state
  still exists. Otherwise it resets to the initial state, with a log message.
- **Deferred to v2:**
  - nested (hierarchical) states;
  - extended state, i.e. variables stored alongside the mode, like a counter or remembered
    brightness.

---

## 14. Actions: D14 (Proposed)

For things that aren't state: notifications, TTS, one-off scripts.

```python
on input_button.doorbell:
    notify.mobile_app_phone(message: "Someone's at the door")

on person.ann == "home" if night:
    light.porch.flash()            # entity-scoped service call: light.flash on light.porch

on humidity > 90% for 30min:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

Service calls are written `domain.service(arg: value)`, or `entity.service(…)` to set the
target. Actions can read signals but can't assign to entities that have bindings (compile
error), which prevents fights. There's no `sleep`/`wait` in v1: if you need delays, use state.

---

## 15. Manual control and overrides: D15 (Decided: edge-triggered default)

When a human changes a device that a binding drives, **the change sticks until the binding's
computed value next changes.** The binding writes only on changes to its own result, so it never
fights a manual change in between.

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
- `on <condition>` treats `unknown → true` as a rising edge only if the value was false before
  the unknown period. A sensor dropping out and coming back doesn't fire spurious triggers.
- `S for D` treats an `unknown` period as breaking the run. `since(S)` ignores it.

---

## 17. Startup and restarts: D17 (Proposed)

- Wait for HA's `started` event, then evaluate everything.
- Write only where the result differs from the **persisted last-written value**. A restart is
  then a no-op unless the inputs changed while HA was down, and it doesn't undo manual changes
  made before the restart.
- Timers (`for`, `since`, debounce, state timeouts) resume from persisted timestamps.
- Triggers missed during downtime: see D13b.

---

## 18. Abstraction: D18 (Open, can defer to v2)

Repeated patterns, like one fan rule per bathroom:

```python
def bath_fan(fan, co2, humidity, remote):
    fan = …                       # a template: expands at compile time
bath_fan(fan.main_bath, sensor.main_co2, sensor.main_humidity, event.main_remote)
bath_fan(fan.guest_bath, …)
```

Or pure functions over signals: `def stale(s, d) = age(s) > d`. Both are compile-time macros,
with no runtime cost. Leaving this out of v1 is fine.

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
| `on X if G` (trigger prefix, gate) | only at the start of an action header or arm |
| `x if c else y` | inside `on`, needs parentheses |
| `or` | |
| `and` | |
| `not` | |
| `for`, `until`, `debounced` (temporal postfix) | `not motion for 5min` = `not (motion for 5min)` |
| `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `??` | |
| `+`, `-` | |
| `*`, `/` | |
| unit literals, `.attr`, calls | |

So `co2 > 800 for 5min or humidity > 60%` means `(co2 > 800 for 5min) or (humidity > 60%)`, and
`on co2 > 800 for 5min if night` is the moment `co2 > 800 for 5min` becomes true, gated by `night`.

---

## 21. Full example

```python
# bathroom.hdsl
co2      = sensor.bathroom_co2
humidity = sensor.bathroom_humidity
remote   = event.bathroom_remote
window   = binary_sensor.bathroom_window

stuffy = (co2 > 800 until co2 < 700) or humidity > 70% for 2min
night  = now in 22:00..07:00

state bath_mode = "auto":
    on remote.long_press                  -> "off"
    on remote.press ->
        bath_mode == "auto"               -> "boost"
        else                              -> "auto"
    on bath_mode == "boost" for 30min     -> "auto"
    on bath_mode == "off" for 2h          -> "auto"

with override 1h:
    fan.bathroom =
        bath_mode == "boost"              -> on
        bath_mode == "off"                -> off
        window                            -> off
        night and not humidity > 85%      -> off
        else                              -> stuffy

light.bathroom =
    since(binary_sensor.bathroom_motion) < 5min -> on(brightness: 10% if night else 100%)
    else                                        -> off

on humidity > 90% for 30min:
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
| D9 | Events & edges | **Decided** | `on X` marks instants; `on <cond>` = becomes true; replaces `turns on`/`becomes`/`leaves`/`at` |
| D10 | Recency syntax | **Decided** | age comparison: `since(E) < 30min`; `since(S)` for signals |
| D10b | `.ago` alias | Open | not in v1 |
| D10c | `held(S)` alongside `for` | Open | keep `for`, add `held()` |
| D11 | Temporal ops | Proposed | `for`, `until`, `debounce`, `min_on` |
| D12 | Time windows | Proposed | `now in 22:00..06:00` |
| D13a | State machine shape | **Decided (provisional)** | TEA reducer with nested arms; revisit after real use |
| D13b | Condition triggers | **Decided** | edge-triggered; re-checked against persisted values on restart and reload; missed times fire once |
| D13c | Referring to states | **Decided** | string comparison, compile-time checked |
| D13d | Visibility in HA | **Decided** | writable `select.ha_dsl_<name>` |
| D13e | Triggers vs gates | Proposed (revised) | exactly one `on` per arm path is the trigger; everything else gates |
| D14 | Actions | Proposed | `on E:` + service calls, no sleeps; late catch-up limited to 1h |
| D15 | Manual-override default | **Decided** | edge-triggered; `with override D` / `since(manual(x)) < D -> keep` |
| D16 | Unknown handling | Proposed | Kleene logic, unknown → keep, no spurious edges after unknown |
| D17 | Startup | Proposed | write only if different from persisted last value |
| D18 | Abstraction | Open (v2) | compile-time templates |

---

## Appendix: alternatives considered

These are written in the syntax current at the time (before D9 introduced `on`).

### A1. Cascade layouts (D6)

Value first (`value when cond`): outputs line up on the left, but long values push conditions
off to the right, and nesting reads awkwardly:
```python
light.hall =
    keep                                when since(manual(light.hall)) < 1h
    on(brightness: 10%, kelvin: 2200)   when since(motion) < 5min and night
    on(brightness: 100%)                when since(motion) < 5min
    off
```
Chained ternaries only: no new syntax, but hard to read past three branches:
```python
climate.living.temperature = (7 if window.living
                              else 15 if not anyone_home for 2h
                              else 22 if since(input_button.warm_up) < 2h
                              else 17 if now in 23:00..06:30
                              else 20.5)
```

### A2. State machine shapes (D13a)

**Per-state blocks.** Readable by mode, but it needed a growing list of special syntax:
any-state arms, `after D`, `on enter:`, multi-state headings, `back`, and an implicit initial state.
```python
state house:
    home:
        not anyone_home for 10min              -> away
        input_button.goodnight                 -> night
    night:
        at 07:00 if workday                    -> home
    movie:
        input_button.movie                     -> back
        after 4h                               -> back
```
**Transition table.** Repeats state names, and `->` means "from → to" instead of
"trigger → result":
```python
state washer = idle:
    idle    -> running  when washer_power > 10 for 2min
    running -> done     when washer_power < 3 for 5min
    done    -> idle     when binary_sensor.washer_door turns on
```
**Grouped by trigger.** Best for remote-control machines, awkward for condition-driven ones:
```python
state bath_mode = auto:
    on remote.press:
        auto        -> boost
        boost, off  -> auto
    after 1h:
        boost       -> auto
```
The chosen reducer can express both groupings through nested arms, without special syntax.

### A3. Recency syntax (D10)

Infix `E within 30min` (with a mirrored `S within D` = "true at some point in the window"),
functions `recent(E, 30min)`, and natural language (`happened in the last 30min`). Age
comparison was chosen.

### A4. Event marking (D9)

- An `@` sigil on every event (`@remote.press`, `since(@remote.press)`): terse, but cryptic.
- Function calls for everything (`fired(remote, "press")`, `rises(door)`): unambiguous, but
  wordy.
- Explicit event-forming suffixes with no marker (`.pressed`, `turns on`), relying on the type
  checker.

`on` was chosen because actions already used it, and it makes the trigger of every arm visible.
