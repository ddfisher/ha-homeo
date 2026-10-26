import pytest

from custom_components.homeo.core import ir
from custom_components.homeo.core.checker import Globals, compile_source
from custom_components.homeo.core.errors import CompileError
from custom_components.homeo.core.types import T
from custom_components.homeo.core.values import KEEP

from .fakehost import standard_world


@pytest.fixture
def host():
    return standard_world()


def ok(host, src, file="test.homeo", globals_=None):
    program, _ = compile_source(src, file, host, globals_)
    return program


def errs(host, src, file="test.homeo", globals_=None):
    with pytest.raises(CompileError) as e:
        compile_source(src, file, host, globals_)
    return e.value.diagnostics


def err(host, src, **kw):
    return errs(host, src, **kw)[0]


# ------------------------------------------------------------------------ names


def test_alias_and_computed_lets(host):
    p = ok(
        host,
        """
let co2 = sensor.bathroom_co2
let stuffy = co2 > 800 or sensor.bathroom_humidity > 70%
fan.bathroom := stuffy
""",
    )
    (b,) = p.bindings
    assert b.expr.text == "sensor.bathroom_co2 > 800 or sensor.bathroom_humidity > 70%"


def test_lets_may_be_used_before_definition(host):
    ok(host, "fan.bathroom := stuffy\nlet stuffy = sensor.bathroom_co2 > 800\n")


def test_unknown_entity_did_you_mean(host):
    d = err(host, "fan.bathroom := sensor.bathroom_c02 > 800")
    assert d.message == "unknown entity `sensor.bathroom_c02`"
    assert d.hint == "did you mean `sensor.bathroom_co2`?"


def test_unknown_name(host):
    d = err(host, "let stuffy = co2x > 1\nlet co2 = sensor.bathroom_co2\n")
    assert d.message == "unknown name `co2x`" and "`co2`" in d.hint


def test_registry_only_entity_is_known(host):
    ok(host, "let x = light.disabled_one")


def test_let_on_left_of_bind(host):
    d = err(host, "let fan1 = fan.bathroom\nfan1 := on\n")
    assert "`fan1` is a `let` name" in d.message
    assert d.hint == "write the full entity id: `fan.bathroom := …`"


def test_local_name_shadows_domain(host):
    # The design's own example: `remote` is an HA domain and a let name.
    p = ok(
        host,
        """
let remote = event.bathroom_remote
state m = "a":
    fresh(remote.press) -> "b"
""",
    )
    assert "event:event.bathroom_remote.press" in p.cells


def test_definition_cycle(host):
    d = err(host, "let a = b\nlet b = not a\n")
    assert "definition cycle" in d.message


def test_duplicate_names(host):
    assert "already defined" in err(host, "let a = on\nlet a = off\n").message


# ------------------------------------------------------------------- attributes


def test_attributes_and_event_types(host):
    p = ok(
        host,
        """
let b = light.hall.brightness > 50%
let t = climate.living.current_temperature < 18
let e = event.bathroom_remote.double_press
let raw = event.bathroom_remote["event_type"] == "press"
""",
    )
    assert "attr:light.hall.brightness" in p.cells
    assert "event:event.bathroom_remote.double_press" in p.cells
    assert "attr:event.bathroom_remote.event_type" in p.cells


def test_percent_against_brightness_is_scaled(host):
    p = ok(host, "let b = light.hall.brightness > 50%\nlight.porch := b\n")
    cmp = p.bindings[0].expr
    assert cmp.right.value.lo == pytest.approx(127.5)


def test_unknown_event_type(host):
    d = err(host, "let e = event.bathroom_remote.dubble_press")
    assert "no event type `dubble_press`" in d.message and "double_press" in d.hint


def test_missing_attribute_is_a_warning(host):
    p = ok(host, "let x = sensor.lux.colour")
    assert any("no attribute `colour`" in w.message for w in p.warnings)


# ------------------------------------------------------------------------ types


def test_timestamp_as_condition_hint(host):
    d = err(host, "fan.bathroom := input_button.boost")
    assert "is a timestamp" in d.message
    assert d.hint == "did you mean `age(input_button.boost) < 30min`?"


def test_time_of_day_as_condition_hint(host):
    d = err(host, "fan.bathroom := 07:00")
    assert "time of day" in d.message and "now >= 07:00" in d.hint


def test_bool_vs_quoted_on(host):
    d = err(host, 'let x = light.hall == "on"')
    assert "can't compare" in d.message and "without quotes" in d.hint


def test_percent_vs_other_unit(host):
    d = err(host, "let x = sensor.outdoor_temp > 50%")
    assert "measured in °C" in d.message


def test_percent_vs_percent_sensor(host):
    ok(host, "let x = sensor.bathroom_humidity > 60%")


def test_string_ordering_rejected(host):
    assert "can't order" in err(host, 'let x = person.ann < "home"').message


def test_arithmetic_types(host):
    p = ok(
        host,
        """
let a = now - sensor.last_boot > 2h
let b = age(sensor.lux) / 1min > 3
let c = now in sunset - 30min..23:00
""",
    )
    del p
    assert "can't apply" in err(host, 'let x = sensor.lux + "a"').message


def test_behaviors_can_be_bound_to_numeric_targets(host):
    """D23: a value that moves with the clock can be a binding result for numeric targets,
    directly, in `on(...)` and in arms."""
    ok(host, "input_number.target := age(sensor.lux) / 1min")
    ok(
        host,
        """
let level =
    now in 19:00..23:00     -> 100% - 96% * (age(19:00) / 4h)
    else                    -> 100%
light.hall := on(brightness: level, kelvin: 2700 - age(19:00) / 1min)
""",
    )


def test_behaviors_still_cant_be_tracked(host):
    src = """
let level =
    now in 19:00..23:00     -> 100% - 96% * (age(19:00) / 4h)
    else                    -> 100%
"""
    assert "changes continuously" in err(host, src + "let x = age(level) > 1h").message
    assert "changes continuously" in err(host, src + "let x = previous(level)").message
    assert "two values that both change" in err(host, src + "let x = level * level").message


def test_age_of_moving_value_rejected(host):
    assert "changes continuously" in err(host, "let x = age(age(sensor.lux)) > 1h").message


def test_any_sensor_is_lenient(host):
    ok(host, 'let a = sensor.mystery > 3\nlet b = sensor.mystery == "x"\n')


def test_comparison_with_select_options_warns(host):
    p = ok(host, 'let x = input_select.mode == "ecco"')
    assert any('no option "ecco"' in w.message for w in p.warnings)


# --------------------------------------------------------------------- time


def test_now_comparisons_become_ranges(host):
    p = ok(host, "let a = now >= 07:00\nlight.porch := a\n")
    node = p.bindings[0].expr
    assert isinstance(node, ir.InTimeRange)
    assert node.start.seconds == 7 * 3600 and node.end.seconds == 0


def test_days(host):
    p = ok(host, "light.porch := today in fri..mon and not binary_sensor.holiday")
    day = p.bindings[0].expr.left
    assert isinstance(day, ir.DayIn) and sorted(day.days.days) == [0, 4, 5, 6]
    ok(host, "let w = today in weekend\nlet s = today == sat\nlet l = today in [sat, sun]\n")


def test_numeric_range_rejected(host):
    assert "ranges are for" in err(host, "let x = sensor.lux in 1..5").message


def test_time_points_in_occurrence_contexts(host):
    p = ok(
        host,
        """
state m = "a":
    fresh(07:00) -> "b"
on sunset - 30min:
    notify.mobile_app_phone(message: "dusk")
""",
    )
    assert "timepoint:07:00" in p.cells and "timepoint:sunset - 30min" in p.cells


def test_every(host):
    p = ok(host, 'on every 15min:\n    notify.mobile_app_phone(message: "tick")\n')
    assert p.cells["every:900"].period == 900


def test_within_expansion(host):
    p = ok(host, "light.porch := binary_sensor.hall_motion within 5min")
    e = p.bindings[0].expr
    assert e.text == "binary_sensor.hall_motion within 5min"
    assert isinstance(e, ir.BoolOp) and e.op == "or"
    assert isinstance(e.right, ir.Compare) and e.right.op == "<"
    assert e.right.text == "age(binary_sensor.hall_motion) < 5min"


def test_not_keeps_its_parentheses_in_texts(host):
    # Texts key history cells: `(not m) within 5min` and `not (m within 5min)` mean different
    # things, so they must not share one (found in review).
    p = ok(
        host,
        """
let m = binary_sensor.hall_motion
light.hall  := ((not m) within 5min) for 1min
light.porch := (not (m within 5min)) for 1min
switch.pump := not (m or binary_sensor.bathroom_window)
""",
    )
    hall, porch, pump = (b.expr for b in p.bindings)
    assert hall.text == "((not binary_sensor.hall_motion) within 5min) for 1min"
    assert porch.text == "(not (binary_sensor.hall_motion within 5min)) for 1min"
    assert pump.text == "not (binary_sensor.hall_motion or binary_sensor.bathroom_window)"
    assert hall.right.left.cell is not porch.right.left.cell


def test_for_and_within_take_conditions(host):
    d = err(host, "light.porch := input_button.boost for 5min")
    assert d.message == "`input_button.boost` is a timestamp, but `for` needs a condition"
    assert d.hint == (
        "use `age(input_button.boost) < 5min` for 'happened in the last 5min', "
        "or `age(input_button.boost) >= 5min` for 'hasn't happened for 5min'"
    )
    d = err(host, "light.porch := manual(light.porch) within 1h")
    assert d.message == "`manual(light.porch)` is a timestamp, but `within` needs a condition"
    assert d.hint == "use `age(manual(light.porch)) < 1h` for 'happened in the last 1h'"
    d = err(host, "light.porch := sensor.lux within 5min")
    assert d.message == "`within` needs a condition, but `sensor.lux` is a number"
    d = err(host, "light.porch := binary_sensor.hall_motion within sensor.lux")
    assert "`within` needs a fixed duration" in d.message
    # a time of day: `age` of its last occurrence
    d = err(host, "light.porch := sunset within 30min")
    assert d.hint == "use `age(sunset) < 30min` for 'happened in the last 30min'"
    # without a duration literal there's no valid suggestion to make with it
    d = err(host, "light.porch := input_button.boost within sensor.lux")
    assert "sensor.lux" not in (d.hint or "")
    for src in ("within 0s", "within -5min", "for 0s"):
        d = err(host, f"light.porch := binary_sensor.hall_motion {src}")
        assert d.message.endswith("needs a positive duration"), src


def test_for_expansion(host):
    p = ok(host, "light.porch := sensor.lux < 20 for 5min")
    e = p.bindings[0].expr
    assert e.text == "sensor.lux < 20 for 5min"
    assert isinstance(e, ir.BoolOp) and isinstance(e.right.left, ir.Age)
    assert e.right.left.cell.key == "expr:sensor.lux < 20"


# ----------------------------------------------------------------- bindings


def test_writers_table(host):
    p = ok(
        host,
        """
fan.bathroom := on(percentage: 60%)
light.hall := on(brightness: 80%, kelvin: 2700) if binary_sensor.hall_motion else off
climate.living.temperature := 21
climate.living := "heat"
cover.blinds.position := 30%
cover.garage := "open"
input_number.target := 5
input_select.mode := "eco"
input_text.note := "hi"
lock.front := "locked"
media_player.tv.volume := 30%
switch.pump := on
input_boolean.guest := off
""",
    )
    assert len(p.bindings) == 13


def test_read_only_target(host):
    d = err(host, "sensor.lux := 5")
    assert d.message == "`sensor.lux` can't be driven"


def test_read_only_attribute(host):
    d = err(host, "climate.living.current_temperature := 5")
    assert d.message == "`climate.living.current_temperature` can't be bound"
    assert "`temperature`" in d.hint


def test_light_attribute_bound_separately(host):
    d = err(host, "light.hall.brightness := 50%")
    assert "can't be bound separately" in d.message and "on(brightness: 50%)" in d.hint


def test_wrong_result_type(host):
    d = err(host, "fan.bathroom := 5")
    assert "`fan.bathroom` takes" in d.message and "is a number" in d.message


def test_unknown_on_setting(host):
    d = err(host, "light.hall := on(brightnes: 50%)")
    assert "no setting `brightnes`" in d.message and "brightness" in d.hint


def test_brightness_needs_percent(host):
    assert "takes a percentage" in err(host, "light.hall := on(brightness: 128)").message


def test_on_spec_not_for_switch(host):
    assert "`switch.pump` takes" in err(host, "switch.pump := on(brightness: 5%)").message


def test_cover_literal_checked(host):
    d = err(host, 'cover.garage := "opened"')
    assert 'can\'t be set to "opened"' in d.message and "open" in d.hint


def test_select_option_checked(host):
    assert "can't be set" in err(host, 'input_select.mode := "turbo"').message


def test_volume_needs_percent(host):
    assert "takes a percentage" in err(host, "media_player.tv.volume := 0.3").message


def test_cover_state_and_position(host):
    d = err(host, 'cover.blinds := "open"\ncover.blinds.position := 50%\n')
    assert "both by state and by position" in d.message


def test_climate_mode_and_setpoint_are_independent(host):
    ok(host, 'climate.living := "heat"\nclimate.living.temperature := 20\n')


def test_two_writers_same_file(host):
    assert "bound twice" in err(host, "fan.bathroom := on\nfan.bathroom := off\n").message


def test_two_writers_across_files(host):
    g = Globals()
    p, reads = compile_source("fan.bathroom := on", "a.homeo", host, g)
    g.add(p, reads)
    d = err(host, "fan.bathroom := off", file="b.homeo", globals_=g)
    assert d.message == "`fan.bathroom` is already bound in a.homeo"
    # recompiling a.homeo itself is fine
    ok(host, "fan.bathroom := off", file="a.homeo", globals_=g)


def test_missing_else(host):
    d = err(host, "fan.bathroom :=\n    sensor.bathroom_co2 > 800 -> on\n")
    assert "must end with `else" in d.message


def test_latest_in_binding_without_else_keeps(host):
    """Issue #10: until one of the conditions has happened, leave the device alone."""
    p = ok(host, "light.desk := latest\n    input_button.desk_on -> on\n")
    last = p.bindings[0].expr.arms[-1]
    assert last.cond is None and last.result.value is KEEP
    assert last.implicit


def test_latest_in_let_may_omit_else(host):
    ok(
        host,
        "let hi = latest\n    sensor.bathroom_co2 > 800 -> true\n    sensor.bathroom_co2 < 700 -> false\n",
    )


def test_latest_conditions_must_be_occurrences(host):
    d = err(host, "let x = latest\n    sensor.lux -> 1\n")
    assert "needs a bool or a timestamp" in d.message


def test_arm_types_must_agree(host):
    assert (
        "different types"
        in err(host, 'input_number.target :=\n    switch.pump -> 1\n    else -> "a"\n').message
    )


def test_binding_reads_own_target(host):
    d = err(host, "light.hall := not light.hall")
    assert "reads `light.hall` itself" in d.message


def test_manual_self_reference_allowed(host):
    ok(host, "fan.bathroom :=\n    age(manual(fan.bathroom)) < 1h -> keep\n    else -> on\n")


def test_bindings_loop(host):
    d = err(host, "light.hall := light.porch\nlight.porch := light.hall\n")
    assert "loop" in d.message


def test_loop_through_machine_is_allowed_with_a_warning(host):
    p = ok(
        host,
        """
state m = "a":
    light.hall -> "b"
light.hall := m == "a"
""",
    )
    (w,) = p.warnings
    assert w.message == (
        "`light.hall` may switch back and forth forever: its rule reads state machine `m`, "
        "which reads `light.hall`"
    )
    assert "homeo doesn't stop such a loop" in w.hint


def test_loop_through_machine_and_another_binding_warns(host):
    p = ok(
        host,
        """
state m = "a":
    m == "a" and light.porch -> "b"
switch.pump := m == "a"
light.porch := switch.pump
""",
    )
    assert [w.message for w in p.warnings] == [
        "`switch.pump` may switch back and forth forever: these bindings and state machine "
        "`m` drive each other in a loop: `switch.pump` → `light.porch` → `switch.pump`"
    ]


def test_loop_through_machine_via_a_let_warns(host):
    p = ok(
        host,
        """
state m = "a":
    m == "a" and switch.pump -> "b"
let pumping = m == "a"
switch.pump := pumping
""",
    )
    assert "may switch back and forth forever" in p.warnings[0].message


def test_machine_reading_only_manual_or_other_entities_doesnt_warn(host):
    p = ok(
        host,
        """
state m = "a":
    m == "a" and age(manual(switch.pump)) < 1h -> "b"
    m == "b" and binary_sensor.hall_motion     -> "a"
switch.pump := m == "a"
""",
    )
    assert p.warnings == []


def test_notify_binding_not_in_v1(host):
    host.registry.add("notify.mobile_app_phone")
    assert "can't be bound in v1" in err(host, 'notify.mobile_app_phone := "x"').message


def test_service_call_in_binding(host):
    assert (
        "only allowed in `on` action bodies"
        in err(host, "light.hall := light.porch.turn_on()").message
    )


# ---------------------------------------------------------------- machines


def test_machine_states_and_typos(host):
    p = ok(
        host,
        """
state bath_mode = "auto":
    fresh(event.bathroom_remote.long_press) -> "off"
    fresh(event.bathroom_remote.press)
        | bath_mode == "auto" -> "boost"
        | else -> "auto"
    bath_mode == "boost" for 1h -> "auto"
""",
    )
    assert p.machines[0].states == ["auto", "off", "boost"]
    d = err(
        host,
        'state m = "auto":\n    m == "boots" -> "auto"\n    fresh(input_button.boost) -> "boost"\n',
    )
    assert d.message == '`m` has no state "boots"' and "boost" in d.hint


def test_machine_state_typo_in_binding(host):
    d = err(host, 'state m = "a":\n    switch.pump -> "b"\nlight.hall := m == "c"\n')
    assert 'no state "c"' in d.message


def test_machine_results_must_be_literals(host):
    d = err(host, 'state m = "a":\n    switch.pump -> person.ann\n')
    assert "must be state names" in d.message


def test_previous_self_allowed(host):
    ok(
        host,
        """
state house = "home":
    house == "home"
        | fresh(input_button.goodnight) -> "night"
    house == "night"
        | fresh(input_button.goodnight) -> previous(house)
        | age(house) > 4h -> previous(house)
""",
    )


def test_fresh_outside_state(host):
    d = err(host, "light.hall := fresh(input_button.boost)")
    assert "only allowed inside a `state` block" in d.message and "age(x)" in d.hint


def test_machine_names_unique_across_files(host):
    g = Globals()
    p, reads = compile_source('state m = "a":\n    switch.pump -> "b"\n', "a.homeo", host, g)
    g.add(p, reads)
    d = err(host, 'state m = "x":\n    switch.pump -> "y"\n', file="b.homeo", globals_=g)
    assert "already defined in a.homeo" in d.message


# ----------------------------------------------------------------- actions


def test_actions(host):
    p = ok(
        host,
        """
let porch = light.porch
on input_button.doorbell:
    notify.mobile_app_phone(message: "Someone's at the door")
on person.ann == "home" if now in 22:00..06:00:
    light.porch.turn_on(brightness_pct: 100)
    porch.turn_off()
""",
    )
    a = p.actions[1]
    assert [(c.domain, c.service, c.entity_id) for c in a.calls] == [
        ("light", "turn_on", "light.porch"),
        ("light", "turn_off", "light.porch"),
    ]
    assert a.guard is not None


def test_action_on_bound_entity(host):
    d = err(host, "light.porch := on\non input_button.doorbell:\n    light.porch.turn_off()\n")
    assert "driven by a binding" in d.message


def test_action_trigger_must_be_occurrence(host):
    assert "needs a bool or a timestamp" in err(host, "on sensor.lux:\n    notify.x()\n").message


def test_unknown_service_warns(host):
    host.services[("notify", "nobody")] = False
    p = ok(host, 'on input_button.doorbell:\n    notify.nobody(message: "x")\n')
    assert any("doesn't exist" in w.message for w in p.warnings)


def test_service_domains_without_entities(host):
    # Found in a real HA: domains like persistent_notification have services but no entities.
    p = ok(
        host,
        'on input_button.doorbell:\n    persistent_notification.create(message: "x")\n'
        "    shell_command.ring()\n",
    )
    assert [(c.domain, c.service) for c in p.actions[0].calls] == [
        ("persistent_notification", "create"),
        ("shell_command", "ring"),
    ]


def test_bus_events(host):
    p = ok(
        host,
        'on bus.zha_event(device_ieee: "ab:cd", command: "on"):\n    notify.mobile_app_phone(message: "x")\n',
    )
    cell = p.actions[0].trigger
    assert isinstance(cell, ir.BusCell) and cell.filters == {
        "device_ieee": "ab:cd",
        "command": "on",
    }


def test_history_functions(host):
    p = ok(
        host,
        """
let pressed3 = count(event.bathroom_remote.press, 10min) >= 3
let t = toggle(input_button.boost, initial: on)
let prev = previous(person.ann) == "away"
let ok = available(sensor.bathroom_co2)
light.porch := pressed3 or t or prev or ok
""",
    )
    kinds = {type(c).__name__ for c in p.cells.values()}
    assert {"CountCell", "ToggleCell"} <= kinds


def test_dry_run_directive(host):
    assert ok(host, "# mode: dry_run\nlight.porch := on\n").dry_run


def test_full_design_example(host):
    import re
    from pathlib import Path

    src = (Path(__file__).parents[2] / "docs" / "design.md").read_text()
    full = re.findall(r"```python\n(# bathroom.homeo.*?)```", src, re.S)[0]
    p = ok(host, full, file="bathroom.homeo")
    assert [b.key for b in p.bindings] == ["fan.bathroom", "light.bathroom"]
    assert p.machines[0].name == "bath_mode"
    assert p.cells["state:sensor.bathroom_co2"].type == T.NUMBER
    assert p.warnings == []


def test_multiple_errors(host):
    ds = errs(host, "fan.bathroom := 5\nlight.hall := sensor.nope\nlet x = y\n")
    assert len(ds) == 3


# ------------------------------------------------------------ review fixes (PR #1)


def test_whole_entity_binding_may_read_other_attributes(host):
    ok(host, 'climate.living := "heat" if climate.living.current_temperature < 18 else "off"')
    ok(host, 'light.hall := light.hall.friendly_name == "Hall"')


def test_whole_entity_binding_reading_its_read_back_attribute_is_a_self_read(host):
    d = err(host, "light.hall := light.hall.brightness < 50%")
    assert "reads `light.hall` itself" in d.message


def test_on_off_domains_are_bools(host):
    host.set_state("schedule.heating", "on", notify=False)
    host.set_state("update.core", "off", notify=False)
    host.set_state("acme.gadget", "on", notify=False)  # a custom integration's domain
    ok(host, "switch.pump := schedule.heating or update.core or acme.gadget")


def test_multi_state_domains_stay_strings_when_off(host):
    host.set_state("media_player.tv", "off", notify=False)
    ok(host, 'let x = media_player.tv == "playing"')


def test_action_service_data_cant_target_bound_entity(host):
    d = err(
        host,
        'light.porch := on\non input_button.doorbell:\n    light.turn_off(entity_id: "light.porch")\n',
    )
    assert "driven by a binding" in d.message


def test_solar_times_need_sun_entity(host):
    del host.states["sun.sun"]
    d = err(host, "light.porch := now in sunset..sunrise")
    assert "needs the `sun.sun` entity" in d.message and "Sun integration" in d.hint
    ok(host, "light.porch := now in 22:00..06:00")  # fixed times don't
