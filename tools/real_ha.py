"""Smoke test: run Homeo in a real Home Assistant with the demo integration's devices.

Unlike the integration tests, this boots a full HA (bootstrap, loader, demo devices that behave
like real ones, restore state, the real file watcher) and drives it in real time (about 1.5 min).

    uv venv --python 3.14 /tmp/realha-venv
    VIRTUAL_ENV=/tmp/realha-venv uv pip install homeassistant uv numpy av
    /tmp/realha-venv/bin/python tools/real_ha.py /tmp/realha-config            # first boot
    /tmp/realha-venv/bin/python tools/real_ha.py /tmp/realha-config restart    # restart

`serve` instead runs HA with its frontend on http://127.0.0.1:8123 until stopped, with an admin
user whose access token it writes to `<config>/token.json`, for the editor panel's browser test
(`frontend/e2e/panel.mjs`):

    /tmp/realha-venv/bin/python tools/real_ha.py /tmp/realha-serve serve

HA installs the demo integration's other requirements on the first run (this takes a while).
Use a separate venv: they don't all install into the test venv. The config directory is
created if needed; use a fresh one for a first boot. Exits non-zero if a check fails.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from homeassistant import bootstrap, runner
from homeassistant.core import Context, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

REPO = Path(__file__).resolve().parent.parent

CONFIGURATION = """
homeassistant:
  name: Homeo test
  latitude: 52.37
  longitude: 4.89
  elevation: 0
  unit_system: metric
  time_zone: Europe/Amsterdam
  currency: EUR
  country: NL
logger:
  default: warning
  logs:
    custom_components.homeo: debug
sun:
demo:
input_boolean:
  goodnight:
input_button:
  boost:
  movie:
input_number:
  co2:
    min: 0
    max: 3000
    step: 1
"""

RULES = """
let co2    = input_number.co2
let stuffy = co2 > 800
let night  = input_boolean.goodnight

fan.ceiling_fan :=
    age(input_button.boost) < 20s       -> on(percentage: 100%)
    stuffy                              -> on(percentage: 60%)
    else                                -> off

light.bed_light :=
    age(manual(light.bed_light)) < 15s  -> keep
    night                               -> off
    else                                -> on(brightness: 50%, kelvin: 2700)

cover.living_room_window.position := 30% if stuffy else 80%
climate.heatpump.temperature := 21 if stuffy else 17
climate.hvac := "heat" if stuffy else "cool"
lock.kitchen_door := "locked" if night else "unlocked"
media_player.bedroom.volume := 30% if night else 60%
number.temperature_setting := 20
select.speed := "light_speed"
switch.ac := stuffy

state house = "home":
    fresh(input_button.movie)
        | house == "home"               -> "movie"
        | else                          -> "home"
    house == "movie" for 30s            -> "home"

on house == "movie":
    persistent_notification.create(message: "movie time", notification_id: "homeo_movie")

on event.push_button_press:
    persistent_notification.create(message: "pushed", notification_id: "homeo_push")
"""

T0 = time.monotonic()
failures: list[str] = []


def log(*a: object) -> None:
    print(f"[{time.monotonic() - T0:5.1f}s]", *a, flush=True)


def check(what: str, ok: bool, detail: object = "") -> None:
    log("ok  " if ok else "FAIL", what, detail if not ok else "")
    if not ok:
        failures.append(what)


def state(hass: HomeAssistant, entity_id: str, attr: str | None = None) -> object:
    st = hass.states.get(entity_id)
    if st is None:
        return None
    return st.state if attr is None else st.attributes.get(attr)


def prepare(config: Path, first: bool, configuration: str = CONFIGURATION) -> None:
    config.mkdir(parents=True, exist_ok=True)
    (config / "configuration.yaml").write_text(configuration)
    custom = config / "custom_components"
    custom.mkdir(exist_ok=True)
    link = custom / "homeo"
    if not link.exists():
        link.symlink_to(REPO / "custom_components" / "homeo")
    (config / "homeo").mkdir(exist_ok=True)
    if first:
        (config / "homeo" / "test.homeo").write_text(RULES)


async def main(config: Path, restart: bool) -> None:
    prepare(config, first=not restart)
    hass = await bootstrap.async_setup_hass(
        runner.RuntimeConfig(config_dir=str(config), log_file=str(config / "ha.log"))
    )
    assert hass is not None
    calls: list[str] = []

    ours: set[str] = set()

    @callback
    def on_fired(ev: Event) -> None:
        ours.add(ev.context.id)

    @callback
    def on_call(ev: Event) -> None:
        if ev.context.id in ours:
            calls.append(f"{ev.data['domain']}.{ev.data['service']}")

    hass.bus.async_listen("homeo_rule_fired", on_fired)
    hass.bus.async_listen("call_service", on_call)
    await hass.async_start()
    for _ in range(300):
        if hass.state is CoreState.running:
            break
        await asyncio.sleep(0.2)
    if not restart:
        flow = await hass.config_entries.flow.async_init("homeo", context={"source": "user"})
        await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await asyncio.sleep(3)
    manager = hass.config_entries.async_entries("homeo")[0].runtime_data
    check("rules compile", not manager.errors, manager.errors)
    rules = config / "homeo" / "test.homeo"

    async def call(domain: str, service: str, **data: object) -> None:
        # A fresh context, like a person or a physical switch
        await hass.services.async_call(domain, service, data, blocking=True, context=Context())

    if restart:
        check("machine restored", state(hass, "select.homeo_house") == "movie")
        check("no action re-fired at boot", "persistent_notification.create" not in calls, calls)
        await asyncio.sleep(25)
        check("`for` waits its full time after boot", state(hass, "select.homeo_house") == "movie")
        await asyncio.sleep(7)
        check("`for` ends 30 s after boot", state(hass, "select.homeo_house") == "home")
    else:
        check("initial sync: light", state(hass, "light.bed_light", "brightness") == 128)
        check("initial sync: no write when matching", "switch.turn_off" not in calls, calls)

        await call("input_number", "set_value", entity_id="input_number.co2", value=900)
        await asyncio.sleep(6)  # the demo cover moves slowly
        check("fan speed", state(hass, "fan.ceiling_fan", "percentage") == 60)
        check("cover position", state(hass, "cover.living_room_window", "current_position") == 30)
        check("cover not re-commanded", calls.count("cover.set_cover_position") == 2, calls)
        check("setpoint", state(hass, "climate.heatpump", "temperature") == 21)
        check("hvac mode", state(hass, "climate.hvac") == "heat")
        check("switch", state(hass, "switch.ac") == "on")

        await call("light", "turn_off", entity_id="light.bed_light")
        await asyncio.sleep(10)
        check("manual change respected", state(hass, "light.bed_light") == "off")
        await asyncio.sleep(7)
        check("re-synced after the override", state(hass, "light.bed_light") == "on")

        response = await hass.services.async_call(
            "homeo", "explain", {"target": "fan.ceiling_fan"}, blocking=True, return_response=True
        )
        summary = response["explanations"][0]["summary"]
        check("explain", "because `stuffy` (line 8)" in summary, summary)

        await call("input_button", "press", entity_id="input_button.boost")
        await asyncio.sleep(1)
        check("boost", state(hass, "fan.ceiling_fan", "percentage") == 100)
        await asyncio.sleep(20)
        check("boost expires", state(hass, "fan.ceiling_fan", "percentage") == 60)

        await call("input_boolean", "turn_on", entity_id="input_boolean.goodnight")
        await asyncio.sleep(4)
        check("lock", state(hass, "lock.kitchen_door") == "locked")
        check("volume", state(hass, "media_player.bedroom", "volume_level") == 0.3)
        check("light off at night", state(hass, "light.bed_light") == "off")

        await call("input_button", "press", entity_id="input_button.movie")
        await asyncio.sleep(1)
        check("machine transition", state(hass, "select.homeo_house") == "movie")
        check("entry action", "persistent_notification.create" in calls, calls)
        await call("select", "select_option", entity_id="select.homeo_house", option="home")
        await asyncio.sleep(1)
        check("select from the UI", state(hass, "select.homeo_house") == "home")

        n = calls.count("persistent_notification.create")
        await call("button", "press", entity_id="button.push")
        await asyncio.sleep(1)
        check("event entity action", calls.count("persistent_notification.create") == n + 1)

        broken = RULES.replace("input_boolean.goodnight", "input_boolean.goodnite")
        await hass.async_add_executor_job(rules.write_text, broken)
        await asyncio.sleep(2)
        issues = [i for (d, i) in ir.async_get(hass).issues if d == "homeo"]
        check("broken file: repairs issue", issues == ["compile_error_test_homeo"], issues)
        await hass.async_add_executor_job(rules.write_text, RULES)
        await asyncio.sleep(2)
        issues = [i for (d, i) in ir.async_get(hass).issues if d == "homeo"]
        check("fixed file: issue cleared", issues == [], issues)

        await call("input_button", "press", entity_id="input_button.movie")
        await asyncio.sleep(1)
        check("machine in `movie` at shutdown", state(hass, "select.homeo_house") == "movie")
    await hass.async_stop()
    log("done:", f"{len(failures)} failed check(s)" if failures else "all checks passed")


SERVE_RULES = {
    "bathroom.homeo": """# Bathroom
let co2    = input_number.co2
let stuffy = co2 > 800
let night  = now in 22:00..07:00

state bath_mode = "auto":
    fresh(input_button.boost)
        | bath_mode == "auto"       -> "boost"
        | else                      -> "auto"
    bath_mode == "boost" for 30min  -> "auto"

fan.ceiling_fan :=
    age(manual(fan.ceiling_fan)) < 1h   -> keep
    bath_mode == "boost"                -> on(percentage: 100%)
    stuffy                              -> on(percentage: 60%)
    else                                -> off

light.bed_light :=
    input_boolean.goodnight     -> off
    night                       -> on(brightness: 10%, kelvin: 2200)
    else                        -> on(brightness: 80%, kelvin: 2700)

on input_button.movie:
    persistent_notification.create(message: "Movie time", notification_id: "movie")
""",
    "garage.homeo": "switch.ac := sensor.outside_temperature > 25\n",
}


async def serve(config: Path) -> None:
    """HA with its frontend, for the editor panel's browser test."""
    first = not (config / ".storage" / "core.config_entries").exists()
    prepare(config, False, CONFIGURATION + "frontend:\nhttp:\n  server_port: 8123\n")
    if first:
        for name, text in SERVE_RULES.items():
            (config / "homeo" / name).write_text(text)
        (config / ".storage").mkdir(exist_ok=True)
        done = ["user", "core_config", "analytics", "integration"]
        (config / ".storage" / "onboarding").write_text(
            json.dumps(
                {"version": 4, "minor_version": 1, "key": "onboarding", "data": {"done": done}}
            )
        )
    hass = await bootstrap.async_setup_hass(
        runner.RuntimeConfig(config_dir=str(config), log_file=str(config / "ha.log"))
    )
    assert hass is not None
    await hass.async_start()
    for _ in range(300):
        if hass.state is CoreState.running:
            break
        await asyncio.sleep(0.2)
    if not hass.config_entries.async_entries("homeo"):
        flow = await hass.config_entries.flow.async_init("homeo", context={"source": "user"})
        await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    users = [u for u in await hass.auth.async_get_users() if u.name == "Owner"]
    user = (
        users[0]
        if users
        else await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    )
    client_id = "http://127.0.0.1:8123/"
    refresh = await hass.auth.async_create_refresh_token(user, client_id=client_id)
    token = {
        "access_token": hass.auth.async_create_access_token(refresh),
        "refresh_token": refresh.token,
        "token_type": "Bearer",
        "expires_in": 1800,
        "hassUrl": "http://127.0.0.1:8123",
        "clientId": client_id,
    }
    (config / "token.json").write_text(json.dumps(token))
    log("ready on http://127.0.0.1:8123 (token in token.json)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    if mode == "serve":
        asyncio.run(serve(Path(sys.argv[1]).resolve()))
    asyncio.run(main(Path(sys.argv[1]).resolve(), mode == "restart"))
    sys.exit(1 if failures else 0)
