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
time for most entities. That would make `motion within 1h` or `door off for 10min` wrong right
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
*"`input_button.boost` is an event; did you mean `input_button.boost within 30min`?"*

---

## 4. Grammar base: **D1 (Proposed)**

| Option | Example | Pros | Cons |
|---|---|---|---|
| **A. Python-flavoured custom grammar** | `boost within 30min` | Full control. We can have `within`, `for`, `5min`, `50%`, `22:00`, `->`. Keeps Python's look. | We write the parser (small: Pratt parser for expressions plus an indentation lexer). Editor highlighting only approximate at first. |
| B. Strict Python syntax + token pre-pass | `within(boost, 30*min)` | Free parser (`ast`), Python highlighting. | Every nice form needs a hack in the pre-pass, or ends up as a function call. |

**Recommendation: A.** The main appeal of the DSL is the event and temporal vocabulary, which
Python's grammar can't express. A hand-written parser also gives the best error messages.
We can ship a VS Code / TextMate grammar later.

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

## 8. Conditionals and priority cascades: D6 (Open, key decision)

Most real rules are a priority list: "boost button wins, then the window being open, then air
quality". This deserves first-class syntax. Python's `x if c else y` remains available for
short cases.

**Option A: condition first** (like Haskell guards or a match)
```python
fan.bathroom =
    boost within 30min       -> on
    window.bathroom          -> off
    else                     -> stuffy
```

**Option B: value first**
```python
fan.bathroom =
    on      when boost within 30min
    off     when window.bathroom
    stuffy
```

**Option C: chained `if`/`else` only** (pure Python, no new syntax)
```python
fan.bathroom = (on if boost within 30min
                else off if window.bathroom
                else stuffy)
```

Rules for A and B: branches are checked top to bottom and the first true one wins. A missing
final `else` is a compile error, unless we make the fallback `keep`. An `unknown` condition
falls through to the next branch. (Alternatively it could make the whole result `unknown`;
see D16.)

**Recommendation: A.** It reads as "if … then …", lines up nicely, and the same `cond -> value`
shape is reused for state machines (§11) and `latest()` (§10). B is nice for short lists of
outputs.

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

## 10. Events → signals: recency (D10, Open, key decision)

This is the "was the button pressed within the last hour" feature.

**Option A: infix `within`** (recommended)
```python
boost_active = input_button.boost within 30min
fan.bathroom =
    input_button.boost within 30min  -> on
    else                             -> stuffy
```

**Option B: functions**
```python
boost_active = recent(input_button.boost, 30min)
```

**Option C: age comparison**
```python
boost_active = since(input_button.boost) < 30min
boost_active = input_button.boost.ago < 30min
```

**Option D: natural language**
```python
boost_active = input_button.boost happened in the last 30min
```

The recommendation is **A**, with `since(E)` (a duration signal, infinite if never) as the
general form underneath. `since` covers the rarer cases, e.g. `since(door turns on) > 2h`.

**`within` also applies to signals,** which gives a pair of operators with mirrored meanings:

| | Event `E` | Bool signal `S` |
|---|---|---|
| `X within D` | E happened in the last D | S was true **at some point** in the last D |
| `X for D` | (error) | S has been true **the whole** last D |

So `motion within 10min` means "motion now, or it ended less than 10 minutes ago". That is the
standard "lights stay on 10 minutes after motion" rule.

Other event→signal combinators:

| | Meaning |
|---|---|
| `toggle(E, initial: off)` | flips on each occurrence |
| `latest(E1 -> v1, E2 -> v2, initial: v0)` | value from whichever event happened most recently |
| `count(E within 10min)` | number of occurrences in the window, e.g. triple-press detection |
| `hold(E)` | the last event's payload, e.g. its `event_type` |

All of these persist across restarts.

---

## 11. Temporal operators on signals (D11, Proposed)

```python
light.porch = lux < 20 for 5min                        # true only after 5 min continuous
fan.bedroom = co2 > 800 until co2 < 700                # latch: hysteresis / deadband
light.hall  = motion within 10min                      # true now or recently
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

## 13. State machines (TEA `update`): D13 (Open)

For modes that cycle or reset themselves, which plain derived values can't express. A `state`
is a named signal with an initial value and `event -> new value` transitions. Its value persists
across restarts.

**Option A: transition list** (same `cond -> value` shape as cascades)
```python
state bath_mode = "auto":
    event.bath_remote.press                       -> "boost" if bath_mode == "auto" else "auto"
    event.bath_remote.long_press                  -> "off"
    bath_mode != "auto" for 1h                    -> "auto"      # hmm: a signal, see note
```

**Option B: explicit states, Harel-chart style**
```python
state bath_mode:
    auto:
        on event.bath_remote.press -> boost
        on event.bath_remote.long_press -> off
    boost (max 1h -> auto):
        on event.bath_remote.press -> auto
    off (max 1h -> auto):
        on event.bath_remote.press -> auto
```

Note on A: transitions need events. A bool signal in event position should probably mean its
rising edge (`… for 1h turns on`). Implicit, or a compile error asking you to write `turns on`?
I lean towards an implicit rising edge **inside `state` blocks only**.

The recommendation is **B** for anything with more than two states. Per-state timeouts
(`max 1h`) are the most common need and B makes them obvious. Simple cases often don't need a
state machine at all: `toggle()`, `latest()` and `within` cover them.

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

## 15. Manual control and overrides: D15 (Open, key decision)

What happens when a human changes a device that a binding drives?

**Option A: edge-triggered (spike behaviour, recommended default).** The binding writes only
when its computed value changes. A manual change sticks until the next flip of the expression.
That's simple and predictable, but "the next flip" might be seconds or days away.

**Option B: timed back-off by default.** After a manual change, the binding yields for a
default period (e.g. 1h), then re-syncs.

**Option C: always enforce.** It's re-asserted immediately. Rarely what you want for
lights and fans, but right for, say, a pump.

Whatever the default, explicit control is spelled with events and `keep` (no new concepts):

```python
fan.bathroom =
    manual(fan.bathroom) within 1h  -> keep      # yield for an hour after a manual change
    else                            -> stuffy
```

…plus sugar for the common cases:
```python
with override 1h:            # same as above, for every binding in the block
    fan.bathroom = stuffy
with override never:         # option C for this block
    switch.pump = pressure < 2
```

`manual(target)` is the one allowed self-reference, so it's exempt from cycle detection.

---

## 16. Unknown / unavailable: D16 (Proposed)

- Three-valued logic: `on or unknown` = `on`, `off and unknown` = `off`, otherwise `unknown`.
- If a binding's result is `unknown`, the target is left alone (an implicit `keep`).
- Fallbacks: `co2 ?? 0`, or `available(sensor.co2)` as an explicit test.
- In cascades, an `unknown` condition falls through to the next branch. (Proposed. The
  alternative is to make the whole result `unknown`, which is safer but brittle.)
- `S for D` treats an `unknown` period as breaking the run. `S within D` ignores it.

---

## 17. Startup and restarts: D17 (Proposed)

- Wait for HA's `started` event, then evaluate everything.
- Write only where the result differs from the **persisted last-written value**. A restart is
  then a no-op unless the inputs changed while HA was down, and it doesn't undo manual changes
  made before the restart.
- Timers (`for`, `within`, debounce, state timeouts) resume from persisted timestamps.

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
  `boost within 30min` (pressed 12 min ago)").
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
| `within`, `for`, `until`, `debounced` (temporal postfix) | `not boost within 30min` = `not (boost within 30min)` |
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
        remote.press within 30min        -> on          # temporary boost
        remote.long_press within 2h      -> off         # temporary silence
        window                           -> off
        night and not humidity > 85%     -> off
        else                             -> stuffy

light.bathroom =
    binary_sensor.bathroom_motion within 5min -> on(brightness: 10% if night else 100%)
    else                                      -> off

on humidity > 90% for 30min turns on:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

---

## 22. Decision log

| ID | Topic | Status | Current recommendation |
|---|---|---|---|
| D0 | Name / file extension | Open | — |
| D1 | Grammar base | Proposed | custom Python-flavoured grammar |
| D2 | Entity refs & aliases | Proposed | full ids + definitions as aliases; dotted = entity |
| D3 | Attributes / string states | Proposed | `entity.attr`; quoted strings, `on`/`off` keywords |
| D4 | Units & literals | Proposed | `5min`, `50%`, `22:00`, `sunset - 30min` |
| D5 | Binding operator & targets | Proposed | `=`, per-domain writers, `on(attr: …)`, `keep` |
| D6 | Priority cascades | **Open (key)** | condition-first `cond -> value` |
| D7 | Event sources | Proposed | table in §9 |
| D8 | Event-type filter | Open | `event.remote.double_press` |
| D9 | Edge syntax | Proposed | `turns on`, `becomes`, `leaves`, `changes()` |
| D10 | Recency syntax | **Open (key)** | `E within 30min`, `since(E)` underneath |
| D11 | Temporal ops | Proposed | `for`, `within`, `until`, `debounce`, `min_on` |
| D12 | Time windows | Proposed | `now in 22:00..06:00` |
| D13 | State machines | Open | explicit states with per-state `max` timeouts |
| D14 | Actions | Proposed | `on E:` + service calls, no sleeps |
| D15 | Manual-override default | **Open (key)** | edge-triggered default + `with override D` |
| D16 | Unknown handling | Proposed | Kleene logic, unknown → keep |
| D17 | Startup | Proposed | write only if different from persisted last value |
| D18 | Abstraction | Open (v2) | compile-time templates |
