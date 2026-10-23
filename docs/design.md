# Homeo: language design

**What this is.** Homeo lets Home Assistant (HA) users write automations as declarative rules,
describing *what each device should be given the state of the world*, instead of HA's YAML
trigger/condition/action automations. For example:

```python
fan.bathroom := co2 > 800 or humidity > 60%
```

This keeps the fan in sync with that expression on every change. Homeo ships as a custom HA
integration (installable via HACS). It watches `.homeo` rule files, compiles them inside HA, and
drives devices through HA service calls.

**How this document came about.** The project owner and Claude wrote it over a long design
conversation. The owner made every decision in the log (§22). Read "Handoff notes" below before
working on the code.

Status: **v1 implementation complete; not yet released.** All seven build steps have a
first version, including the editor panel; see "Implementation status" in `docs/implementation-plan.md` (§6) for what works, what
was verified, and what's next. Every decision has an ID (`D1`, `D2`, …) and is listed in the
decision log (§22). Two are marked *provisional*, to revisit once real rules have
been written: the state machine shape (D13a) and bare times as time points (D12). Alternatives we
compared are kept in the appendix.

**Homeo** (from *homeostasis*: a system that keeps itself in balance). Rule files use `.homeo`,
and the integration's domain is `homeo`.

**In v1:** everything in §1–§21 that isn't marked "later" or "v2".

**Deliberately not in v1:**
- `with override`, `until`, debounce, `min_on`/`min_off` sugar (§11);
- declarative notifications (D22);
- user-defined writers (§7);
- nested states and extended state (§13);
- templates and functions (D18);
- area-scoped names (D2);
- persistence across restarts (§17).

**Suggested build order:**
1. lexer and parser;
2. name resolution and type checking;
3. runtime core (signals, change times, cascades, `latest`, state machines, actions), tested
   against a fake HA host with a simulated clock;
4. writers;
5. the HA integration: host adapter, file watching, reloads, `select` entities, errors as repairs
   issues;
6. tooling (`homeo.explain`, dry run);
7. **finally:** an editor in HA's UI for `.homeo` files, with syntax highlighting and inline
   errors (§19).

---

## Handoff notes for implementers

### Working with this document and the owner

- **This document is the source of truth.** If implementation shows a decision doesn't work, or
  you hit a gap in the spec, don't silently invent semantics. Choose the option most consistent
  with the principles below, record it here (and in the decision log), and flag it to the owner.
- **The owner's design preferences**, as observed during the design:
  - **FRP / TEA / React mindset.** Rules describe values, not triggers. Effects are confined to
    top-level `on` actions.
  - **A small core.** Sugar is added only once real rules need it. Several sugar items were
    deliberately deferred ("maybe later", §11); don't add them back unasked.
  - **No special cases ("warts").** The owner prefers one general mechanism. For example, restart
    behaviour falls out of unknown handling rather than special restart rules.
  - **Restarts are rare.** Reasonable, predictable behaviour beats exact reconstruction.
  - **Provisional decisions should stay cheap to change.** D13a (state machine shape) and D12
    (bare times as time points) are to be revisited once real rules exist.

### Why this architecture (rejected paths, briefly)

- **pyscript (generating code for it):** pyscript runs `.py` files through its own AST
  interpreter. Its `ast_compare` (`custom_components/pyscript/eval.py`) collapses every comparison
  to a plain bool, so an operator-overloading library can't work there. Generating
  decorator-based triggers would limit the semantics.
- **AppDaemon:** real Python, but a separate process. Its state cache lags its own writes, it
  restarts all apps on reconnect, and it needs a long-lived token.
- **A plain Python library with operator overloading:** Python can't overload
  `and`/`or`/`not`/`x if c else y`, and `&`/`|` have surprising precedence (`a > 1 | b > 2`
  means `a > (1 | b) > 2`). Hence Homeo has its own syntax and parser.
- **Custom integration (chosen):**
  - in-process, real CPython;
  - HA's own state-tracking helpers;
  - native `Context` (needed for manual-change detection);
  - `RestoreEntity`;
  - HA's official test harness.

### Home Assistant facts the design relies on

Verify these against the current HA version when implementing.

- **Event and button entities:**
  - Event entities (`event.*`, HA 2023.8+) have the ISO timestamp of their last event as their
    state, with an `event_type` attribute.
  - `input_button.*` and `button.*` entities have the timestamp of their last press as their
    state.
- **Restarts:**
  - `last_changed` resets to the boot time for most entities on restart.
  - Restore state (`RestoreEntity`) is saved periodically (about every 15 minutes) and on a clean
    shutdown. `async_get_last_state()` returns the stored `State`, including its `last_changed`.
- **Context:** service calls run with a `homeassistant.core.Context` that we create. Entities
  reuse a service call's context for state writes within a short window (5 s in HA's
  `entity.py`); later reports get a fresh context. D21 rule 2 exists because of this.
- **Useful APIs:**
  - `async_track_state_change_event`;
  - `async_track_point_in_time` / `async_call_later`;
  - `hass.services.async_call(..., context=ctx)`;
  - the issue registry, for repairs issues;
  - `homeassistant.util.dt` for time.
- **Don't block the event loop:** read rule files with `hass.async_add_executor_job`, and wait for
  `EVENT_HOMEASSISTANT_STARTED` before evaluating rules.
- **HACS:** the repository needs a `hacs.json` and the integration in `custom_components/homeo/`,
  with a `manifest.json`.

### Code layout

As built, see `docs/implementation-plan.md` §1. The original suggestion, which it follows:

- **A host-agnostic core package** (lexer, parser, checker, runtime) that imports nothing from HA.
  Unit-test it with a fake host and a simulated clock. The host interface sketched during
  prototyping was:
  - `read(entity_id, attr)`;
  - `write(target, value)`;
  - `now()`;
  - `schedule(when, callback)`;
  - plus state-change notifications.
- **`custom_components/homeo/`:**
  - setup, file watching and reloads;
  - the HA host adapter;
  - writers;
  - the `select` entities for state machines;
  - later, the editor panel.
- **Integration tests** with `pytest-homeassistant-custom-component`.

### Spec gaps you'll need to settle

These weren't discussed. Decide each one, document it here, and flag it to the owner.

- **Lexical details:**
  - identifiers, comments (`#`) and string escapes;
  - tabs vs spaces;
  - exact continuation rules: §8 says a more-indented line that doesn't start with `|` continues
    the previous arm.
- **Types and units:**
  - numeric sensors with units other than `%`;
  - the types of `now` and `today`;
  - arithmetic between durations and timestamps.
- **Glitch-free evaluation:** when one input change affects several signals, evaluate in
  dependency order, so no binding or machine sees a half-updated state.
- **`every D` and `count(x, D)`:**
  - `every D`: aligned to the clock, or to load time?
  - `count(x, D)`: precision of the time window.
- **Writers:**
  - read-back tolerances (brightness rounding, setpoints);
  - what "matches" means for a multi-attribute `on(...)`.
- **Clock edge cases:**
  - daylight-saving changes for time points and ranges;
  - `sunrise`/`sunset` while `sun.sun` is unavailable.
- **Output details:** the wording of error messages, and the dry-run configuration format.

### Implementation decisions (gaps settled)

Settled during implementation and reviewed by the owner: all accepted, except that restart
handling was changed to follow D17 as written (I1 below reflects the change). Details and
rationale are in `docs/implementation-plan.md` (IDs match); verified HA facts are in
`docs/research-notes.md`.

| ID | Gap | Decision |
|---|---|---|
| I1 | Restart lower bounds, wake-ups | Time quantities are intervals that move with the clock; one mechanism gives exact wake-ups and D17's "at least since boot" bounds. Restart rules are exactly D17's: changes after boot are exact, earlier ones mean "some time before the restart", `manual()` starts as never happened |
| I2 | Glitch-free evaluation | Waves in topological order; machines are sources and settle (chains) before bindings and actions run |
| I3 | Ranges | `now in A..B` is true when `A` occurred more recently than `B` (start inclusive, end exclusive) |
| I4 | DST | Non-existent wall times resolve to the first instant after the gap; ambiguous ones to the first occurrence |
| I5 | `sunset` with `sun.sun` unavailable | Solar times come from `sun.sun` (D4). It only shows *next* occurrences, so homeo remembers each one as it passes; right after a start, the previous one is estimated as next − 24 h. `sun.sun` missing: compile error; unavailable: `unknown` |
| I6 | `every D` | Aligned to local midnight (`every 15min` ticks at :00/:15/…) |
| I7 | `count(x, D)` | Exact event timestamps, window `(now − D, now]` |
| I8 | Machine states | Results must be string literals or `previous(self)`, so the state set is closed |
| I9 | Lexical details | Identifiers as Python; segments after `.` may start with a digit; `#` comments; single-line strings with `\\ \" \' \n \t`; tabs in indentation are errors |
| I10 | Continuation | More-indented lines not starting with `\|` continue the statement or arm (all statements, not only arms) |
| I11 | Names vs domains | Local names shadow builtins, which shadow HA domains (needed for `let remote = …; remote.press`) |
| I12 | `??` precedence | One level tighter than comparisons; comparisons don't chain |
| I13 | `latest` with `\|` groups | Group competes by its guard's occurrence time; sub-arms are a first-match cascade |
| I14 | Entity types | Inferred from domain, then attributes/current state; `any` when unsure |
| I15 | Units | Only `%` is interpreted (scaled for light `brightness`); other units compare as plain numbers |
| I16 | Time arithmetic | `ts − ts → duration`, `ts ± duration`, `time-of-day ± duration`; clock-moving values must be compared before use as results |
| I17 | Cycles | `let` and binding cycles are errors (except via `manual()`); state machines break cycles |
| I18 | Writer read-back | `on(...)` matches when every given parameter matches; per-attribute tolerances; transitional states match their goal; `text.*` accepted like `input_text.*` |
| I19 | Error format | `file:line:col: error: …`, caret snippet, optional `hint:` |
| I20 | Dry run | `# mode: dry_run` directive or an integration option; logs would-be calls |
| I21 | `homeo.explain` | A service with a response; target = binding target, entity, machine or `let` name; per arm: condition value, subexpression values (sugar expanded, `let` names kept), active arm; plus summary, device state, last write |
| I22 | Editor panel | CodeMirror 6, bundled into one committed module (HACS ships only `custom_components/homeo/`); admin-only websocket API (`homeo/files`, `files/subscribe`, `file/read`, `file/check`, `file/save`, `file/delete`, `explain`); errors checked as you type against the other running files; save reloads at once; Explain follows the cursor; unsaved edits kept across pages; saves refuse to overwrite edits made elsewhere |

### Repository state

- **Repository name:** the GitHub repo is `ddfisher/ha-homeo` (formerly `ddfisher/ha-dsl`).
- **Code:** `main` holds the implementation (`custom_components/homeo/`, tests in `tests/`),
  this document, `docs/research-notes.md` and `docs/implementation-plan.md`.
- **Old prototypes:** early prototypes (pyscript code generation, and a Python
  operator-overloading library) survive only in the history of the
  `claude/upbeat-hawking-3wnqwd` branch. They use superseded syntax and approaches; don't build
  on them.

---

## 1. Goals

- **Declarative state (FRP).** Write "what each device should be, given the world",
  e.g. `fan.bathroom := co2 > 800 or humidity > 60%`. The system keeps it true across transitions.
- **Events are just state with a timestamp,** as Home Assistant already stores them. "Was the
  button pressed in the last hour?" is `age(input_button.boost) < 1h`. That makes temporary
  overrides a one-liner, with no imperative timers.
- **Readable by someone who knows a little Python.** Python-flavoured, indentation-based,
  `and`/`or`/`not`, `x if c else y`.
- **Mistakes caught at load time:** unknown entities, type errors, conflicting writers, cycles.
- **A small core, with sugar defined in terms of it.** Sugar like `for` is defined as a rewrite
  into core constructs (§11). More sugar is only added once real rules
  show a need (§11, "maybe later").

Non-goals for v1: replacing HA's UI automation editor; dashboards; arbitrary Python inside rules.

---

## 2. Architecture

| | Status |
|---|---|
| Ships as a **custom integration**, installable via **HACS** (`hacs.json`) | **Decided** |
| Watches `<config>/homeo/*.homeo` and recompiles on save | **Decided** |
| The DSL has its own syntax, compiled inside HA at load time (not a Python library) | **Decided** |
| Compile straight to an in-memory signal graph, not to Python source | **Decided** |
| Reloads are atomic: if a file has errors, keep the old rules running and report | **Decided** |
| v1 keeps no storage of its own: it relies on what HA itself restores, and treats anything older than a restart as "some time before the restart" (§17) | **Decided** |

**Why compile to a graph rather than generate Python?** The compiler's output would only be
runtime objects anyway (signals, bindings, timers). Building them
directly means no code generation, no `exec`, and errors that carry the user's file and line
numbers for free.

**Restarts are rare, so v1 doesn't try to reconstruct history across them.** HA resets
`last_changed` to the boot time for most entities. Rather than keeping our own copy, v1 treats
those change times as "some time before the restart", and only brings devices in line with
their rules once a rule's result is definite (§17). Persistence can be added piece by piece later
if reboots turn out to matter.

Pipeline: `file → parse → AST → resolve names + type-check → graph → Runtime ↔ HassHost`.

---

## 3. Core model (D9, Decided: events as state)

Everything is a **signal**. A signal has:
- a **value** at every moment;
- a **change time**: when that value last changed (tracked by the runtime; see §17 for restarts).

There is no separate event type. Things that "happen" are signals whose value is the **time they
last happened**, exactly as HA stores them: an `event.*` entity's state is the time of its last
event, and a button's state is the time of its last press (§9).

One derived notion is used throughout: the **occurrence time** `t(x)` of a signal used as a
condition.

| `x` is | `x` counts as true when | `t(x)` |
|---|---|---|
| a bool | it is `on`/`true` | when it last became true |
| a timestamp (event, button, `manual()`, time point) | it has ever happened | its value, i.e. when it last happened |

Core constructs that use change times:

| Construct | Meaning | § |
|---|---|---|
| `age(x)` | time since `x` last changed | §10 |
| `latest` block | the arm whose condition happened most recently | §10 |
| `fresh(x)` | inside a state machine: `x` happened after the machine last changed | §13 |
| `on x:` action | run when `x` happens after the action last ran | §14 |

Top-level statements:

| Statement | Meaning |
|---|---|
| **Binding** `fan.x := <signal>` | keep this entity in sync with the signal |
| **Definition** `let stuffy = <signal>` | a named value: an alias for an entity, or anything computed |
| **State machine** `state mode = "init":` + arms | a named signal that remembers (§13) |
| **Action** `on <signal>: <service calls>` | imperative escape hatch (notifications etc., §14). Top-level only. |

Value types: `bool`, `number`, `string`, `duration`, `time-of-day`, `timestamp`. Any value can
also be `unknown`. Type errors come with hints, e.g.
*"`input_button.boost` is a timestamp; did you mean `age(input_button.boost) < 30min`?"*

---

## 4. Grammar base: D1 (Decided)

A **custom, Python-flavoured grammar** with our own parser: a Pratt parser for expressions plus an
indentation-aware lexer. It looks like Python (`and`/`or`/`not`, `x if c else y`, indentation)
but adds what Python can't express: `5min`, `50%`, `22:00`, `for`, `let`, `:=`, `->` arms, `|` guards.

Rejected: strict Python syntax parsed with `ast` plus a token pre-pass. Every temporal form would
need a pre-pass hack or turn into a function call. A hand-written parser also gives better error
messages. The in-HA editor (§19) provides syntax highlighting; a VS Code / TextMate grammar can
follow later.

---

## 5. Names, entities and strings

### D2. Names: `let` and `:=` (Decided)

- **Full entity ids** are always valid: `sensor.bathroom_co2`. Known HA domains are recognised
  lexically, so `sensor.x` is never ambiguous.
- **`let name = expr`** names anything. An alias is just the simplest case:
  ```python
  let co2     = sensor.bathroom_co2                 # alias
  let stuffy  = co2 > 800 or humidity > 70%         # computed value
  ```
- **`:=` is the only way to drive a device,** and its left side is always a **full entity id or
  attribute**: `fan.bathroom_exhaust := stuffy`. A `let` name on the left of `:=` is a compile
  error, even if it's a plain alias. So every driven device is greppable by its real id. (This
  may be relaxed later if it proves tedious.)
- **Every line that changes a device is visibly marked by `:=`.** Plain `=` only appears in `let`
  and `state` declarations.
- **Each file has its own namespace** for `let` and `state` names. To use another file's state
  machine, read its entity (`select.homeo_house`, D13d). State machine names must still be
  unique across files, because their entity ids would collide.
- **Idea for v2:** area-scoped blocks (`in area bathroom: fan := co2 > 800`), resolved through
  HA's area registry. This would relax the full-id rule inside such blocks.

Unknown entity ids are errors, with "did you mean". The check runs against HA's entity registry
when the rules load.

### D3. Attributes (Decided)

`light.hall.brightness`: the third segment is an attribute. The one exception is the `event`
domain, where the third segment filters by event type (see D8).
Attribute names that aren't identifiers use `light.hall["rgb color"]`.

### D3b. String states (Decided)

- `on`/`off` and `true`/`false` are keywords.
- Other states are quoted strings: `person.ann == "home"`, `climate.lr.hvac_action in ["heating", "idle"]`.
- Rejected: bare words checked against the entity's known states (`person.ann == home`). They
  collide with local names.

---

## 6. Literals & units: D4 (Decided)

| Literal | Meaning |
|---|---|
| `30s`, `5min`, `2h`, `1d`, `1h30min` | durations. `m` is deliberately **not** minutes (it reads like metres). |
| `50%` | percent. Compared with a `%` sensor, it means 50. As a brightness (`on(brightness: 50%)`) it's sent as `brightness_pct`, and compared with HA's 0–255 `brightness` when read back. |
| `07:00`, `22:30` | time of day (in ranges and comparisons), or a time point (§12) |
| `sunrise`, `sunset`, `sunset - 30min`, `dawn`, `dusk` | solar times (from `sun.sun`) |
| `mon`…`sun`, `weekday`, `weekend` | days |
| `"text"` | string |

Numbers are compared with sensor values after parsing the state as a float. A non-numeric state
is `unknown`, not an error.

---

## 7. Bindings and targets: D5 (Decided)

```python
fan.bathroom := co2 > 800 or humidity > 60%           # → fan.turn_on / fan.turn_off
light.hall := on(brightness: 80%, kelvin: 2700) if motion else off
climate.living.temperature := 21 if home else 17      # → climate.set_temperature
cover.blinds.position := 0 if now in sunset..sunrise else 100
```

- **`:=` binds (Decided, D2).**
- **One writer per target (Decided).** Two bindings for the same entity (or the same attribute)
  are a compile error, across all files. Combine them with a cascade (§8).
- **`keep` (Decided)** is a special value meaning "don't drive this right now". The entity is
  left as it is. When the expression stops being `keep`, the device's current state stands in
  for the previous result, as after a restart (D16). So the device is brought back in line if it
  differs. This is the basis of override handling (§15).

### Writers (Decided)

HA has no "set the state of this device". You change a device by **calling a service**, and the
right service and its data depend on the domain and on what you're setting. A **writer** is the
built-in rule for one kind of target that knows three things:

1. **How to write a value:** which service call(s) to make.
2. **How to read it back:** which state or attribute to compare, in which units. "Write only on
   change" (D15) and manual-change detection (D21) need this. For example, we write
   `brightness_pct: 80`, but HA reports `brightness: 204` on a 0–255 scale.
3. **Whether the target is writable at all.** `sensor.x := 5` and read-only attributes like
   `light.hall.color_mode` are compile errors.

v1 writers:

| Target | Example | Service call |
|---|---|---|
| on/off: `switch`, `fan`, `light`, `input_boolean`, `siren` | `fan.bathroom := on` | `<domain>.turn_on` / `turn_off` |
| light with settings | `light.hall := on(brightness: 80%, kelvin: 2700)` | `light.turn_on(brightness_pct: 80, color_temp_kelvin: 2700)` |
| fan with speed | `fan.bathroom := on(percentage: 60%)` | `fan.turn_on(percentage: 60)` |
| climate setpoint | `climate.living.temperature := 21` | `climate.set_temperature` |
| climate mode | `climate.living := "heat"` | `climate.set_hvac_mode` |
| cover position | `cover.blinds.position := 30%` | `cover.set_cover_position` |
| cover open/closed | `cover.blinds := "open"` | `cover.open_cover` / `close_cover` |
| numbers | `input_number.x := 5`, `number.x := 5` | `set_value` |
| selects | `input_select.x := "eco"`, `select.x := "eco"` | `select_option` |
| text | `input_text.x := "…"` | `set_value` |
| lock | `lock.front := "locked"` | `lock.lock` / `lock.unlock` |
| media volume | `media_player.tv.volume := 30%` | `media_player.volume_set` |

Decisions:

- **A light is bound as a whole.** Setting a light's brightness turns it on, so
  `light.hall := off` and `light.hall.brightness := 50%` as separate bindings would fight.
  Lights take `off`, or `on(brightness: …, kelvin: …)`, and binding a light's attributes
  separately is a compile error.
  - **Fans work the same way** (`off` or `on(percentage: …)`), since setting a speed also turns a
    fan on.
  - **A cover** is bound either by state (`"open"`/`"closed"`) or by `position`, not both.
  - **Climate's mode and setpoint** use independent services, so separate bindings are fine
    there. The same goes for media volume.
- **Anything not in the table** (vacuums, alarm panels, water heaters, …) is handled with actions
  in v1 (`on x: vacuum.start()`). v2 may add user-defined writers that name the service to call
  for each value.

---

## 8. Priority cascades: D6 (Decided: `cond -> value` arms, `|` for guarded sub-arms)

Most real rules are a **priority list**: several reasons a device might be on or off, where some
reasons beat others. Cascades are the dedicated layout for that:

```python
fan.bathroom :=
    age(remote.press) < 30min          -> on          # temporary boost
    window                             -> off
    else                               -> stuffy

climate.living.temperature :=
    binary_sensor.living_window        -> 7           # frost protection only
    not anyone_home for 2h             -> 15
    age(input_button.warm_up) < 2h     -> 22
    now in 23:00..06:30                -> 17
    else                               -> 20.5

light.hall :=
    age(manual(light.hall)) < 1h       -> keep
    motion or age(motion) < 5min                      # a guard with sub-arms
        | night                        -> on(brightness: 10%, kelvin: 2200)
        | else                         -> on(brightness: 100%)
    else                               -> off
```

- **One arm per line: `condition -> result`.** First match wins, top to bottom.
- **Guards with sub-arms:** a condition with no `->`, followed by indented `|` lines, is a group.
  Its sub-arms only apply while the condition is true. The group matches only if one of its
  sub-arms matches; otherwise evaluation falls through to the next arm. Groups can nest.
- **Parsing is unambiguous:** a line starting with `|` is a sub-arm. Any other more-indented
  line continues the previous arm, so long conditions can wrap.
- **A final `else` is required in bindings** (compile error otherwise). State machines differ
  (§13).
- **Values can be anything:** `on`/`off`, numbers, `on(brightness: …)`, `keep`, or another
  expression.
- **The cascade is one expression,** re-evaluated whenever any input changes. The binding writes
  when the result changes. That includes moving from one arm to another, e.g. when a
  higher-priority condition stops being true.
- **Temporary overrides fall out naturally.** An override is a high-priority arm whose condition
  turns false on its own over time, like `age(remote.press) < 30min`. When it expires, the next
  arm takes over and the device re-syncs. No timers, no "restore previous state" logic.
- An `unknown` condition falls through to the next arm (D16).
- Python's `x if c else y` remains available for short one-liners.

The same arm syntax is used by `latest` blocks (§10) and state machines (§13).

---

## 9. Events as state: sources (D7, Decided)

Each of these is a **timestamp signal**: its value is the time it last happened.

| Source | Syntax | Notes |
|---|---|---|
| Event entity (HA 2023.8+: remotes, doorbells, buttons) | `event.hall_remote` | HA's own state. Its attributes (`event_type`, …) describe the last event. |
| One event type | `event.hall_remote.double_press` (D8) | Time of the last event of that type, tracked by the runtime. After a restart, only the latest event's type is known (from HA's state); other types count as "before the restart" (§17). |
| Button helpers / button entities | `input_button.boost`, `button.x` | HA's own state: time of the last press |
| Manual control | `manual(fan.bathroom)` | Time of the last change not made by homeo (§15) |
| Time points | `07:00`, `sunset - 30min` | Time of the most recent occurrence (§12) |
| Periodic | `every 10min` | Time of the most recent tick |
| Raw bus events | `bus.zha_event(device_ieee: "…", command: "on")` | For integrations without event entities. Time of the last matching event, tracked by the runtime ("before the restart" after a restart). |

Because these are ordinary signals, they work with every construct: `age()`, `latest`,
`fresh()`, `on`, and `previous()`. There's no separate event type, so there are no
event/signal conversion rules.

### D8. Event-type filtering for `event.*` entities (Decided)

`event.hall_remote.double_press` is the time of the last `double_press` event. It's consistent with
attribute syntax. Attributes of event entities are rarely needed; if one is, use
`event.x["event_type"]`.

Rejected:
- `event.hall_remote("double_press")`: a function-call form for no gain;
- `event.hall_remote.event_type == "double_press"`: this only describes the *last* event, so a
  later single press hides it.

---

## 10. Time since, and "most recent wins"

### D10. `age(x)` (Decided)

**`age(x)`: time since `x` last changed.** It's the one primitive for recency:

```python
let boost_active = age(input_button.boost) < 30min     # pressed in the last 30 min
let stale        = age(sensor.co2) > 1h                # sensor stopped reporting
let override     = age(manual(fan.bathroom)) < 1h      # changed by hand in the last hour
light.hall := motion or age(motion) < 10min            # motion now, or ended < 10 min ago
```

- For timestamp signals, the change time *is* the value, so `age(remote.press)` is "time since
  the last press".
- `age(x)` of an expression tracks when the expression's value last changed, e.g.
  `age(co2 > 800)`.
- Comparisons like `age(x) < D` need no polling. The runtime knows exactly when the comparison
  will flip and schedules a wake-up then.
- `age(door) < 5min` counts closing as well as opening. "Opened in the last 5 min" is
  `door and age(door) < 5min`.

### D19. `latest` blocks (Decided; the name may still change)

**The arm whose condition happened most recently wins.** "Happened" means `t(x)` from §3: the last
press for buttons and events, and the moment it last became true for bool conditions.

```python
light.desk := latest
    input_button.desk_on       -> on
    input_button.desk_off      -> off
    else                       -> keep         # neither has ever been pressed

let blinds_mode = latest
    input_button.blinds_auto   -> "auto"
    manual(cover.blinds)       -> "manual"     # a manual change sticks until "auto" is pressed
    else                       -> "auto"
```

- **Arms use the same syntax as cascades** (`cond -> result`, with `|` sub-arms). Results can be
  any expression.
- **`else`** is used when none of the conditions has ever happened. It's required in bindings,
  as with cascades, and is usually `keep` ("no opinion yet"). In a `let`, it may be left out; the
  value is then `unknown` until one of the conditions has happened (D16).
- **Conditions must be bools or timestamps.** Anything else is a type error.
- **Ties** (same occurrence time): the first arm wins.

### Other history functions (Decided)

| | Meaning |
|---|---|
| `previous(x)` | the value before `x` last changed |
| `toggle(x, initial: off)` | flips each time `x` happens, e.g. a button |
| `count(x, 10min)` | how many times `x` happened in the last 10 min, e.g. triple-press detection. The runtime keeps this history itself, since HA only stores the latest. |

After a restart (§17):
- `toggle()` restarts from its `initial` value (no storage in v1);
- `count()` only counts what happened since the restart;
- `previous(x)` is `unknown` until `x` changes again.

---

## 11. Temporal sugar: D11 (Decided)

Sugar is **defined as a rewrite into core constructs**, not as a separate feature. Error
messages and `homeo.explain` can show the expansion.

| Sugar | Expands to | Status |
|---|---|---|
| `x for D` | `x and age(x) >= D` | **Decided** |

```python
light.porch := lux < 20 for 5min                 # = lux < 20 and age(lux < 20) >= 5min
```

Hysteresis needs no sugar; it's a `latest` block. Each condition counts from the moment it last
*became* true, so between 700 and 800 the fan keeps whichever state it was last switched to:
```python
fan.bedroom := latest
    co2 > 800                  -> on             # turns on when co2 rises above 800
    co2 < 700                  -> off            # turns off when co2 falls below 700
    else                       -> keep           # neither has happened yet: leave the fan alone
```

### Maybe later

Possible sugar, **not planned for v1**. We'll add these only if real rules keep needing them:

| Sugar | Would expand to |
|---|---|
| `with override D:` | adds `age(manual(target)) < D -> keep` as the first arm of each binding inside the block (§15) |
| `with override never:` | a binding option rather than a rewrite: after a manual change, the binding writes its current result again straight away (pumps, safety rules) |
| `a until b` (latch) | a `latest` block with arms `a -> true`, `b -> false` and no `else` (`unknown` until either has happened) |
| `x debounced D` / `with debounce D:` | write only once the result has been stable for D (`age(result) >= D`) |
| `with min_on D1, min_off D2:` | equipment protection: once switched, stay in that state at least this long |

---

## 12. Time: D12 (Decided)

Time-of-day **ranges** are bool signals:
```python
let night   = now in 22:00..06:00              # wraps midnight
let evening = now in sunset - 30min..23:00
let workday = today in mon..fri and not binary_sensor.holiday
```

A time on its own, where a condition is expected, is a **time point**: a timestamp signal whose
value is its most recent occurrence. It behaves like a button that HA presses every day at that
time:
```python
fresh(07:00) and workday       -> "home"      # 07:00 has passed since entering this state
on sunset - 30min:                               # action, daily
```

So `07:00` means a time of day inside `now in …` ranges and comparisons, and a time point
elsewhere. That's unambiguous for the compiler. **Decided:** `fresh(07:00)` as written. We'll
revisit (e.g. an explicit `at(07:00)`) only if it causes confusion in practice.

---

## 13. State machines (TEA `update`): D13

### When you need one

Derived values (`let` definitions and `:=` bindings) can't remember *how they got here*. `age()`, `latest` and
`toggle()` cover simple memory ("pressed in the last 30 min", "last button wins"). A state
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
fan.bathroom :=
    bath_mode == "boost"   -> on
    bath_mode == "off"     -> off
    else                   -> stuffy
```

### D13a. Shape: a TEA reducer (Decided, provisionally; revisit after real use)

`state name = initial:`, followed by arms that compute the new value. Guards with `|` sub-arms
(§8) let you group arms by state, by trigger, or not at all:

```python
state bath_mode = "auto":
    fresh(remote.long_press)                    -> "off"
    fresh(remote.press)                                       # grouped by trigger
        | bath_mode == "auto"                   -> "boost"
        | else                                  -> "auto"
    bath_mode == "boost" for 1h                 -> "auto"
    bath_mode == "off" for 2h                   -> "auto"

state washer = "idle":
    washer != "running" and washer_power > 10 for 2min      -> "running"
    washer == "running" and washer_power < 3 for 5min       -> "done"
    washer == "done" and fresh(binary_sensor.washer_door)   -> "idle"

on washer == "done":                                        # entry action = ordinary action
    notify.phone(message: "Washing is done")

state house = "home":
    house == "home"                                         # grouped by state
        | not anyone_home for 10min             -> "away"
        | fresh(input_button.goodnight)         -> "night"
        | fresh(input_button.movie)             -> "movie"
    house == "away"
        | anyone_home                           -> "home"
    house == "night"
        | fresh(07:00) and workday              -> "home"
        | fresh(09:00)                          -> "home"
        | fresh(kitchen_motion and now in 05:00..07:00) -> "home"
    house == "movie"
        | fresh(input_button.movie)             -> previous(house)
        | age(house) > 4h                       -> previous(house)
```

### D13b. Semantics: arms are plain conditions (Decided)

- **Whenever an input changes,** the arms are evaluated top to bottom against the current value.
  The first arm that matches gives the new value. If no arm matches, the value is unchanged, so
  no final `else` is needed.
- **Arms read the value from before the update,** as in TEA. So `bath_mode == "auto"` inside
  `bath_mode`'s own arms is allowed, and isn't a cycle.
- **There are no triggers or edges.** Arms are ordinary conditions, so they're level-triggered.
  `house == "away"` with the sub-arm `| anyone_home -> "home"` also fires if the machine enters
  `away` while someone is home. When you only want what happened *after* entering the current state, use
  `fresh()`.
- **Timeouts** are ordinary conditions: `bath_mode == "boost" for 1h`, or `age(house) > 4h`.
- **Chains:** if a transition makes another arm match, it is applied too. The same value
  appearing twice in one step is a loop, which is reported as an error, and the machine stops.
- **Restarts** (§17): the machine's value and change time are restored from its `select` entity
  (HA's restore state), and the arms are re-evaluated.
  - Arms on exact timestamps still work: a 07:00 that passed while HA was down, after entering the
    state, is still `fresh`.
  - Arms that depend on change times from before the restart are `unknown` until they can be
    settled (§17 rule 2). That covers `fresh()` of a bool, `age()` and `for`.
  - Plain conditions (`anyone_home`) evaluate normally.

### D20. `fresh(x)` (Decided)

Inside a `state` block: **`x` happened after this machine last changed**, i.e. `t(x)` is later
than the machine's change time.

- Buttons and events: pressed or fired since the current state was entered.
- Bool conditions: became true since the current state was entered.
- After a transition, the machine's change time is "now", so everything that caused it is no
  longer fresh. Arms built on `fresh()` therefore never loop.
- Using `fresh()` outside a `state` block is a compile error. Actions have the same idea built in
  (§14).

### D13c. Referring to states (Decided: string comparison)

`bath_mode == "boost"`, `bath_mode in ["boost", "off"]`. These are checked at compile time
against the inferred states (the initial value, literal results and `previous()`), so a typo like
`"boots"` is an error. The usual tools apply: `age(bath_mode)`, `previous(bath_mode)`, and
`on bath_mode == "done":`.

### D13d. Visibility in HA (Decided: writable `select` entity)

Each machine is exposed as `select.homeo_<name>`. It shows in history and on dashboards, and can
be set from the UI. A manual selection is an ordinary change: `age()` resets, `fresh()` is
relative to it, and `on` actions for the new value run. The entity also persists the machine's
state across restarts (restore state).

### Other details (Decided, D13f)

- **Reload:** when a rules file is edited, the machine keeps its current state if that state
  still exists. Otherwise it resets to the initial state, with a log message.
- **Deferred to v2:**
  - nested (hierarchical) states;
  - extended state, i.e. variables stored alongside the mode, like a counter or remembered
    brightness.

---

## 14. Actions and effects: D14 (Decided)

For things that aren't state: notifications, TTS, flashing a light, running a script.

```python
on input_button.doorbell:
    notify.mobile_app_phone(message: "Someone's at the door")

on person.ann == "home" if night:
    light.porch.flash()            # entity-scoped service call: light.flash on light.porch

on humidity > 90% for 30min:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

### `on` is top-level only

`on x [if g]:` is a **top-level statement**, the same kind of thing as a binding or a `state`
declaration. It can't appear anywhere else: not inside a `state` block, a binding, a cascade, a
`latest` block, an expression, or another action's body. Each of those is a
compile error with a hint:
- inside a state machine: *"use `fresh(x)` for 'happened since entering this state'"*;
- inside a binding or expression: *"use `age(x) < D` for 'happened recently'"*.

This keeps the split clean. State machines and bindings are pure functions of signals and change
times, and every effect in a file is visible at the top level.

### Why `on` is still needed

Everything else in the language describes **values**. The runtime may evaluate them any number
of times, re-evaluates them after restarts, and only *changes* reach HA. Bindings write only when
their result changes, and they step aside for `keep`. That's exactly right for state, and exactly
wrong for **effects**, where what matters is *how many times* something happens:

- one notification per doorbell press: not zero (if a value happens not to change), and not two
  (if the rules are re-evaluated after a restart);
- not suppressed by an override, and not "re-synced" later.

Getting that right needs bookkeeping: which occurrences have already been handled. **`on` is the
one construct with that bookkeeping.** In React terms, bindings are `render` (pure and
idempotent) and `on` is `useEffect`. In Elm terms, `on` produces a `Cmd`.

It isn't a separate mechanism, though. Each action keeps one timestamp, "last handled" (in memory;
§17 covers restarts).
**`on x if g: body` means: whenever `t(x)` is later than the last-handled time, set last-handled
to `t(x)`, and run `body` if `g` is true.** That's the same comparison as `fresh()`, relative to
the action instead of a state machine.

### `if` vs `and` in the header

Both are meaningful, and they differ:

| | Fires when |
|---|---|
| `on person.ann == "home" if night:` | Ann **arrives** while it's night. A daytime arrival is marked handled without running, so nightfall later doesn't trigger it. In FRP terms, this filters the arrival events. |
| `on person.ann == "home" and night:` | "Ann home and it's night" **becomes true**: she arrives at night, *or* night falls while she's home. |

### D22. Declarative notifications (Decided: not in v1)

Many actions are really notifications, and notifications can be state. The HA mobile app
supports a `tag` (a newer notification replaces the old one) and `clear_notification`. So a
notification could be a binding, React-style:

```python
notify.mobile_app_phone :=
    washer == "done"      -> "Washing is done"
    else                  -> none
```

This sends the message when the value changes to it, and **clears it from the phone** when it goes
back to `none` (here, when the washer leaves `done`). Restarts are handled by the same rules as
other bindings (§17). It doesn't cover true one-offs (a ding per
doorbell press, TTS, `flash()`), so `on` would remain for those.

**Decided:** `on x [if g]:` is the single effect construct. Declarative notification targets are
not in v1; they're a candidate for a later version.

### Other details (Decided)

- **After a restart, everything that happened before it counts as handled** (§17). There's no
  catch-up in v1.
- `x` in `on x` must be a bool or a timestamp, like `latest` conditions.
- On first load, "last handled" starts at "now", so nothing from the past fires. Reloads keep it
  for actions that still exist.
- Service calls are written `domain.service(arg: value)`, or `entity.service(…)` to set the
  target.
- Actions can read signals but can't call services on entities that have bindings (compile
  error), which prevents fights.
- There's no `sleep`/`wait` in v1: if you need delays, use state.

---

## 15. Manual control and overrides

### D15. Default (Decided: edge-triggered)

When a human changes a device that a binding drives, **the change sticks until the binding's
computed value next changes.** The binding writes only on changes to its own result, so it never
fights a manual change in between.

For a time limit, add a `manual()` arm that yields with `keep` (no new concepts):

```python
fan.bathroom :=
    age(manual(fan.bathroom)) < 1h   -> keep      # yield for an hour after a manual change
    else                             -> stuffy
```

Shorthand for this (`with override 1h:`) and a re-assert-immediately option
(`with override never:`) are "maybe later" sugar (§11).

When the hour is up, the `keep` arm stops matching. The device's current state stands in for the
previous result (§7, D16), so the device is brought back in line if it differs.

`manual(target)` is the one allowed self-reference, so it's exempt from cycle detection.

Rejected alternatives:
- a timed back-off by default, because it surprises you when you forget it's there;
- always enforcing by default, because it's wrong for lights and fans.

### D21. Detecting manual changes (Decided)

HA attaches a **`Context`** (`id`, `parent_id`, `user_id`) to every state change. When homeo
calls a service, it creates the context and records its id. HA entities then use that context
for the state writes caused by the call.

A change to a driven entity counts as **manual** when all of these hold:
1. **Its context isn't one of ours.** It was caused by:
   - a person in the UI or app (`user_id` set);
   - a physical switch or the device itself (no `user_id`, no `parent_id`);
   - another automation or script (`parent_id` set).
2. **It moves the entity away from what homeo last wrote.** This filters out late reports of
   our own writes. HA only reuses a service call's context for writes within about 5 seconds,
   so a slow device that reports "on" 10 s later shows up with a fresh context. Because it
   matches what we wrote, it isn't counted.
3. **It isn't a transition to or from `unavailable`/`unknown`.** A device dropping off the
   network and coming back isn't a human action.

Changes made by *other* HA automations and scripts count as manual too, so they're respected like
a person's. (A way to restrict this to people only can be added later if needed.)

Known limits:
- A device that changes on its own (e.g. a smart plug's built-in schedule) looks like a physical
  press.
- Attribute targets (brightness, setpoints) need a tolerance when comparing with what we wrote,
  e.g. brightness 127 vs 128 after rounding.

---

## 16. Unknown / unavailable: D16 (Decided)

- Three-valued logic: `on or unknown` = `on`, `off and unknown` = `off`, otherwise `unknown`.
- If a binding's result is `unknown`, the target is left alone (an implicit `keep`).
- **A binding writes when its result changes from one known value to a different known value.**
  "Previous" means the last *known* result, skipping `unknown` periods:
  - `on → unknown → off` writes `off`;
  - `on → unknown → on` writes nothing;
  - if there is no previous known result (after a restart, for a brand-new binding, or after the
    result was `keep`), the **device's current state** stands in for it. The binding writes its
    first known result only if the device doesn't already match it.
- Fallbacks: `co2 ?? 0`, or `available(sensor.co2)` as an explicit test.
- In cascades and `latest`, an `unknown` condition falls through to the next arm. (The
  alternative, making the whole result `unknown`, is safer but brittle: one flaky sensor would
  freeze the whole rule.)
- **Change times skip over `unknown` periods.** If a value is the same before and after a dropout,
  its change time doesn't move, so `age()`, `fresh()`, `on` and `latest` don't see a spurious
  change. (`x for D` therefore isn't broken by a brief dropout.)

---

## 17. Startup and restarts: D17 (Decided)

Restarts are rare, so v1 aims for **simple, predictable** behaviour, not exact catch-up.
**Principle: after a restart, devices are brought in line with their rules, but only once a
rule's result is definite.**

**What survives a restart without any storage of ours:**
- **Timestamp entities** (`event.*`, `input_button.*`, `button.*`): their state *is* the time,
  so `age()`, `latest` and `fresh()` on them stay exact.
- **Time points** (`07:00`, `sunset`): computed from the clock.
- **State machines:** HA restores their `select` entity, including its change time. After a
  crash it can be up to ~15 minutes stale, since HA saves restore state periodically.
- **Current entity states:** HA re-reads them from the devices.

**What's lost:**
- the change times of everything else (HA resets `last_changed` to the boot time);
- `manual()` history;
- what we last wrote.

**Rules:**

1. **Wait for HA's `started` event,** then evaluate everything.
2. **Change times from before the restart mean "some time before the restart".** `age(x)` is
   then only known to be *at least* the time since boot. Comparisons that this settles are
   definite; the rest are `unknown` (D16) until they can be decided. For example:
   - `age(x) > 1h`, two hours after boot: true;
   - `age(x) < 10min`: `unknown` for the first 10 minutes, then false;
   - `x for 10min`: `unknown` for the first 10 minutes. The `for` simply waits its full duration
     again;
   - `fresh(x)`, for a bool `x` that hasn't changed since boot: `unknown`, so the arm doesn't
     fire;
   - `latest`: anything that happened after boot beats anything from before it. Two pre-restart
     occurrences can't be ordered, so the result is `unknown`.

   So `light.hall := motion or age(motion) < 10min` doesn't switch the light on after a reboot.
3. **Bindings need no restart rule of their own.** A binding's previous result isn't stored, so
   by D16 the device's current state stands in for it. As soon as a rule's result is definite,
   the device is brought in line if it doesn't already match. This means:
   - a device that should have changed while HA was down catches up at boot (e.g. blinds that
     should have opened at 07:00);
   - results that are `unknown` right after boot (rule 2) wait until they're definite, so a motion
     light isn't switched on because of the reboot;
   - **accepted cost:** a manual change made before the restart is overridden if its rule
     disagrees, since `manual()` history isn't kept. Restarts are rare enough for this to be
     fine in v1.
4. **Actions treat everything before the restart as handled.** No catch-up.
5. **State machines** keep their restored value and re-evaluate (§13). A 07:00 transition missed
   during downtime still happens at boot. Arms that depend on pre-restart change times wait until
   they can be settled.
6. **Rule reloads are not restarts.** Reloads happen on every save, so they must not undo manual
   changes. When a file is edited:
   - each binding that still exists keeps its previous known result, so an edit that changes a
     rule's result applies immediately and unrelated edits leave devices alone;
   - actions keep their "last handled" time;
   - state machines keep their state (D13f);
   - a brand-new binding starts from the device's current state, as after a restart.

**Things we could add later, each on its own, if reboots turn out to matter more:**
- persist `manual()` timestamps, so timed overrides survive restarts;
- persist each action's last-handled time, for capped catch-up of missed actions;
- persist last-written values, so a manual change made before a restart survives it;
- read change times from HA's recorder, when it's enabled and keeps the entity, for exact `age()`.


---

## 18. Abstraction: D18 (Decided: deferred to v2)

Repeated patterns, like one fan rule per bathroom:

```python
def bath_fan(fan, co2, humidity, remote):
    fan := …                      # a template: expands at compile time
bath_fan(fan.main_bath, sensor.main_co2, sensor.main_humidity, event.main_remote)
bath_fan(fan.guest_bath, …)
```

Or pure functions over signals: `def stale(s, d) = age(s) > d`. Both are compile-time macros,
with no runtime cost. Leaving this out of v1 is fine.

---

## 19. Errors, tooling, diagnostics (Decided)

- Compile errors appear as an HA **repairs issue / persistent notification**, with file, line,
  column and a caret snippet. Old rules keep running until the file compiles.
- **Dry-run mode** (per file: `# mode: dry_run`, or an integration option) logs the writes it
  would make and the actions it would run, instead of doing them.
- **`homeo.explain`** service: given a target, it reports the current value of each
  subexpression, with sugar expanded, and which arm is active ("fan.bathroom is ON because
  `age(remote.press) < 30min` (pressed 12 min ago)").
- Optional diagnostic sensor per binding, whose attributes hold the active arm and reason.
- Service calls carry a `Context` that HA's logbook attributes to homeo and the rule's
  file:line.
- **Editor in HA's UI** (the final v1 step). A Homeo sidebar panel lists the `.homeo` files and
  edits them with:
  - **syntax highlighting** for the language;
  - **inline errors:** the panel sends the text to the integration, which compiles it without
    loading it and returns errors with line and column. These are shown as squiggles and
    messages in the editor as you type;
  - **save** writes the file, which triggers the normal (atomic) reload.

  Implementation details (editor component, API between panel and integration) are left to
  that step.

---

## 20. Operator precedence (loosest to tightest)

| | |
|---|---|
| `cond -> result` (arms), `\| cond -> result` (sub-arms) | block structure: one arm per line |
| `on x if g:` (action header) | |
| `x if c else y` | |
| `or` | |
| `and` | |
| `not` | |
| `for` (sugar, §11) | `not motion for 5min` = `not (motion for 5min)` |
| `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `??` | |
| `+`, `-` | |
| `*`, `/` | |
| unit literals, `.attr`, calls | |

So `co2 > 800 for 5min or humidity > 60%` means `(co2 > 800 for 5min) or (humidity > 60%)`.

---

## 21. Full example (v1 syntax)

```python
# bathroom.homeo
let co2      = sensor.bathroom_co2
let humidity = sensor.bathroom_humidity
let remote   = event.bathroom_remote
let window   = binary_sensor.bathroom_window
let motion   = binary_sensor.bathroom_motion

let co2_high = latest                 # hysteresis: on above 800, off below 700
    co2 > 800                         -> true
    co2 < 700                         -> false     # no else: unknown until co2 crosses either
let stuffy   = co2_high or humidity > 70% for 2min
let night    = now in 22:00..07:00

state bath_mode = "auto":
    fresh(remote.long_press)            -> "off"
    fresh(remote.press)
        | bath_mode == "auto"           -> "boost"
        | else                          -> "auto"
    bath_mode == "boost" for 30min      -> "auto"
    bath_mode == "off" for 2h           -> "auto"

fan.bathroom :=
    age(manual(fan.bathroom)) < 1h      -> keep         # yield for an hour after a manual change
    bath_mode == "boost"                -> on
    bath_mode == "off"                  -> off
    window                              -> off
    night and not humidity > 85%        -> off
    else                                -> stuffy

light.bathroom :=
    motion or age(motion) < 5min        -> on(brightness: 10% if night else 100%)
    else                                -> off

on humidity > 90% for 30min:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

---

## 22. Decision log

| ID | Topic | Status | Decision |
|---|---|---|---|
| D0 | Name / file extension | **Decided** | Homeo; `.homeo` files in `<config>/homeo/`; integration domain `homeo` |
| D1 | Grammar base | **Decided** | custom Python-flavoured grammar, own parser |
| D2 | Names | **Decided** | `let name = expr` for aliases and computed values; `:=` binds full entity ids only; one namespace per file |
| D3 | Attributes / string states | **Decided** | `entity.attr`; quoted strings, `on`/`off` keywords |
| D4 | Units & literals | **Decided** | `5min`, `50%`, `22:00`, `sunset - 30min` |
| D5 | Bindings & targets | **Decided** | `:=`; one writer per target; `keep` (re-syncs from device state when it ends); built-in writer table; lights and fans bound as a whole; other domains via actions in v1 |
| D6 | Priority cascades | **Decided** | `cond -> value` arms, `\|` for guarded sub-arms, first match wins, `else` required in bindings |
| D7 | Event sources | **Decided** | timestamp signals; table in §9 |
| D8 | Event-type filter | **Decided** | `event.remote.double_press` |
| D9 | Events | **Decided** | no event type: events are timestamp-valued state, as in HA |
| D10 | Recency | **Decided** | `age(x)`: time since `x` last changed |
| D11 | Temporal sugar | **Decided** | `x for D` = `x and age(x) >= D`; `with override`, `until`, debounce, `min_on` are "maybe later" |
| D12 | Time | **Decided** (time points provisional) | `now in 22:00..06:00` ranges; `fresh(07:00)`: a bare time is a time point |
| D13a | State machine shape | **Decided (provisional)** | `state m = init:` + arms (TEA reducer); revisit after real use |
| D13b | State machine semantics | **Decided** | arms are level-triggered conditions; chains with loop detection; restarts per D17 |
| D13c | Referring to states | **Decided** | string comparison, compile-time checked |
| D13d | Visibility in HA | **Decided** | writable `select.homeo_<name>` |
| D13f | Reload & v2 deferrals | **Decided** | keep state across reloads if it still exists; nested states and extended state deferred to v2 |
| D14 | Actions / effects | **Decided** | `on x [if g]:` is the one effect construct, top-level only; once per occurrence; no catch-up after restarts (D17) |
| D15 | Manual-override default | **Decided** | edge-triggered; timed overrides via `age(manual(x)) < D -> keep` |
| D16 | Unknown handling | **Decided** | Kleene logic; unknown → keep; change times skip dropouts; a binding writes when its known result changes (device state stands in when there's no previous result, e.g. after a restart or `keep`) |
| D17 | Restarts & reloads | **Decided** | no storage of our own in v1; pre-restart change times are lower bounds; devices synced to definite results at boot; reloads keep bindings', actions' and machines' state |
| D18 | Abstraction | **Decided: v2** | compile-time templates |
| D19 | Most-recent-wins | **Decided** (name tentative) | `latest` block with `cond -> value` arms |
| D20 | "Since entering this state" | **Decided** | `fresh(x)`, only inside `state` blocks |
| D21 | Manual-change detection | **Decided** | HA `Context` + differs from last write + not (un)availability; other automations count |
| D22 | Declarative notifications | **Decided: not in v1** | maybe later: `notify.x := <message or none>` using tags/clear |

---

## Appendix: alternatives considered

### A1. Cascade layouts (D6)

- **`|` on every arm** (`| cond -> value` everywhere): too noisy. `|` is now used only for the
  sub-arms under a guard, where it replaces the earlier `cond ->` followed by an indented block.
- **Value first** (`value when cond`): outputs line up on the left, but long values push
  conditions off to the right, and nesting reads awkwardly.
- **Chained ternaries only:** no new syntax, but hard to read past three branches.

### A2. State machine shapes (D13a)

- **Per-state blocks.** Readable by mode, but they needed a growing list of special syntax:
  any-state arms, `after D`, `on enter:`, multi-state headings, `back`, and an implicit initial
  state. Each of these is now an ordinary expression (`age`, `on`, `in`, `previous`).
- **Transition table** (`from -> to when trigger`): repeats state names, and `->` meant
  "from → to" instead of "condition → result".
- **Grouped by trigger** (`on remote.press: auto -> boost`): best for remote-control machines,
  awkward for condition-driven ones. A `fresh(remote.press)` guard with `|` sub-arms gives the
  same grouping.

### A3. A separate event type (previous D9)

Events as their own type, with `on X` marking instants everywhere (`on door` = "becomes true"),
`since(E)`, and a trigger/gate rule for state-machine arms. It worked, but needed edge semantics,
special restart rules ("compare with the persisted value") and a rule for what fires each arm.
Treating events as timestamp-valued state, as HA does, removed all three.

### A4. Recency and ordering syntax

- `since(E)` / `since(S)`: replaced by `age(x)`.
- `E within D`, `recent(E, D)`, `E.ago`: superseded by `age`.
- `x after y` (generic "changed more recently than"): replaced by `latest` blocks for
  last-wins logic and `fresh()` inside state machines.
- `new x`: renamed `fresh(x)`, because `new` means something very different in most languages.
