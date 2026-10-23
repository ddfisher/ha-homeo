# Homeo: implementation and testing plan

Companion to `docs/design.md` (the spec) and `docs/research-notes.md` (verified HA facts).
This document says **how** v1 gets built and tested, and records the decisions taken where the
spec left gaps. Each such decision has an ID (`I1`, `I2`, …), is summarised in the design doc's
"Implementation decisions" section, and is flagged to the owner for review.

---

## 1. Architecture in code

```
custom_components/homeo/
  manifest.json, hacs-facing metadata, strings.json, translations/en.json, services.yaml, brand/
  __init__.py        config-entry setup and the homeo.reload service (HA imported lazily)
  manager.py         compiling, atomic reloads, repairs issues, boot time, select entities
  watcher.py         file watcher (watchdog/inotify) for <config>/homeo/*.homeo
  config_flow.py     single-instance config flow, dry-run option
  hass_host.py       HassHost: the HA implementation of the core's Host interface
  select.py          select.homeo_<name> entities for state machines (RestoreEntity)
  logbook.py         describes homeo_rule_fired events (logbook attribution)
  websocket.py       websocket API for the editor panel (I22)
  panel.py           registers the sidebar panel and serves frontend/homeo-panel.js
  frontend/          the built panel module (from frontend/ at the repo root; committed)
  core/              host-agnostic; must not import homeassistant (enforced by a test)
    errors.py        Diagnostic (file, line, col, message, hint) + caret formatting
    lexer.py         tokens with line/col and per-line indentation
    ast.py           dataclass AST
    printer.py       AST back to canonical text (messages, keys)
    parser.py        Pratt expressions + layout-aware statements and arm blocks
    types.py         static types
    checker.py       name resolution, type checking, target/writer checks, cycles → Program IR
    ir.py            the resolved, typed program: expression nodes, cells, bindings, actions
    values.py        runtime values: Unknown, Keep, OnSpec, Days, time intervals
    timeutil.py      time-of-day, daily time points, ranges, DST handling
    writers.py       per-target service mapping and read-back matching
    runtime.py       cells, propagation, scheduling, bindings, machines, actions
    explain.py       homeo.explain: why a binding, machine or let has its value (I21)
    host.py          Host protocol (what the runtime needs from its environment)
tests/
  core/              fast unit tests with a FakeHost and simulated clock (fakehost.py, rig.py)
  integration/       pytest-homeassistant-custom-component tests
tools/make_icon.py   draws brand/icon.png and icon@2x.png
tools/real_ha.py     smoke test in a full Home Assistant with the demo integration's devices;
                     `serve` mode runs one with its frontend for the panel's browser test
frontend/            the editor panel's source, unit tests (npm test), browser test (e2e/)
hacs.json, LICENSE (MIT), README.md, requirements_test.txt, .github/workflows/tests.yml
```

The core lives inside the integration directory because HACS only ships
`custom_components/homeo/` (research notes §2). It stays importable and testable on its own.

### Pipeline

`text → lexer → parser → AST → checker (with an EntityCatalog from the host) → Program → Runtime`

- **Program** (checker output) is a list of definitions, machines, bindings and actions whose
  expressions are resolved: every name points at an entity read, a `let`, a machine, or a
  builtin, and every node carries its static type.
- **Runtime** turns a Program into a graph of **cells** and runs it against a Host.

### Host interface (core/host.py)

```python
class Host(Protocol):
    def now(self) -> float                          # UTC epoch seconds
    boot_time: float                                # when HA (or the fake) started
    tz: tzinfo                                      # local time zone
    def get_state(self, entity_id) -> EntityState | None   # state, attributes, last_changed, last_updated
    def subscribe_states(self, entity_ids, cb) -> Unsub     # cb(StateChange) incl. `ours: bool`
    def subscribe_bus(self, event_type, cb) -> Unsub        # cb(event_data: dict, time)
    def schedule(self, when: float, cb) -> Unsub
    def call_service(self, call: ServiceCall) -> None       # host creates + remembers the Context
    def publish_machine(self, name, value, changed_at, options) -> None  # the select entity
    def restore_machine(self, name) -> tuple[str, float] | None          # (value, change time)
    def log(self, level, message) -> None
```

The checker needs a separate read-only `EntityCatalog` (exists? domain, current state and
attributes for type inference, event types). Both HassHost and FakeHost provide one.

---

## 2. Runtime model

### 2.1 Values

| Static type | Runtime representation |
|---|---|
| `bool` | `True` / `False` |
| `number` | `Num` (see below); `%` literals carry a `percent` flag |
| `string` | `str` |
| `duration` | `Num` (seconds) |
| `timestamp` | `Num` (UTC epoch seconds) |
| `time-of-day` | `DailyTime` (fixed `hh:mm[:ss]` or solar event + offset), resolved per local date |
| `day` | `Days` (a set of weekdays) |
| `on(...)` | `OnSpec(params)` |
| list | tuple |
| any | `UNKNOWN` sentinel |
| `keep` | `KEEP` sentinel (only legal as a binding/arm result) |

**I1. Time quantities are intervals that move with the clock.** A `Num` is
`[lo, hi]` plus a rate for each bound (how fast it changes per second of wall time). Ordinary
numbers are exact (`lo == hi`) with rate 0. `now` is exact with rate 1; `age(x)` is
`now − t(x)`, also rate 1.

This one representation gives three things the design asks for, with no special cases:
- **Exact wake-ups (D10).** Comparing two moving values tells us exactly when the comparison
  flips (solve `lo/hi` crossings), so the runtime schedules a wake-up instead of polling.
- **Restart lower bounds (D17 rule 2).** A change time from before boot is the interval
  `(−∞, boot]`. Then `age(x)` is `[now − boot, ∞)`, and a comparison is definite only when the
  whole interval is on one side, else `unknown`. `age(x) > 1h` becomes true 1 h after boot;
  `age(x) < 10min` is unknown for 10 minutes, then false. `latest` and `fresh()` compare
  intervals the same way, so two pre-boot times can't be ordered (unknown) while a post-boot
  time beats any pre-boot one.
- **Change times at load (D17 rule 2, as the design states).** "Boot" is when homeo first
  started in this HA run (HA's `started` event on a normal boot; kept across config-entry
  reloads). An entity whose HA `last_changed` is after boot changed for real, so its change
  time is exact. One at or before boot means "some time before the restart": `(−∞, boot]`.
  Attributes, which HA doesn't timestamp, are `(−∞, max(boot, last_updated)]`. Derived values
  (e.g. `bath_mode == "boost"`) start as "some time before now", so `for` on them waits its
  full duration after a restart (D13b, §17 rule 5); `age()` of a restored machine itself is
  exact. `manual()` history is lost (§17), so `manual(x)` starts as never happened, and a
  pre-restart manual change is overridden (the design's accepted cost).
- **Values that appear after load.** D16: change times skip unknown periods. A cell with no
  known value yet (e.g. a device that's `unavailable` at boot) gets "some time before now" as
  the change time of its first known value, so a device reconnecting after boot doesn't look
  like a fresh change (no motion light switched on by `age(motion) < 10min`).

An occurrence time `[−∞, T]` means "never happened, or happened at or before T" (e.g. a bool that
is false at load); `NEVER = [−∞, −∞]` means it definitely never happened (e.g. a button whose
state is `unknown`). `latest` takes its `else` only when every condition is `NEVER`.

### 2.2 Cells and change times

The runtime builds a **cell** for each thing whose history matters:
- every entity read (`entity`, `entity.attr`, `event.x.type`, `manual(e)`, `bus.e(...)`, `every D`,
  time points) — shared across files, keyed by what they read;
- every `let`, state machine, binding and action header;
- every argument of a history construct (`age`, `fresh`, `latest` conditions, `on`, `previous`,
  `toggle`, `count`; `for` after expansion).

Everything else is a pure expression evaluated inside the cell that contains it.

A cell stores: current value, last known value, change time `ct` (an interval), `t_true` (when a
bool last became true), previous value, and its next wake-up time. Per D16, change times skip
`unknown`: if a value goes `A → unknown → A`, `ct` doesn't move.

`t(x)` (design §3) is `t_true` for bool cells. For timestamp cells it's the value itself once
that's at or before now (I23).

### 2.3 Propagation (glitch-free)

**I2. Evaluation runs in waves, in topological order.** Cells get a static rank (topological
order of the dependency graph, built at compile time). An input change (state change, bus event,
wake-up, `select` change, reload) marks source cells dirty; one wave recomputes dirty cells in
rank order, each at most once, and marks dependents dirty only when a value or change time
actually changed. So no binding, machine or action ever sees a half-updated state.

State machines are **sources** in the graph (their arms read the machine's pre-update value,
D13b, so the machine's own value feeds its arms through a one-step delay rather than an edge):

```
loop:
    run a wave over dirty cells
    evaluate each machine whose arm inputs changed (or that just transitioned)
    if none transitioned: break
    apply transitions (ct = now), mark machines dirty; if a machine repeats a value in this
    loop, report a loop error (log and Repairs issue) and stop that machine (D13b) until a
    state is selected from its `select` entity or the rules are reloaded
then: run bindings and actions on the settled values
```

Bindings and actions run once, after all machines settle, so a chain of transitions produces
one write.

### 2.4 Time

- **Wake-ups.** Evaluating a cell collects the earliest future instant at which time alone could
  change its value (crossings of moving `Num`s, the next boundary of a time range, the next
  occurrence of a time point, the next tick of `every`, the next expiry in a `count` window).
  The runtime keeps one host timer for the earliest wake-up across all cells.
- **I3. Time-of-day ranges** `now in A..B` are true when the most recent occurrence of `A`
  is later than the most recent occurrence of `B` (start inclusive, end exclusive). This handles
  wrapping midnight and solar times uniformly. `A == B` is an empty range. `now >= 00:00`
  (`now > 00:00`) is always true rather than the empty range `00:00..00:00`.
- **I4. DST.** Daily times are resolved per local date with `zoneinfo`. A wall-clock time that
  doesn't exist that day (spring forward) resolves to the first instant after the gap; an
  ambiguous one (fall back) resolves to its first occurrence. So a daily time point fires
  exactly once per local day.
- **I5. Solar times** come from the `sun.sun` entity, as D4 says (owner decision; the Sun
  integration is set up by default). `sun.sun` only shows each event's *next* occurrence
  (`next_rising`, `next_setting`, `next_dawn`, `next_dusk`) and moves it forward when the event
  passes, but rules also need the most recent one (`fresh(sunset)`, `on sunset + 30min:` after
  sunset, ranges). So `core/sun.py` remembers each occurrence as `sun.sun` moves past it. Until
  it has seen one (right after homeo starts), the previous occurrence is estimated as the next
  one minus 24 h: off by a few minutes at mid latitudes (D17: predictable beats exact), and
  wrong in polar day/night. Rules depend on a `sun` cell whose value changes only when solar
  times change, not on `sun.sun`'s frequent elevation updates. If `sun.sun` doesn't exist,
  rules using solar times don't compile (with a hint to enable the Sun integration); while
  it's unavailable, solar times are `unknown`.
- **I6. `every D`** is aligned to the clock in local time: ticks at multiples of `D` since local
  midnight (`every 15min` → :00, :15, :30, :45), so it's predictable and survives reloads and
  restarts. `D` must divide 24 h; otherwise ticks count from the Unix epoch, and the checker warns.
- **I7. `count(x, D)`** counts occurrences with exact timestamps (event times or the moment a bool
  became true) in the window `(now − D, now]`, and schedules a wake-up when the oldest falls out.
- **`now`** is a timestamp (moving, rate 1). **`today`** is a `day`. `now` compared with a
  time of day compares local wall-clock time.

### 2.5 Bindings (D5, D15, D16, D17)

Per binding, the runtime keeps `last_known_result` and `last_written`:
- result `unknown` → leave the device alone, keep `last_known_result`;
- result `keep` → leave the device alone, clear `last_known_result` (so the device state stands
  in when `keep` ends);
- known result `R`:
  - if there's a `last_known_result`, write iff `R != last_known_result`;
  - otherwise write iff the device doesn't already match `R` (writer read-back);
  - then `last_known_result = R`.

Reloads carry `last_known_result` and `last_written` over by target id (D17 rule 6). A failed
service call is logged; it isn't retried. But when a bound device comes back from
`unavailable`/`unknown`, its bindings forget `last_known_result`, so the device's state stands
in again (D16) and a write lost while it was offline is made now.

### 2.6 Manual changes (D21)

The host tags each state change with `ours` (its context id is one we issued; HassHost keeps a
bounded set). A change is manual iff it's not ours, neither old nor new state is
`unavailable`/`unknown` or transitional (`opening`, `closing`, ...: a slow cover's progress
reports arrive with a fresh context once HA stops reusing ours, so the change is judged when it
settles), and, for some binding on the entity, the part that binding drives changed away from
what it last wrote (writer read-back with tolerance; if it hasn't written yet, any change to
it counts). Entities without bindings: any change of state counts. A manual change sets
`manual(entity)` to the change time.

### 2.7 Actions (D14)

Each action keeps `last_handled` (starts at load time, carried over on reload by its canonical
header text). When `t(x)` becomes definitely later than `last_handled`: set `last_handled = t(x)`,
then run the body if the `if` guard is definitely true. Service calls are issued in order without
waiting (no `sleep`/`wait` in v1).

### 2.8 State machines (D13)

Values are strings. Restored on restart from the `select` entity: value from the state, change
time from extra restore data (research notes §5). On reload, a machine keeps its value if that
value still exists in the new definition (D13f), and its change time with it.

**I8. A machine's states must be closed.** Arm results must be string literals or
`previous(<self>)`, and the initial value a string literal. That's what makes compile-time
checking of `m == "boots"` (D13c) and the `select` entity's options possible.

---

## 3. Language details (spec gaps)

**I9. Lexical rules.**
- Identifiers: `[A-Za-z_][A-Za-z0-9_]*`. After a `.` (no space), a segment may start with a
  digit, since HA object ids can (`sensor.0x1234_temp`).
- Comments: `#` to end of line. `# mode: dry_run` on its own line before any statement is a file
  directive.
- Strings: `"…"` or `'…'`, single line, escapes `\\ \" \' \n \t`. Unknown escapes are errors.
- **Tabs in indentation are an error** ("indent with spaces"). Indentation width is free but
  must be consistent within a block.
- Durations: a number directly followed by `s`, `min`, `h` or `d`; compounds like `1h30min`;
  decimals allowed (`1.5h`). Percent: `50%`. Times: `7:00`, `07:00`, `07:00:30`.
- Inside `(…)` and `[…]`, line breaks are ignored.

**I10. Layout and continuation.**
- A statement (or arm) continues onto following lines that are **more indented** than the line
  it started on and don't start with `|` (design §8, generalised to all statements).
- A line ending in `:=`, `= latest`/`:= latest`, or `:` (after `state …` or `on …`) opens an
  indented block. In arm blocks, every arm starts at the block's indentation; `|` lines indented
  further are sub-arms of the guard above them.
- `on x: call()` on one line is allowed, as is a body block.

**I11. Name resolution order.** A dotted name `a.b.c` resolves `a` as: a `let`/`state` name in
this file; else a builtin (`now`, `today`, `sunrise`, `sunset`, `dawn`, `dusk`, day names); else
an HA domain (entity id `a.b`, with `.c` an attribute or an event type). So local names shadow
domains: the design's own example uses `let remote = event.bathroom_remote` and then
`remote.press`, while `remote` is also an HA domain. `sun.sun` still works because `sun` the day
is only taken when there's no dot.

**I12. `??` binds slightly tighter than comparisons.** The design puts them in one row; one
level tighter makes `co2 ?? 0 > 800` and `800 < co2 ?? 0` both mean what they look like.
Comparisons don't chain (`a < b < c` is an error with a hint).

**I13. `latest` with `|` groups.** A group competes with the occurrence time of its guard; its
sub-arms are then a first-match cascade that picks the result. If no sub-arm matches, the group
doesn't count and the next most recent arm wins.

**I14. Types for entities** (inferred at load from domain, then attributes):
- bool: `automation`, `binary_sensor`, `calendar`, `fan`, `humidifier`, `input_boolean`,
  `light`, `remote`, `schedule`, `script`, `siren`, `switch`, `update`; and `group`s or
  entities of domains HA core doesn't have (custom integrations) whose state is `on`/`off` at
  load (known multi-state domains like `media_player` or `climate` stay strings even when
  `off`);
- timestamp: `event`, `input_button`, `button`, `scene`, `sensor` with `device_class: timestamp`;
- number: `input_number`, `number`, `counter`, `sensor` with a `unit_of_measurement` or
  `state_class`, or whose current state parses as a number;
- string: everything else (`person`, `select`, `climate`, `cover`, `lock`, …);
- attributes: from the current attribute value's Python type; `any` if absent.
- `any` is accepted wherever a specific type is expected, and converted at run time
  (non-convertible → `unknown`, per D4).

**I15. Units.** Units other than `%` are not interpreted: values compare as plain numbers. `50%`
compares as 50 with numbers, except with a light's `brightness` attribute (0–255), where it's
scaled (as the design says for read-back). Comparing `%` with a sensor whose unit is something
else (e.g. `°C`) is a type error with a hint.

**I16. Arithmetic.** `timestamp − timestamp → duration`; `timestamp ± duration → timestamp`;
`duration ± duration → duration`; `duration * number`, `duration / number → duration`;
`duration / duration → number`; `time-of-day ± duration → time-of-day` (as in `sunset − 30min`).
A value that moves with the clock (e.g. raw `age(x)`) can't be a binding result or a machine
state; it must be compared first (checker error with a hint).

**I17. Cycles.** `let` cycles are errors. A binding that reads its own target, directly or
through `let`s or other bindings, is an error, except through `manual()` (D15). State machines
break cycles (their arms see the previous value), so a binding may feed a machine that feeds the
binding. The runtime's loop detection catches oscillation within one step; oscillation that
goes out through a device and back (binding → device → machine → binding) isn't caught yet
(a known bug, recorded as an `xfail` test in `tests/core/test_edge_cases.py`).

**I18. Writers: read-back tolerance and multi-attribute match.**
- `on(...)` matches when the state is `on` and **every given parameter** matches; parameters not
  given aren't compared.
- Tolerances: light brightness ±3 on the 0–255 scale; colour temperature ±2% (mired rounding);
  fan percentage ± half of `percentage_step` (min 1); climate setpoint ± half of
  `target_temp_step` (default 0.25); cover position ±1; media volume ±0.01; `number` values
  ± half of `step` if present.
- Transitional states (`opening`, `closing`, `locking`, `unlocking`) match the state they're moving
  towards, so a slow cover isn't re-commanded mid-travel.
- `text.x` is accepted like `input_text.x` (the same `set_value` service), for symmetry with
  `number`/`input_number` and `select`/`input_select`.

**I19. Error messages** are `file:line:col: error: message`, then the source line with a caret
under the span, then an optional `hint:` line. Multiple errors per file are reported (the checker
keeps going after an error where it can).

**I20. Dry run.** A `# mode: dry_run` directive (or the integration option "dry run all files")
makes writes and actions log at INFO (`[dry run] would call light.turn_on {...}`) instead of
calling services. Bindings still update `last_known_result` as if they had written, so dry run
logs only changes, but not `last_written` (nothing was written, so a person's change is judged
manual as usual, D21). When dry run is turned off (directive removed, option cleared), each
binding forgets its last result, so the device's state stands in and the device is brought in
line once, as for a new binding (D16).

**I23. Timestamps in the future.** A timestamp signal's `t(x)` is its most recent value at or
before now: a value in the future (a phone's next-alarm sensor, `sensor.next_alarm - 30min`)
happens when its time comes, and the runtime wakes up then. `on sensor.next_alarm:` fires when
the alarm time arrives, not when the alarm is set or moved; `age()` counts from the last value
that passed. Until one has been seen to pass (e.g. right after loading), the last occurrence is
"some time before now", as for change times from before a restart (I1), so `age(x) < 10min` is
unknown for 10 minutes, then false. The value itself is unchanged: `sensor.next_alarm - now`
still reads the future time.

**I21. `homeo.explain`** (§19) is a service that returns a response (Developer tools → Actions
shows it as YAML; automations can use it too). Its `target` is a binding target
(`fan.bathroom`, `climate.x.temperature`), an entity (all its bindings), a state machine (`house`
or `select.homeo_house`) or a `let` name (in every file that defines it). Each explanation has a
one-line `summary`, the current result, for bindings the device's state, whether it matches and
the last write, and per arm: the arm as written (with `let` names), its condition's value
(`happened` for `latest` arms), whether it's the active one, and `values`: every non-constant
subexpression with its current value, `let`s labelled by name and sugar expanded (`x for D`
lists `age(x) >= D`). Values are JSON: bools and numbers as such; durations, times and
intervals as text ("12min", "08:01:00 (12min ago)", "at least 3min" for a change time from
before a restart). Explaining evaluates at the current time and changes no runtime state. The
optional diagnostic sensor per binding (§19) isn't built: the service covers the need without
adding entities.

**I22. Editor panel** (§19). A sidebar panel "Homeo" for admins, registered when the frontend
is loaded. The editor is CodeMirror 6 (what HA's own code editor uses) with a stream-parser
highlighter mirroring the lexer, coloured with HA's code editor theme variables (so themes and
dark mode apply), entity-id completion from the live states, and Ctrl/Cmd+S to save. HACS only
ships `custom_components/homeo/`, so `frontend/` (source, tests, README) is bundled with esbuild
into one committed module, `custom_components/homeo/frontend/homeo-panel.js` (~400 KB, 130 KB
gzipped, nearly all CodeMirror); CI checks it matches the source. It talks to the integration
over admin-only websocket commands (`websocket.py`): `homeo/files`, `homeo/file/read`,
`homeo/files/subscribe`, `homeo/file/check` (parse and check the text as that file, against
the other running files, without loading), `homeo/file/save` (atomic write, then an immediate
reload; a file with errors is saved and its previous version keeps running, as on any save),
`homeo/file/delete` and `homeo/explain`. Explain works on the statement
under the cursor and explains the running version; while its pane is open it follows the
cursor, and it stays open across files. Typing in the editor is kept from HA's global keyboard
shortcuts, which don't recognise an editable area inside a shadow root.

Edits are never lost silently (review of PR ddfisher/ha-homeo#4):
- **Leaving the panel:** unsaved edits are kept in `sessionStorage` and restored when you come
  back (HA rebuilds a custom panel on each visit); closing or reloading the tab asks first.
- **Edits made elsewhere** (File editor or VS Code add-ons, Samba, `git pull`): files carry a
  version (a hash of their text). `homeo/files/subscribe` sends the file list after every
  reload, so an open file that changed on disk reloads itself if it has no unsaved edits, and
  otherwise shows a notice ("Load the file from disk" / "Keep mine"). `homeo/file/save` takes
  the version the edit started from and refuses with `conflict` if the file has changed since,
  unless `force`.
- **File names:** any file the integration loads can be edited (`küche.homeo`,
  `living room.homeo`); only names that would leave the folder or are hidden are refused.

---

## 4. Build order and milestones

| # | Milestone | Done when |
|---|---|---|
| M1 | Lexer + parser | every example in the design parses; precedence table cases; layout/continuation; good syntax errors |
| M2 | Checker | names, types, targets, writers, one-writer rule across files, `else` rules, `fresh`/`on` placement, state checks, cycles; each design-listed compile error has a test |
| M3 | Runtime core | cells, waves, wake-ups, cascades, `latest`, `age`/`for`, history functions, machines, actions, bindings, manual detection, restart and reload semantics, against FakeHost |
| M4 | Writers | service mapping and read-back for the full v1 table |
| M5 | HA integration | config flow, HassHost, file watching + atomic reloads, `select` entities with restore, repairs issues, contexts + logbook, `homeo.reload`, dry run |
| M6 | Tooling | `homeo.explain` (per-subexpression values, active arm, sugar expanded); optional diagnostic sensor |
| M7 | Editor panel | sidebar panel, syntax highlighting, inline errors via websocket `check`, save |
| — | Release | `hacs.json`, brand icon, LICENSE, README, CI (pytest, hassfest, HACS action), first GitHub release |

---

## 5. Testing plan

**Core unit tests (`tests/core`, no HA needed at run time).**
- `FakeHost`: in-memory entity states, a simulated clock (`advance(seconds)` runs due timers in
  order and moves `now` to each timer's time), recorded service calls, a `set_state(...)` that
  mimics HA (keeps `last_changed` when the state string is unchanged; `ours` flag for writes
  echoed back), a fixed time zone, and a `sun.sun` entity that behaves like HA's (next
  occurrences, recomputed as each event passes).
- Lexer: token kinds, positions, durations/percent/time literals, indentation, tabs, strings and
  escapes, errors.
- Parser: AST for every snippet in the design (§5–§21), precedence table (§20) one case per row,
  layout and continuation, error positions and messages.
- Checker: one test per compile error the design names (unknown entity with "did you mean",
  `let` on the left of `:=`, two writers for one target across files, light attribute bound
  separately, cover state + position, missing `else`, `fresh` outside `state`, `on` nested,
  service call on a bound entity, timestamp misuse hint, state typo, read-only target, cycles,
  moving values as results).
- Runtime scenarios, each a small rule file plus a scripted timeline: cascades and arm switching,
  temporary overrides expiring, `latest` hysteresis and ties, `age` wake-ups at the exact
  instant, `for`, three-valued logic, dropouts not moving change times, every §17 rule-2 example
  after a simulated restart, machine chains, `fresh`, timeouts and loop detection, actions
  (`if` vs `and`, once per occurrence, nothing at load), manual detection (ours / late echo /
  availability), reload semantics, time ranges across midnight, DST transitions, solar times,
  `every`, `count`, `toggle`, `previous`.
- Golden test: the full example (§21) loaded and driven through a scripted day.
- A guard test that no module under `core/` imports `homeassistant`.

**Integration tests (`tests/integration`, pytest-homeassistant-custom-component).**
- Setup through a `MockConfigEntry`; rule files written to a temp config dir.
- Bindings call the right services (`async_mock_service`) with our context; echoes of our writes
  aren't manual; a state change with a foreign context is.
- File edits trigger an atomic reload; a broken file creates a repairs issue and keeps old rules;
  fixing it deletes the issue.
- `select.homeo_*` entities: options, UI selection drives the machine, restore (with
  `mock_restore_cache_with_extra_data`) keeps value and change time.
- Startup waits for `started`; time points use `async_fire_time_changed`.
- Logbook attribution via the `homeo_rule_fired` event.

**Tooling.** `pytest` with `asyncio_mode = auto`; `ruff` for lint/format. CI
(`.github/workflows/tests.yml`) runs ruff and the full test suite on Python 3.14 with the latest
HA; `.github/workflows/validate.yml` runs `hassfest` and the HACS action.

---

## 6. Implementation status (handoff)

Last updated in session 2 (branch `claude/tender-einstein-ymrxoo`). Session 1 ended when PR
ddfisher/ha-homeo#1 was merged into `main`.

### Where things stand

| # | Milestone | Status |
|---|---|---|
| M1 | Lexer + parser | **Done.** Every complete example in the design parses. |
| M2 | Checker | **Done.** Every compile error the design names, with hints; cross-file rules via `Globals`. |
| M3 | Runtime core | **Done.** The §21 example runs through a scripted day; restarts, reloads and DST are covered. |
| M4 | Writers | **Done.** The full v1 table with read-back tolerances (`core/writers.py`). |
| M5 | HA integration | **First version done** (see below); a few polish items remain. |
| M6 | Tooling | **Done.** `homeo.explain` (I21) and dry run (I20). The optional diagnostic sensor isn't built (I21). |
| M7 | Editor panel | **Done** (I22). Tested in a real HA frontend with a browser (`frontend/e2e/panel.mjs`). |
| — | Release | **Partly done:** `hacs.json`, MIT license, brand icon, README, tests and validation (`hassfest`, HACS) in CI. Missing: a GitHub release. |

M5 as built:
- **Setup:** single-instance config flow with a dry-run option. Rules are compiled once HA has
  started.
- **Files:** `<config>/homeo/*.homeo` is watched with `watchdog` (inotify), with a 0.5 s debounce
  because editors save in several steps; if inotify can't be set up, it falls back to checking
  every 2 s. Reloads are atomic per file: errors become a Repairs issue and the previous
  version keeps running. Files with errors are retried when an entity appears (late-loading
  integrations).
- **Cross-file reloads:** unchanged files are checked first, so a conflict between files is
  reported in the file just edited. A broken file keeps its previous version only if that
  doesn't clash with the files that compiled, so two rules never drive one device (D5).
- **State machines:** `select.homeo_<name>` entities that restore value and change time, and can
  be set from the UI.
- **Other:** service calls carry homeo's context; the logbook shows the rule behind each change;
  `homeo.reload` service.

### What has been verified, and what hasn't

- 302 Python tests: 279 core (FakeHost, simulated clock), 23 integration
  (`pytest-homeassistant-custom-component`). They pass on HA 2026.10.2 / Python 3.14 (the CI
  setup) and on HA 2026.2.3 / Python 3.13 (the minimum in `hacs.json`). The full suite passed
  100 consecutive runs after the watcher's shutdown races were fixed.
- **Real Home Assistant (session 2):** `tools/real_ha.py` boots a full HA 2026.10.2 (real
  bootstrap and loader, the demo integration's devices, restore state, the real file watcher)
  and checks, in real time: initial sync, every kind of writer against devices that behave like
  real ones (a slow cover isn't re-commanded mid-travel), a manual change and its timed
  override, a boost expiring, a state machine set from its `select` entity, actions on a machine
  and on an event entity, a broken edit raising and clearing a Repairs issue, and, after a
  restart, the machine restored with its change time, `for` waiting its full time, and no action
  re-firing. It found one bug: service calls to domains without entities
  (`persistent_notification.create`) didn't compile.
- **Editor panel:** 5 frontend unit tests (`npm test`), and a browser test
  (`frontend/e2e/panel.mjs`, Playwright) against a real HA 2026.10.2 frontend: file list,
  highlighting, typing without triggering HA's shortcuts, inline errors, entity and domain
  completion, Explain (following the cursor, across files, on `let`s), unsaved edits surviving
  a visit to another page, a file changed on disk (reloaded, or a notice when edited), save,
  and the phone layout, in light and dark themes.
- **Not yet done:** running Homeo with real physical devices.

### Known limitations and open questions

- Solar times (I5): right after homeo starts, the previous sunrise/sunset is estimated as the
  next one minus 24 h until `sun.sun` moves past one. That's a few minutes off at mid latitudes
  and wrong in polar day/night.
- A failed service call isn't retried, except that a device returning from `unavailable`
  gets its state compared with the rule again (§2.5).
- Lists aren't allowed in service data (`entity_id: ["a", "b"]`); only after `in`. Use a
  comma-separated string. An unquoted entity in service data (`entity_id: light.hall`) sends
  the entity's *state*, not its id.
- Reloading the config entry (not a rule file) restarts the runtime: bindings forget their last
  results and actions their last-handled times, as after an HA restart. The boot time is kept.
- `latest` over bool conditions that were false at load is `unknown` until one of them happens
  (I1). In the §21 example, `co2_high` is unknown until co2 crosses 700 or 800, as the design's
  comment says.
- The "`else` required" rule applies to blocks written directly in a binding; a binding that
  uses a `let` holding a `latest` without `else` gets `unknown` (→ keep) until a condition
  happens.
- Restart handling follows D17 as written (§2.1): change times after boot are exact, `manual()`
  starts as never happened (a pre-restart manual change is overridden), and `for` on derived
  values waits its full duration after a restart. Owner decision; don't add restart-specific
  machinery.
- Review suggestions deferred on purpose (PR #1 threads, left open):
  - merge similar cell kinds (`EveryCell`/`TimePointCell`; the stored-timestamp cells): revisit
    when building `homeo.explain`;
  - typed per-kind fields instead of `RCell.extra`: revisit with the cell kinds;
  - registering `homeo.reload` in `async_setup`: needs a top-level `CONFIG_SCHEMA`, which
    conflicts with importing the core without HA.

### Next steps, in order

1. ~~Try it in a real HA instance~~ (done with the demo devices; `tools/real_ha.py`). Trying
   it with real physical devices is still worthwhile.
2. ~~Add `hassfest` and the HACS action to CI; add a test for removing stale `select`
   entities~~ (done; `hassfest` also passes when run locally from HA core's source). The HACS
   action only runs once the repository is public (it can't validate private ones), and
   ignores the description and topics until they're set on GitHub.
3. ~~M6: `homeo.explain`~~ (done, I21).
4. ~~M7: the editor panel~~ (done, I22).
5. First GitHub release. Before that: try Homeo with real devices, and set the repository's
   description and topics (and make it public) so the HACS check runs.

### Working on the code

```sh
uv venv --python 3.14 && uv pip install -r requirements_test.txt
.venv/bin/pytest                                   # all tests (~3 s)
.venv/bin/ruff check custom_components tests tools && .venv/bin/ruff format --check custom_components tests tools
.venv/bin/python tools/make_icon.py               # redraw the brand icon
```

`tools/real_ha.py` is a slower smoke test in a full Home Assistant; its docstring says how to
run it (it needs its own venv).

Reading order for a newcomer: `docs/design.md` (the spec), this document (§1 layout, §2 runtime
model, §3 decisions), then `core/checker.py` and `core/runtime.py`. Runtime behaviour is
easiest to learn from `tests/core/test_runtime.py`, whose scenarios are grouped by design
section.
