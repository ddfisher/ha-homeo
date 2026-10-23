# Homeo: research notes

Research done before implementation, to verify the Home Assistant (HA) facts that
`docs/design.md` relies on and to fill in the API details the design leaves open.

**Method.** Facts below were checked against HA's own source code, installed from PyPI
(`homeassistant==2026.10.2`, the latest release on 2026-10-23), plus the HACS and HA developer
docs (read from their GitHub sources). File paths are relative to the `homeassistant` package.
Where something is a judgment call rather than a fact, it's marked **→ implication** and carried
into `docs/implementation-plan.md`.

---

## 1. Versions and tooling

| | Finding |
|---|---|
| Latest HA | `2026.10.2`, requires **Python ≥ 3.14.2** |
| Test harness | `pytest-homeassistant-custom-component` `0.13.373` tracks the latest HA and requires Python ≥ 3.14. Version `0.13.316` pins HA `2026.2.3`, the last line that runs on Python 3.13. |
| Validation in schemas | HA 2026 schemas use `probatio` (e.g. `light/const.py`), not only `voluptuous`. Irrelevant to us unless we define service schemas; `homeassistant.helpers.config_validation` (`cv`) is still the public way to build them. |
| Astral | `astral` (2.2) is a core HA dependency, used by `helpers/sun.py`. We can use it without declaring a requirement. |

**→ implication:** develop and test against HA 2026.10 on Python 3.14. The host-agnostic core must
not import HA, so it also runs on plain Python 3.13+.

## 2. HACS packaging

From `hacs/documentation` (`docs/publish/integration.md`, `start.md`, `include.md`):

- One integration per repo, in `custom_components/<domain>/`. All runtime files live there.
- `manifest.json` must define at least `domain`, `documentation`, `issue_tracker`, `codeowners`,
  `name`, `version`.
- **Brand assets are now required:** a `brand/` directory *inside the integration directory* with
  at least `icon.png` (HA's developer docs, `creating_integration_file_structure.md`, "Brand images
  - `brand/`"). The HACS default-repo check falls back to `home-assistant/brands` if missing.
- `hacs.json` at the repo root needs at least `name`; optional `homeassistant` (minimum HA version),
  `hide_default_branch`, `zip_release`, etc.
- Inclusion in HACS's default list additionally needs: the HACS GitHub Action and `hassfest`
  passing, a full GitHub release, an OSI license GitHub can identify, a repo description, issues
  enabled and topics. (Not needed for "custom repository" installs.)
- The domain must not collide with a core integration. There is no core `homeo` integration.

Manifest keys we'll use (`loader.py` `Manifest`): `config_flow`, `single_config_entry`,
`integration_type`, `iot_class` (`calculated`), `dependencies`, `after_dependencies`,
`requirements`.

**→ implication:** add `hacs.json`, `custom_components/homeo/manifest.json`, a `brand/icon.png`, a
LICENSE (MIT), and CI with `hassfest` + HACS action before a release.

## 3. State objects, change events and times

`core.py`, class `State` and `StateMachine.async_set_internal`:

- `State` has `state`, `attributes`, `last_changed`, `last_updated`, `last_reported`, `context`.
- `last_changed` is kept from the old state only when the **state string** is unchanged
  (`same_state`); attribute-only changes keep `last_changed` and bump `last_updated`.
- If neither state nor attributes changed, HA fires **`state_reported`** (not `state_changed`)
  and mutates `old_state.last_reported` in place. `async_track_state_change_event` does **not**
  see these.
- If the writer passes no context, HA creates a fresh `Context()` (no `user_id`, no `parent_id`).
- State strings longer than 255 chars become `unknown`.

`helpers/event.py`:

- `async_track_state_change_event(hass, entity_ids, action)` → callback receives
  `Event[EventStateChangedData]` with `entity_id`, `old_state`, `new_state` (either may be `None`:
  entity added/removed). Returns an unsubscribe callable. Routed by entity id (cheap).
- `async_track_point_in_utc_time(hass, action, when)` fires *at or after* `when`. It re-arms if the
  loop timer fires early (`_TrackPointUTCTime.__call__`), so callbacks can rely on
  `dt_util.utcnow() >= when`. `async_track_point_in_time` is the same with local→UTC conversion.
  `async_call_later(hass, delay, action)` is the relative form.
- `async_track_time_interval(hass, action, interval)` for periodic work.

**→ implication:** the HA host adapter subscribes per entity with
`async_track_state_change_event`, schedules wake-ups with `async_track_point_in_utc_time`, and
reads `now` with `dt_util.utcnow()`. Change times for *our* signals are tracked by the runtime,
not taken from `last_changed`, because attribute signals and derived expressions need their own.

## 4. Context and manual-change detection (D21)

`helpers/entity.py`:

- `CONTEXT_RECENT_TIME_SECONDS = 5`. `Entity.async_set_context(ctx)` records `ctx` and
  `time.time()`; when the entity next writes its state, it uses that context only if it was set
  ≤ 5 s ago, otherwise `None` (→ HA makes a fresh context). **Confirmed** as the design says.
- `helpers/service.py` (`entity_service_call` and friends) calls `entity.async_set_context(call.context)`
  on every targeted entity before running the service. So a write caused by our service call
  carries our context if the device reports within 5 s.

`core.py`, class `Context(user_id=None, parent_id=None, id=None)`: `id` defaults to a new ULID;
equality is by `id`.

**→ implication (D21):** create `Context()` per write (optionally `parent_id` = the context of
the change that caused re-evaluation, which helps HA's logbook chain), remember the ids we issued
(bounded LRU), and classify a later change as manual iff: its `context.id` isn't ours, it differs
from our last write (writer read-back with tolerance), and neither side is `unavailable`/`unknown`.

## 5. Restore state (state machines, D13d / D17)

`helpers/restore_state.py`:

- `STATE_DUMP_INTERVAL = 15 min` (plus a dump on clean shutdown); stored states expire after
  7 days (`STATE_EXPIRATION`). **Confirmed.**
- `RestoreEntity.async_get_last_state()` → stored `State` (with its `last_changed`).
- `RestoreEntity.async_get_last_extra_data()` / `extra_restore_state_data` → arbitrary
  JSON-serialisable data via `ExtraStoredData.as_dict()` (`RestoredExtraData` for reading back).

**Pitfall found:** the restored `State.last_changed` is the time the *entity last wrote that
state*. After a restart our `select` entity re-writes its restored state, so HA stamps a new
`last_changed` = boot time. A second restart would then restore the boot time, not the machine's
real change time.

**→ implication:** store the machine's own change time in `extra_restore_state_data`
(`{"changed_at": iso}`) and also expose it as a state attribute for visibility. Fall back to the
restored `last_changed` if the extra data is missing.

## 6. Event and button entities (D7, D9)

- `components/event/__init__.py`: `EventEntity` (a `RestoreEntity`). `state` is the last event's
  time, `isoformat(timespec="milliseconds")`, or `None` (→ `unknown`) if it never fired.
  Attributes `event_type` (last event's type) and `event_types` (all possible types). Each event
  gets a new timestamp, so every event is a `state_changed`, even with the same type. **Confirmed.**
- `components/button/__init__.py`: `ButtonEntity.state` is the ISO timestamp of the last press
  (restored across restarts). `input_button` is built on it. **Confirmed.**
- `scene.*` entities also expose last-activated time as state (BaseScene); natural to treat them
  as timestamp signals too.

**→ implication:** `event.x.<type>` is tracked by the runtime: on each `state_changed` whose new
`event_type` attribute is `<type>`, set that type's occurrence time to the parsed state. `event_types`
lets the checker validate `<type>` at load time ("did you mean").

## 7. Services and attributes for v1 writers (§7)

Verified service names (`components/<domain>/services.yaml`) and attribute names:

| Target | Service(s) | Data | Read back from |
|---|---|---|---|
| `switch`, `input_boolean`, `siren` | `turn_on` / `turn_off` | – | state `on`/`off` |
| `fan` | `turn_on` (`percentage`), `turn_off` | `percentage` 0–100 | state; attr `percentage`; attr `percentage_step` gives speed granularity |
| `light` | `turn_on` (`brightness_pct`, `color_temp_kelvin`), `turn_off` | `brightness_pct` and `brightness` are mutually exclusive (`light/const.py`) | state; attr `brightness` (0–255); attr `color_temp_kelvin`; `min/max_color_temp_kelvin` |
| `climate` setpoint | `set_temperature` | `temperature` | attr `temperature`; `target_temp_step` |
| `climate` mode | `set_hvac_mode` | `hvac_mode` | state |
| `cover` position | `set_cover_position` | `position` 0–100 | attr `current_position` |
| `cover` open/closed | `open_cover` / `close_cover` | – | state (`open`/`closed`/`opening`/`closing`) |
| `input_number`, `number` | `set_value` | `value` | state |
| `input_select`, `select` | `select_option` | `option` | state |
| `input_text` (and `text`) | `set_value` | `value` | state |
| `lock` | `lock` / `unlock` (also `open`) | – | state (`LockState`: `locked`, `unlocked`, `locking`, `unlocking`, `jammed`, `open`, `opening`) |
| `media_player` volume | `volume_set` | `volume_level` **0.0–1.0** | attr `volume_level` |

Conversion facts:

- `light/helper.py`: `brightness_pct` → `brightness = round(255 * pct / 100)`. So 80% → 204,
  50% → 128 (127.5 rounds to 128). **→ tolerance:** compare at 0–255 within ±1 step plus device
  rounding; we'll use ±3 (≈1%).
- `media_player.volume_set` takes a 0–1 float: `30%` → `0.3`.
- Fan speeds snap to `percentage_step`; a 60% request on a 3-speed fan reads back as 66.67. **→
  tolerance:** half of `percentage_step` (default 1).
- Climate setpoints snap to `target_temp_step` (or device precision). **→ tolerance:** half a step,
  default 0.25.
- Transitional states (`opening`, `closing`, `locking`, `unlocking`) should count as "moving
  towards" the target for read-back, not as a mismatch.

## 8. Sun and solar times (§6, §12)

- `sun.sun` attributes: `next_dawn`, `next_dusk`, `next_rising`, `next_setting`, `next_noon`,
  `next_midnight`, `elevation`, `azimuth`, `rising` (`components/sun/const.py`). Only *next*
  occurrences: the most recent sunset isn't available from the entity.
- `helpers/sun.py`: `get_astral_event_date(hass, event, date)` returns the event time for a
  given local date (`event` is an `astral.sun` function name: `sunrise`, `sunset`, `dawn`,
  `dusk`, `noon`), or `None` if it doesn't occur that day (polar day/night).
  `get_astral_event_next(hass, event, utc_point_in_time, offset)` gives the next one. These use
  `hass.config` latitude/longitude/elevation and don't need the `sun` integration.

**→ implication:** read solar times from `sun.sun` as the design says (D4; the owner's call,
since the Sun integration is set up by default), and remember each occurrence as `sun.sun`
moves past it, because only the next one is published. `sun.sun` recomputes all `next_*`
attributes when the earliest event passes (`components/sun/entity.py`, `update_events`), and
its elevation/azimuth attributes change every few minutes, so only changes to `next_*` should
count. If `sun.sun` is unavailable, solar times are `unknown`. (The astral helpers above
compute any day's events, but would bypass the entity.)

## 9. Startup

- `helpers/start.py`: `async_at_started(hass, cb)` runs `cb` once HA is `running` (immediately if
  it already is), otherwise on `EVENT_HOMEASSISTANT_STARTED`. **→** use this for D17 rule 1.
- Entities created by integrations that load late may appear after `started`; the runtime must
  handle `old_state is None` (entity added) as an ordinary change from `unknown`.

## 10. Errors: repairs issues and notifications (§19)

- `helpers/issue_registry.async_create_issue(hass, domain, issue_id, *, is_fixable, severity,
  translation_key, translation_placeholders, ...)` creates or replaces an issue;
  `async_delete_issue(hass, domain, issue_id)` removes it. The text comes from
  `strings.json`/`translations/en.json` under `issues.<translation_key>`, with placeholders.
- `components/persistent_notification.async_create(hass, message, title, notification_id)` and
  `async_dismiss(hass, notification_id)`.

**→ implication:** one issue per file, id `compile_error_<file>`, translation key
`compile_error` with placeholders `{file}` and `{errors}` (the formatted caret snippets, as a
code block). Delete it when the file compiles again. Repairs render markdown, so caret
snippets go in a fenced block.

## 11. Logbook attribution (§19)

`components/logbook/processor.py` (`ContextAugmenter`): a state change's "caused by" line comes
from the **first event recorded with the same context id**. A `call_service` event gives
"triggered by service X.Y"; a custom event type gives whatever that domain's
`logbook.py::async_describe_events` returns (this is how `automation_triggered` shows
"triggered by …").

**→ implication:** before each write or action, fire `homeo_rule_fired` on the bus with our
context and data `{name, source: "bathroom.homeo:35", entity_id}`, and add `logbook.py` describing
it. The logbook then shows "Bathroom fan turned on triggered by homeo rule bathroom.homeo:35".

## 12. Entities we own: `select.homeo_<name>` (D13d)

- `components/select/__init__.py`: `SelectEntity` with `options`, `current_option`,
  `async_select_option(option)`.
- Platforms are set up from a config entry via
  `hass.config_entries.async_forward_entry_setups(entry, [Platform.SELECT])`; the platform's
  `async_setup_entry(hass, entry, async_add_entities)` keeps `async_add_entities` for adding
  machines on reload. Removed machines: `entity.async_remove()` plus
  `entity_registry.async_remove(entity_id)`.
- Give each a stable `unique_id` (`homeo_state_<name>`), so the entity id stays
  `select.homeo_<name>` and the registry tracks it.
- A UI selection arrives as `async_select_option` with the user's context → treat as an ordinary
  change of the machine (D13d).

**→ implication:** the integration uses a config flow with `"single_config_entry": true` (one
"Homeo" entry, added from the UI), which is also what owning entity platforms requires.
`integration_type: "service"`, `iot_class: "calculated"`.

## 13. Entity existence checks (D2)

- `helpers/entity_registry.async_get(hass)` lists registered entities, including ones that are
  currently unavailable or not yet loaded.
- Entities without a `unique_id` (some YAML-defined ones, `sun.sun` historically) are **not** in
  the registry but are in `hass.states`.

**→ implication:** "known entities" = registry entries ∪ `hass.states.async_entity_ids()`. Disabled
registry entries count as known but warn.

## 14. File watching and reloads (§2)

- HA has no file-watching helper for integrations. The core `folder_watcher` integration uses the
  `watchdog==6.0.0` package as a requirement.
- File I/O must go through `hass.async_add_executor_job` (HA warns on blocking I/O in the loop).

**→ implication:** watch `<config>/homeo/` with `watchdog` (inotify on Linux), listed as a
requirement (`watchdog>=6.0.0`, compatible with `folder_watcher`'s pin). Its observer runs a
thread: start and stop it in the executor, and stop it on `EVENT_HOMEASSISTANT_STOP` and on
unload (HA's test harness fails tests that leave threads behind). Debounce events, since
editors save in several steps (write, rename, chmod). Fall back to watchdog's polling observer
if inotify can't be set up (e.g. the watch limit is reached). Plus a `homeo.reload` service.

## 15. Editor panel (§19, last step)

- `components/panel_custom.async_register_panel(hass, frontend_url_path, webcomponent_name,
  sidebar_title, sidebar_icon, module_url, ...)` registers a sidebar panel served from a static
  path (`hass.http.async_register_static_paths`).
- `components/websocket_api.async_register_command` for `homeo/files/list`, `homeo/file/read`,
  `homeo/file/check` (compile without loading, return diagnostics), `homeo/file/save`.

Details deferred to that step.

## 16. Test harness

`pytest_homeassistant_custom_component`:

- fixtures `hass`, `enable_custom_integrations`, `freezer` (pytest-freezer);
- `common.async_fire_time_changed(hass, when)` advances scheduled timers;
- `common.mock_restore_cache` / `mock_restore_cache_with_extra_data` seed restore state;
- `MockConfigEntry` for config-entry setup.

**→ implication:** core logic is unit-tested with our own fake host and simulated clock (fast,
deterministic, no HA). Integration tests use the harness, with `hass.states.async_set` to drive
inputs and `async_mock_service` to capture service calls.

## 17. Summary: design facts checked

| Design claim (Handoff notes) | Result |
|---|---|
| Event entity state = ISO time of last event, `event_type` attribute | ✅ (milliseconds precision) |
| `input_button` / `button` state = time of last press | ✅ |
| `last_changed` resets to boot time on restart | ✅ for entities re-created at boot; restored entities too, because they re-write state |
| Restore state saved every ~15 min and on clean shutdown; `async_get_last_state()` incl. `last_changed` | ✅, but see the §5 pitfall: store our own change time in extra data |
| Context reused for writes within 5 s | ✅ `CONTEXT_RECENT_TIME_SECONDS = 5` |
| APIs: `async_track_state_change_event`, `async_track_point_in_time`, `async_call_later`, `services.async_call(context=)`, issue registry, `util.dt` | ✅ all present with the expected signatures |
| Read files in the executor; wait for `started` | ✅ use `async_add_executor_job` and `helpers.start.async_at_started` |
| HACS: `hacs.json` + `custom_components/homeo/manifest.json` | ✅ plus a now-required `brand/icon.png` |
