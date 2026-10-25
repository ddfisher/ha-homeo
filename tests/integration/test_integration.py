"""Integration tests against a real (test) Home Assistant."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from custom_components.homeo.const import DOMAIN, EVENT_RULE_FIRED

FAN = """
fan.bathroom :=
    age(manual(fan.bathroom)) < 1h      -> keep
    else                                -> sensor.bathroom_co2 > 800
"""


async def test_binding_calls_service_with_our_context(hass: HomeAssistant, setup_homeo):
    hass.states.async_set("sensor.bathroom_co2", "500", {"unit_of_measurement": "ppm"})
    hass.states.async_set("fan.bathroom", "off")
    turn_on = async_mock_service(hass, "fan", "turn_on")
    fired = []
    hass.bus.async_listen(EVENT_RULE_FIRED, fired.append)
    await setup_homeo({"bathroom.homeo": "fan.bathroom := sensor.bathroom_co2 > 800\n"})

    hass.states.async_set("sensor.bathroom_co2", "900", {"unit_of_measurement": "ppm"})
    await hass.async_block_till_done()
    assert len(turn_on) == 1
    call = turn_on[0]
    assert call.data["entity_id"] == "fan.bathroom"
    assert fired and fired[0].context.id == call.context.id
    assert fired[0].data["source"] == "bathroom.homeo:1"


async def test_manual_detection_uses_context(hass: HomeAssistant, setup_homeo, freezer):
    hass.states.async_set("sensor.bathroom_co2", "500", {"unit_of_measurement": "ppm"})
    hass.states.async_set("fan.bathroom", "off")
    turn_on = async_mock_service(hass, "fan", "turn_on")
    turn_off = async_mock_service(hass, "fan", "turn_off")
    entry = await setup_homeo({"bathroom.homeo": FAN})
    manager = entry.runtime_data

    # past the post-load uncertainty of manual()
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.bathroom_co2", "900", {"unit_of_measurement": "ppm"})
    await hass.async_block_till_done()
    assert len(turn_on) == 1
    # the device reports back with our context: not manual
    hass.states.async_set("fan.bathroom", "on", context=turn_on[0].context)
    await hass.async_block_till_done()
    manual = manager.runtime.cells["manual:fan.bathroom"].extra["t"]
    assert manual.lo == float("-inf")

    # a person turns it off (fresh context): manual, and the rule yields for an hour
    hass.states.async_set("fan.bathroom", "off", context=Context(user_id="abc"))
    await hass.async_block_till_done()
    assert manager.runtime.cells["manual:fan.bathroom"].extra["t"].lo > 0
    assert len(turn_off) == 0
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(turn_on) == 2  # re-synced after the hour


async def test_compile_errors_create_issue_and_keep_old_rules(
    hass: HomeAssistant, setup_homeo, rules_dir
):
    hass.states.async_set("switch.pump", "off")
    hass.states.async_set("binary_sensor.window", "off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    entry = await setup_homeo({"pump.homeo": "switch.pump := binary_sensor.window\n"})
    manager = entry.runtime_data

    (rules_dir / "pump.homeo").write_text("switch.pump := binary_sensor.windw\n")
    await manager.async_reload()
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, "compile_error_pump_homeo")
    assert issue is not None
    assert "unknown entity `binary_sensor.windw`" in issue.translation_placeholders["errors"]

    # old rules still run
    hass.states.async_set("binary_sensor.window", "on")
    await hass.async_block_till_done()
    assert len(turn_on) == 1

    (rules_dir / "pump.homeo").write_text("switch.pump := binary_sensor.window\n")
    await manager.async_reload()
    assert issues.async_get_issue(DOMAIN, "compile_error_pump_homeo") is None


async def test_stopped_machine_creates_issue_until_reload(
    hass: HomeAssistant, setup_homeo, freezer
):
    hass.states.async_set("input_button.boost", "unknown")
    hass.states.async_set("binary_sensor.window", "on")
    rules = """
state m = "a":
    m == "a" and fresh(input_button.boost) -> "b"
    m == "b" and binary_sensor.window -> "a"
"""
    entry = await setup_homeo({"m.homeo": rules})
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("input_button.boost", dt_util.utcnow().isoformat())
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, "machine_stopped_m")
    assert issue is not None
    assert issue.translation_placeholders == {"name": "m", "file": "m.homeo", "loop": "a → b → a"}

    await entry.runtime_data.async_reload()
    assert issues.async_get_issue(DOMAIN, "machine_stopped_m") is None


async def wait_for(hass: HomeAssistant, predicate, timeout: float = 5.0) -> None:
    """Wait for something done by the watcher thread (real time, not the frozen clock)."""
    loop_deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > loop_deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()


async def test_file_watcher_reloads_on_save(hass: HomeAssistant, setup_homeo, rules_dir):
    hass.states.async_set("switch.pump", "off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    entry = await setup_homeo({})
    (rules_dir / "new.homeo").write_text("switch.pump := on\n")
    await wait_for(hass, lambda: len(turn_on) == 1)
    (rules_dir / "new.homeo").write_text("switch.pump := off\n")
    await wait_for(hass, lambda: len(turn_off) == 1)
    (rules_dir / "new.homeo").unlink()
    await wait_for(hass, lambda: not entry.runtime_data.programs)
    (rules_dir / "notes.txt").write_text("not a rules file\n")  # ignored


MACHINE = """
state bath_mode = "auto":
    fresh(input_button.boost)
        | bath_mode == "auto" -> "boost"
        | else -> "auto"
    bath_mode == "boost" for 30min -> "auto"
"""


async def test_state_machine_select_entity(hass: HomeAssistant, setup_homeo, freezer):
    hass.states.async_set("input_button.boost", "unknown")
    entry = await setup_homeo({"bath.homeo": MACHINE})
    state = hass.states.get("select.homeo_bath_mode")
    assert state is not None
    assert state.state == "auto"
    assert state.attributes["options"] == ["auto", "boost"]

    freezer.tick(timedelta(seconds=1))  # the clock is frozen; press after entering "auto"
    hass.states.async_set("input_button.boost", dt_util.utcnow().isoformat())
    await hass.async_block_till_done()
    assert hass.states.get("select.homeo_bath_mode").state == "boost"

    # choosing an option in the UI drives the machine
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.homeo_bath_mode", "option": "auto"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime.machine_value("bath_mode") == "auto"
    assert hass.states.get("select.homeo_bath_mode").state == "auto"


async def test_removed_machine_removes_its_select_entity(
    hass: HomeAssistant, setup_homeo, rules_dir
):
    hass.states.async_set("input_button.boost", "unknown")
    entry = await setup_homeo({"bath.homeo": MACHINE})
    registry = er.async_get(hass)
    assert registry.async_get("select.homeo_bath_mode") is not None

    renamed = MACHINE.replace("bath_mode", "bath")
    (rules_dir / "bath.homeo").write_text(renamed)
    await entry.runtime_data.async_reload()
    await hass.async_block_till_done()
    assert hass.states.get("select.homeo_bath_mode") is None
    assert registry.async_get("select.homeo_bath_mode") is None
    assert hass.states.get("select.homeo_bath").state == "auto"

    # and it comes back with the same entity id when the machine does
    (rules_dir / "bath.homeo").write_text(MACHINE)
    await entry.runtime_data.async_reload()
    await hass.async_block_till_done()
    assert hass.states.get("select.homeo_bath_mode").state == "auto"
    assert hass.states.get("select.homeo_bath") is None


async def test_state_machine_restored_with_change_time(hass: HomeAssistant, setup_homeo, freezer):
    hass.states.async_set("input_button.boost", "unknown")
    entered = dt_util.utcnow() - timedelta(minutes=20)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("select.homeo_bath_mode", "boost"),
                {"changed_at": entered.isoformat()},
            )
        ],
    )
    rules = MACHINE.replace(
        'bath_mode == "boost" for 30min', 'bath_mode == "boost" and age(bath_mode) >= 30min'
    )
    await setup_homeo({"bath.homeo": rules})
    assert hass.states.get("select.homeo_bath_mode").state == "boost"
    freezer.tick(timedelta(minutes=10, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("select.homeo_bath_mode").state == "auto"  # 30 min after entering


async def test_dry_run_option(hass: HomeAssistant, setup_homeo, caplog):
    hass.states.async_set("switch.pump", "off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    await setup_homeo({"pump.homeo": "switch.pump := on\n"}, options={"dry_run": True})
    assert turn_on == []
    assert "[dry run]" in caplog.text


async def test_reload_service_and_unload(hass: HomeAssistant, setup_homeo, rules_dir):
    hass.states.async_set("switch.pump", "off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    entry = await setup_homeo({})
    (rules_dir / "pump.homeo").write_text("switch.pump := on\n")
    await hass.services.async_call(DOMAIN, "reload", blocking=True)
    await hass.async_block_till_done()
    assert len(turn_on) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "reload")


async def test_time_points_fire(hass: HomeAssistant, setup_homeo, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-01-05 06:59:00+00:00")
    calls = async_mock_service(hass, "notify", "phone")
    await setup_homeo({"t.homeo": 'on 07:00:\n    notify.phone(message: "morning")\n'})
    assert calls == []
    freezer.move_to("2026-01-05 07:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls) == 1 and calls[0].data == {"message": "morning"}


async def test_behavior_follows_with_transitions(hass: HomeAssistant, setup_homeo, freezer):
    """D23: a ramp is sent as pieces with `transition:`, at most 5 minutes each."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-01-05 18:59:00+00:00")
    hass.states.async_set("light.hall", "on", {"brightness": 255, "supported_features": 32})
    calls = async_mock_service(hass, "light", "turn_on")
    rule = (
        "light.hall :=\n"
        "    now in 19:00..23:00 -> on(brightness: 100% - 96% * (age(19:00) / 4h))\n"
        "    else -> keep\n"
    )
    await setup_homeo({"t.homeo": rule})
    assert calls == []
    for minute, pct in ((0, 98), (5, 96), (10, 94)):
        freezer.move_to(f"2026-01-05 19:{minute:02d}:00+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert calls[-1].data == {
            "entity_id": "light.hall",
            "brightness_pct": pct,
            "transition": 300,
        }
    assert len(calls) == 3


async def test_calls_to_one_entity_run_in_order(hass: HomeAssistant, setup_homeo, freezer):
    """A jump to a behavior's current value finishes before its transition starts (D23)."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-01-05 18:59:00+00:00")
    hass.states.async_set("light.hall", "off", {"supported_features": 32})
    log = []
    gate = asyncio.Event()

    async def turn_on(call):
        log.append(("start", dict(call.data)))
        if "transition" not in call.data:
            await gate.wait()  # a slow device
        log.append(("end", dict(call.data)))

    hass.services.async_register("light", "turn_on", turn_on)
    rule = (
        "light.hall :=\n"
        "    now in 19:00..23:00 -> on(brightness: 100% - 96% * (age(19:00) / 4h))\n"
        "    else -> keep\n"
    )
    await setup_homeo({"t.homeo": rule})
    freezer.move_to("2026-01-05 19:00:00+00:00")
    async_fire_time_changed(hass)
    for _ in range(20):
        await asyncio.sleep(0)
    assert [(k, d.get("brightness_pct")) for k, d in log] == [("start", 100)]
    gate.set()
    await hass.async_block_till_done()
    assert [(k, d.get("brightness_pct")) for k, d in log] == [
        ("start", 100),
        ("end", 100),
        ("start", 98),
        ("end", 98),
    ]


async def test_a_slow_call_doesnt_hold_up_later_ones(hass: HomeAssistant, setup_homeo):
    """Only a jump and its transition wait for each other; other calls stay independent."""
    hass.states.async_set("input_boolean.x", "off")
    hass.states.async_set("switch.pump", "off")
    log = []
    gate = asyncio.Event()

    async def turn_on(call):
        log.append("on start")
        await gate.wait()  # a device that doesn't answer
        log.append("on end")

    async def turn_off(call):
        log.append("off")

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("switch", "turn_off", turn_off)
    await setup_homeo({"t.homeo": "switch.pump := input_boolean.x\n"})
    hass.states.async_set("input_boolean.x", "on")
    for _ in range(20):
        await asyncio.sleep(0)
    hass.states.async_set("input_boolean.x", "off")
    for _ in range(20):
        await asyncio.sleep(0)
    assert log == ["on start", "off"]
    gate.set()
    await hass.async_block_till_done()


async def test_superseded_transition_is_not_sent(hass: HomeAssistant, setup_homeo, freezer):
    """A transition waiting behind a slow jump is dropped once a newer call has gone out, so it
    can't undo that call (D23)."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-01-05 18:59:00+00:00")
    hass.states.async_set("light.hall", "off", {"supported_features": 32})
    hass.states.async_set("input_boolean.x", "off")
    log = []
    gate = asyncio.Event()

    async def turn_on(call):
        log.append(("on", call.data.get("brightness_pct"), call.data.get("transition")))
        if "transition" not in call.data:
            await gate.wait()  # the device stalls on the jump

    async def turn_off(call):
        log.append(("off", None, None))

    hass.services.async_register("light", "turn_on", turn_on)
    hass.services.async_register("light", "turn_off", turn_off)
    rule = (
        "light.hall :=\n"
        "    input_boolean.x -> off\n"
        "    now in 19:00..23:00 -> on(brightness: 100% - 96% * (age(19:00) / 4h))\n"
        "    else -> keep\n"
    )
    await setup_homeo({"t.homeo": rule})
    freezer.move_to("2026-01-05 19:00:00+00:00")
    async_fire_time_changed(hass)
    for _ in range(20):
        await asyncio.sleep(0)
    hass.states.async_set("input_boolean.x", "on")
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await hass.async_block_till_done()
    assert log == [("on", 100, None), ("off", None, None)]


async def test_config_flow_single_instance(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort"


async def test_files_with_errors_retry_when_entities_appear(
    hass: HomeAssistant, setup_homeo, freezer
):
    hass.states.async_set("switch.pump", "off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    entry = await setup_homeo({"late.homeo": "switch.pump := binary_sensor.late_one\n"})
    assert "late.homeo" in entry.runtime_data.errors
    hass.states.async_set("binary_sensor.late_one", "on")  # its integration loads later
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "late.homeo" not in entry.runtime_data.errors
    assert len(turn_on) == 1


async def test_boot_time_survives_entry_reload(hass: HomeAssistant, setup_homeo):
    entry = await setup_homeo({})
    boot = entry.runtime_data.host.boot_time
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.host.boot_time == boot


async def test_cross_file_conflict_blamed_on_edited_file(
    hass: HomeAssistant, setup_homeo, rules_dir
):
    hass.states.async_set("fan.bathroom", "off")
    hass.states.async_set("binary_sensor.window", "off")
    entry = await setup_homeo(
        {"a.homeo": "let unused = on\n", "b.homeo": "fan.bathroom := binary_sensor.window\n"}
    )
    manager = entry.runtime_data
    (rules_dir / "a.homeo").write_text("fan.bathroom := on\n")  # now conflicts with b.homeo
    await manager.async_reload()
    assert set(manager.errors) == {"a.homeo"}
    bound = [b.key for p in manager.programs.values() for b in p.bindings]
    assert bound == ["fan.bathroom"]  # only b.homeo drives the fan
    assert not manager.programs["a.homeo"].bindings  # a.homeo's previous version still runs


async def test_old_version_that_now_conflicts_is_stopped(
    hass: HomeAssistant, setup_homeo, rules_dir
):
    hass.states.async_set("fan.bathroom", "off")
    entry = await setup_homeo({"a.homeo": "fan.bathroom := on\n", "b.homeo": "let x = on\n"})
    manager = entry.runtime_data
    (rules_dir / "a.homeo").write_text("fan.bathroom := on\nlet broken = nope\n")
    await manager.async_reload()
    assert "a.homeo" in manager.errors and manager.programs["a.homeo"].bindings
    (rules_dir / "b.homeo").write_text("fan.bathroom := off\n")  # takes over the fan
    await manager.async_reload()
    bound = [(n, b.key) for n, p in manager.programs.items() for b in p.bindings]
    assert bound == [("b.homeo", "fan.bathroom")]


@pytest.mark.parametrize("expected_lingering_timers", [True])  # HA's sun integration polls
async def test_solar_times_match_the_sun_entity(hass: HomeAssistant, setup_homeo):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "sun", {})
    await hass.async_block_till_done()
    hass.states.async_set("input_boolean.dark", "off")
    async_mock_service(hass, "input_boolean", "turn_on")
    async_mock_service(hass, "input_boolean", "turn_off")
    entry = await setup_homeo({"sun.homeo": "input_boolean.dark := now in sunset..sunrise\n"})
    dark = entry.runtime_data.runtime.binding("input_boolean.dark").last_known
    assert dark == (hass.states.get("sun.sun").state == "below_horizon")


async def test_explain_service(hass: HomeAssistant, setup_homeo):
    hass.states.async_set("sensor.bathroom_co2", "900", {"unit_of_measurement": "ppm"})
    hass.states.async_set("fan.bathroom", "off")
    async_mock_service(hass, "fan", "turn_on")
    await setup_homeo({"bathroom.homeo": FAN})

    response = await hass.services.async_call(
        DOMAIN, "explain", {"target": "fan.bathroom"}, blocking=True, return_response=True
    )
    (e,) = response["explanations"]
    assert e["binding"] == "fan.bathroom"
    assert e["result"] == "on"
    assert e["rule"] == "bathroom.homeo:2"
    assert [a.get("active", False) for a in e["arms"]] == [False, True]
    assert e["arms"][1]["values"] == {"sensor.bathroom_co2": 900}

    with pytest.raises(ServiceValidationError, match="did you mean `fan.bathroom`"):
        await hass.services.async_call(
            DOMAIN, "explain", {"target": "fan.bathrom"}, blocking=True, return_response=True
        )


async def test_editor_websocket_api(hass: HomeAssistant, setup_homeo, rules_dir, hass_ws_client):
    hass.states.async_set("switch.pump", "off")
    hass.states.async_set("binary_sensor.window", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    entry = await setup_homeo(
        {"pump.homeo": "switch.pump := binary_sensor.window\n", "broken.homeo": "let x =\n"}
    )
    client = await hass_ws_client(hass)

    async def ws(msg: dict) -> dict:
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    r = await ws({"type": "homeo/files"})
    assert r["success"]
    assert [{k: f[k] for k in ("name", "loaded", "errors")} for f in r["result"]["files"]] == [
        {"name": "broken.homeo", "loaded": False, "errors": 1},
        {"name": "pump.homeo", "loaded": True, "errors": 0},
    ]

    r = await ws({"type": "homeo/file/read", "name": "pump.homeo"})
    assert r["result"]["text"] == "switch.pump := binary_sensor.window\n"
    r = await ws({"type": "homeo/file/read", "name": "../configuration.yaml"})
    assert not r["success"] and r["error"]["code"] == "invalid_format"

    # check: compiles without loading, against the other files
    r = await ws({"type": "homeo/file/check", "name": "new.homeo", "text": "switch.pump := on\n"})
    (d,) = r["result"]["diagnostics"]
    assert d["severity"] == "error"
    assert d["message"] == "`switch.pump` is already bound in pump.homeo"
    assert (d["line"], d["col"]) == (1, 1)
    r = await ws({"type": "homeo/file/check", "name": "pump.homeo", "text": "switch.pump := on\n"})
    assert r["result"]["diagnostics"] == []
    assert "new.homeo" not in {
        f["name"] for f in (await ws({"type": "homeo/files"}))["result"]["files"]
    }

    # save: writes and reloads right away
    r = await ws({"type": "homeo/file/save", "name": "broken.homeo", "text": "let x = on\n"})
    assert r["result"]["loaded"] is True and r["result"]["diagnostics"] == []
    assert (rules_dir / "broken.homeo").read_text() == "let x = on\n"
    assert "broken.homeo" in entry.runtime_data.programs
    r = await ws({"type": "homeo/file/save", "name": "pump.homeo", "text": "switch.pump := nope\n"})
    assert r["result"]["loaded"] is False
    assert "nope" in r["result"]["diagnostics"][0]["message"]

    r = await ws({"type": "homeo/explain", "target": "switch.pump"})
    assert r["result"]["explanations"][0]["binding"] == "switch.pump"  # the old version runs

    r = await ws({"type": "homeo/file/delete", "name": "broken.homeo"})
    assert r["success"]
    assert not (rules_dir / "broken.homeo").exists()
    assert "broken.homeo" not in entry.runtime_data.programs


async def test_editor_websocket_api_needs_admin(
    hass: HomeAssistant, setup_homeo, hass_ws_client, hass_admin_user
):
    await setup_homeo({})
    hass_admin_user.groups = []  # no longer an admin
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "homeo/files"})
    r = await client.receive_json()
    assert not r["success"] and r["error"]["code"] == "unauthorized"


async def test_panel_registered_with_the_frontend(hass: HomeAssistant, setup_homeo):
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")  # the test environment has no frontend package
    with (
        patch.object(hass.http, "async_register_static_paths", AsyncMock()) as static,
        patch("homeassistant.components.panel_custom.async_register_panel", AsyncMock()) as reg,
        patch("homeassistant.components.frontend.async_remove_panel") as remove,
    ):
        entry = await setup_homeo({})
        (paths,) = static.call_args.args
        assert paths[0].url_path == "/homeo_static"
        assert paths[0].path.endswith("custom_components/homeo/frontend")
        kwargs = reg.call_args.kwargs
        assert kwargs["frontend_url_path"] == "homeo"
        assert kwargs["webcomponent_name"] == "homeo-panel"
        assert kwargs["require_admin"] is True
        assert kwargs["module_url"].startswith("/homeo_static/homeo-panel.js?v=")

        await hass.config_entries.async_unload(entry.entry_id)
        remove.assert_called_once_with(hass, "homeo", warn_if_unknown=False)
        # static paths can't be unregistered: setting up again doesn't add them twice
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert static.call_count == 1
        assert reg.call_count == 2


async def test_editor_file_names(hass: HomeAssistant, setup_homeo, rules_dir, hass_ws_client):
    """Any file the integration loads can be edited; nothing outside the folder can."""
    await setup_homeo({"küche.homeo": "let a = on\n", "living room.homeo": "let b = on\n"})
    client = await hass_ws_client(hass)

    async def ws(msg: dict) -> dict:
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    for name in ("küche.homeo", "living room.homeo"):
        r = await ws({"type": "homeo/file/read", "name": name})
        assert r["success"], r
        r = await ws({"type": "homeo/file/save", "name": name, "text": "let c = off\n"})
        assert r["success"] and r["result"]["loaded"], r
    for bad in ("../x.homeo", "a/b.homeo", "a\\b.homeo", ".hidden.homeo", ".homeo", "x.txt"):
        r = await ws({"type": "homeo/file/read", "name": bad})
        assert not r["success"] and r["error"]["code"] == "invalid_format", bad


async def test_editor_save_conflicts(hass: HomeAssistant, setup_homeo, rules_dir, hass_ws_client):
    """A save based on an old version of the file is refused, unless forced."""
    await setup_homeo({"a.homeo": "let a = on\n"})
    client = await hass_ws_client(hass)

    async def ws(msg: dict) -> dict:
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    version = (await ws({"type": "homeo/file/read", "name": "a.homeo"}))["result"]["version"]
    (rules_dir / "a.homeo").write_text("let a = off  # edited elsewhere\n")
    save = {"type": "homeo/file/save", "name": "a.homeo", "text": "let a = 1\n"}
    r = await ws({**save, "version": version})
    assert not r["success"] and r["error"]["code"] == "conflict"
    assert "changed outside the editor" in r["error"]["message"]
    assert "edited elsewhere" in (rules_dir / "a.homeo").read_text()

    r = await ws({**save, "version": version, "force": True})
    assert r["success"]
    assert (rules_dir / "a.homeo").read_text() == "let a = 1\n"
    r = await ws({**save, "text": "let a = 2\n", "version": r["result"]["version"]})
    assert r["success"]  # based on the version just saved

    # a "new" file that appeared on disk meanwhile
    (rules_dir / "b.homeo").write_text("let b = on\n")
    r = await ws({"type": "homeo/file/save", "name": "b.homeo", "text": "x", "version": None})
    assert r["error"]["code"] == "conflict" and "created outside" in r["error"]["message"]


async def test_editor_files_subscription(
    hass: HomeAssistant, setup_homeo, rules_dir, hass_ws_client
):
    entry = await setup_homeo({"a.homeo": "let a = on\n"})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "homeo/files/subscribe"})
    assert (await client.receive_json())["success"]
    first = await client.receive_json()
    assert [f["name"] for f in first["event"]["files"]] == ["a.homeo"]

    (rules_dir / "b.homeo").write_text("let b = on\n")
    await entry.runtime_data.async_reload()  # what the watcher does
    update = await client.receive_json()
    assert [f["name"] for f in update["event"]["files"]] == ["a.homeo", "b.homeo"]
