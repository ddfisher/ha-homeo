# Homeo

<img src="custom_components/homeo/brand/icon.png" alt="" width="96" align="right">

Declarative rules for Home Assistant. Instead of trigger/condition/action automations, you
describe what each device should be, given the state of the world:

```python
fan.bathroom := sensor.bathroom_co2 > 800 or sensor.bathroom_humidity > 60%
```

Homeo keeps the fan in sync with that expression on every change, without fighting manual
changes. It's a custom integration: it watches `<config>/homeo/*.homeo`, compiles the rules
inside Home Assistant and drives devices through service calls.

**Status: early development.** The language core and the integration work, are tested
against Home Assistant's test harness and have been run in a full Home Assistant with its demo
devices, but not yet with real ones. See
"Implementation status" in [`docs/implementation-plan.md`](docs/implementation-plan.md#6-implementation-status-handoff).

## Docs

- [`docs/design.md`](docs/design.md): the language design (the spec).
- [`docs/research-notes.md`](docs/research-notes.md): verified Home Assistant facts the
  implementation relies on.
- [`docs/implementation-plan.md`](docs/implementation-plan.md): architecture, decisions on gaps
  in the spec, testing plan, progress.

## Installing (development)

1. Copy `custom_components/homeo` into your Home Assistant `config/custom_components/`
   (or add this repo to HACS as a custom repository).
2. Restart Home Assistant and add the **Homeo** integration (Settings → Devices & services).
3. Open **Homeo** in the sidebar (admins only) to create and edit rule files, with highlighting
   and errors shown as you type. Or put files in `config/homeo/` yourself, e.g.
   `config/homeo/bathroom.homeo`. Saving a file reloads it; errors show up in the editor and
   under Settings → Repairs, and the previous version keeps running.

## A taste

```python
let co2      = sensor.bathroom_co2
let humidity = sensor.bathroom_humidity
let night    = now in 22:00..07:00

state bath_mode = "auto":                       # shows up as select.homeo_bath_mode
    fresh(event.bathroom_remote.press)
        | bath_mode == "auto"           -> "boost"
        | else                          -> "auto"
    bath_mode == "boost" for 30min      -> "auto"

fan.bathroom :=
    age(manual(fan.bathroom)) < 1h      -> keep    # yield for an hour after a manual change
    bath_mode == "boost"                -> on
    night                               -> off
    else                                -> co2 > 800 or humidity > 70% for 2min

on humidity > 90% for 30min:
    notify.mobile_app_phone(message: "Bathroom still very humid. Window open?")
```

## Why is it doing that?

In the editor, put the cursor on a rule and press **Explain**. Or call the `homeo.explain`
action (Developer tools → Actions) with a target: a bound entity
(`fan.bathroom`), an attribute target (`climate.living.temperature`), a state machine
(`bath_mode`) or a `let` name. The response says what the rule wants and why, arm by arm:

```yaml
explanations:
  - binding: fan.bathroom
    rule: bathroom.homeo:11
    result: "on"
    summary: fan.bathroom should be on, because `bath_mode == "boost"` (line 13)
    device: "on"
    device_matches: true
    arms:
      - arm: age(manual(fan.bathroom)) < 1h -> keep
        line: 12
        condition: false
        values:
          age(manual(fan.bathroom)): never happened
      - arm: bath_mode == "boost" -> on
        line: 13
        condition: true
        active: true
      ...
```

## Development

```sh
uv venv --python 3.14 && uv pip install -r requirements_test.txt
.venv/bin/pytest            # core unit tests + integration tests
.venv/bin/ruff check custom_components tests tools
```

The language core (`custom_components/homeo/core/`) doesn't import Home Assistant and is
tested against a fake host with a simulated clock (`tests/core/`). Integration tests use
`pytest-homeassistant-custom-component` (`tests/integration/`). `tools/real_ha.py` is a slower
smoke test in a full Home Assistant with demo devices. The editor panel's source is in
[`frontend/`](frontend/README.md).

## License

MIT. See [`LICENSE`](LICENSE).
