import re

import pytest

from custom_components.homeo.core import ir
from custom_components.homeo.core.checker import Globals, compile_source
from custom_components.homeo.core.errors import CompileError
from custom_components.homeo.core.parser import parse
from custom_components.homeo.core.printer import PREC, sexpr
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
    assert isinstance(cmp.right, ir.Scale) and cmp.right.factor == pytest.approx(2.55)
    assert cmp.right.operand.value.lo == 50
    assert cmp.text == "light.hall.brightness > 50%"  # shown as written


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
    points = {c.text for c in p.cells.values() if isinstance(c, ir.TimePointCell)}
    assert points == {"07:00", "sunset - 30min"}


def test_time_points_are_keyed_by_value(host):
    p = ok(
        host,
        """
on sunset - 10min - 5min:
    notify.mobile_app_phone(message: "a")
on sunset - (10min - 5min):
    notify.mobile_app_phone(message: "b")
on sunset - 15min:
    notify.mobile_app_phone(message: "c")
""",
    )
    a, b, c = (x.trigger for x in p.actions)
    assert a is not b and a is c
    # printed from the value, so a shared cell reads the same whichever use created it
    assert (a.text, b.text) == ("sunset - 15min", "sunset - 5min")


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
    # `(not m) within 5min` and `not (m within 5min)` mean different things (found in review).
    # `within` binds tighter than `not`, so the second needs no parentheses.
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
    assert porch.text == "(not binary_sensor.hall_motion within 5min) for 1min"
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
    cell = e.right.left.cell
    assert cell.text == "sensor.lux < 20" and cell.key == ir.expr_key(cell.expr)


# The bool entities p, q, r, and blocks over them, for the history-cell tests below.
_PQR = """
let p = binary_sensor.hall_motion
let q = binary_sensor.bathroom_window
let r = binary_sensor.holiday
let c1 =
    p -> on
    else -> off
let c2 =
    q -> on
    else -> off
"""


@pytest.mark.parametrize(
    "first, second",
    [
        ("(p or q) ?? r", "p or (q ?? r)"),
        ("(p if q else r) if r else q", "p if q else (r if r else q)"),
        ("(not p) within 5min", "not (p within 5min)"),
        ("p and (q and r)", "(p and q) and r"),
        ("c1", "c2"),  # blocks used to print as just `cascade`
    ],
)
def test_different_expressions_get_different_history_cells(host, first, second):
    # History cells are keyed by expression structure, not text (found in review).
    p = ok(host, _PQR + f"light.hall := ({first}) for 1min\nlight.porch := ({second}) for 1min\n")
    hall, porch = (b.expr.right.left.cell for b in p.bindings)
    assert hall is not porch and hall.key != porch.key
    assert hall.text != porch.text


@pytest.mark.parametrize(
    "first, second",
    [
        ("p or q", "(p) or ((q))"),
        ("p or q", "binary_sensor.hall_motion or binary_sensor.bathroom_window"),
        ("c1", "c1"),
    ],
)
def test_the_same_expression_shares_one_history_cell(host, first, second):
    p = ok(host, _PQR + f"light.hall := ({first}) for 1min\nlight.porch := ({second}) for 1min\n")
    hall, porch = (b.expr.right.left.cell for b in p.bindings)
    assert hall is porch


@pytest.mark.parametrize(
    "src",
    [
        "(p or q) ?? r",
        "p or q ?? r",
        "(p if q else r) if r else q",
        "p if q else r if r else q",
        "p if (q if r else p) else r",
        "not (p or q)",
        "(not p) within 5min",
        "not p within 5min",
        "(p within 5min) for 1min",
        "p and (q and r)",
        "(p and q) and r",
        "sensor.lux - (sensor.outdoor_temp - 1) > 2 * (3 + sensor.lux)",
        "-(sensor.lux + 1) < 0",
        "(sensor.lux ?? 0) + 1 > 5",
        "(p ?? q) == r",
        "(sensor.lux > 5) == p",
        "now in 22:00..06:00 or p",
        "(p or q) in [on, off]",
    ],
)
def test_texts_read_back_as_the_same_expression(host, src):
    # A node's text parses to the same tree as its source: no missing or changed parentheses.
    p = ok(host, _PQR + f"let x = {src}\nlight.hall := x\n")
    text = p.bindings[0].expr.text
    names = {"p": "binary_sensor.hall_motion", "q": "binary_sensor.bathroom_window"}
    names["r"] = "binary_sensor.holiday"
    want = re.sub(r"\b[pqr]\b", lambda m: names[m.group()], src)
    assert sexpr(parse(f"let x = {text}").statements[0].value) == sexpr(
        parse(f"let x = {want}").statements[0].value
    ), text


def test_history_cell_keys_dont_depend_on_inferred_types():
    # A sensor with no unit reads as `any` while it's unavailable. That mustn't change the keys
    # of the cells over it, or a reload at that moment would lose their history (found in
    # review).
    src = "switch.pump := sensor.plain > 5 within 10min\n"
    types, keys = [], []
    for state in ("7", "unavailable"):
        h = standard_world()
        h.set_state("sensor.plain", state, notify=False)
        p = ok(h, src)
        types.append(p.cells["state:sensor.plain"].type)
        keys.append({k for k in p.cells if k.startswith("expr:")})
    assert types == [T.NUMBER, T.ANY]
    assert keys[0] and keys[0] == keys[1]


def test_sugar_and_its_expansion_share_one_history_cell(host):
    # `x for D` reads `x` as a condition; written out by hand, an `any` sensor stays `any`.
    p = ok(
        host,
        "switch.pump := (sensor.mystery for 5min) for 1min\n"
        "light.porch := (sensor.mystery and age(sensor.mystery) >= 5min) for 1min\n",
    )
    pump, porch = (b.expr.right.left.cell for b in p.bindings)
    assert pump is porch and pump.text == "sensor.mystery for 5min"


@pytest.mark.parametrize(
    "first, second",
    [
        ("sensor.lux == 2 * 3", "sensor.lux == 6"),
        ("sensor.lux > -(5)", "sensor.lux > -5"),
        ("now >= 22:00", "now > 22:00"),
        ("now in sunset - 10min - 5min..23:00", "now in sunset - 15min..23:00"),
        ("today in [sat, sun]", "today == weekend"),
        ("today in mon..fri", "today in [mon, tue, wed, thu, fri]"),
        ('light.hall["brightness"] > 50', "light.hall.brightness > 50"),
        ("p and age(p) >= 5min", "p for 5min"),
    ],
)
def test_a_shared_history_cell_prints_alike_from_either_use(host, first, second):
    # Uses that share a cell print alike, so neither shows the other's text (found in review).
    p = ok(host, _PQR + f"switch.pump := ({first}) for 1min\nlight.porch := ({second}) for 1min\n")
    pump, porch = (b.expr.right.left.cell for b in p.bindings)
    assert pump is porch
    assert p.bindings[0].expr.text == p.bindings[1].expr.text


@pytest.mark.parametrize(
    "src",
    [
        "sensor.lux == 2 * 3",
        "sensor.lux > -5 and -sensor.lux < 5",
        "sensor.lux - -5 > 0",
        "age(input_button.boost) > 1h + 30min",
        "now >= sunset - 30min",
        "now < 06:00",
        "now not in 22:00..06:00",
        "now in sunset - 10min..23:00 + 30min",
        "today in mon..fri",
        "today != sat",
        "today not in [tue, thu]",
        "binary_sensor.hall_motion and age(binary_sensor.hall_motion) >= 5min",
        "binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 5min",
        "light.hall.brightness > 50%",
        'input_select.mode in ["eco", "away"]',
        "(sensor.lux > 5 within 1min) for 2min",
        "binary_sensor.hall_motion == on",
    ],
)
def test_texts_parse_back_to_the_same_structure(host, src):
    p = ok(host, f"switch.pump := {src}\n")
    e = p.bindings[0].expr
    again = ok(host, f"switch.pump := {e.text}\n").bindings[0].expr
    assert ir.structure(again) == ir.structure(e), e.text


@pytest.mark.parametrize("word", ["for", "within"])
def test_a_duration_that_divides_by_zero_is_an_error(host, word):
    # It used to fold to an unknown constant and crash the positive-duration check (found in
    # review). Elsewhere a constant division by zero stays as written, and is unknown.
    d = err(host, f"light.porch := binary_sensor.hall_motion {word} 5min / 0")
    assert d.message == f"`5min / 0` divides by zero, so `{word}` has no duration"
    p = ok(host, "switch.pump := sensor.lux > 1 / 0")
    assert p.bindings[0].expr.text == "sensor.lux > 1 / 0"


def test_failed_lets_dont_share_a_history_cell(host):
    # Each stands for its own `let`, so messages name the right one (found in review).
    ds = errs(
        host,
        "let a = nosuch.x\nlet b = nosuch.y\nlight.porch := age(a) > 5min\nlight.hall := age(b)\n",
    )
    assert ds[-1].message == "`age(b)` is a duration, but `light.hall` needs a condition"


def test_history_cell_keys_stay_short_when_nested(host):
    # A cell's structure includes the keys of the cells it reads (twice in `for`/`within`).
    src = "binary_sensor.hall_motion"
    for _ in range(12):
        src = f"({src}) within 1min"
    p = ok(host, f"switch.pump := toggle({src})\n")
    assert max(len(k) for k in p.cells) < 100


def test_messages_leave_out_block_bodies(host):
    src = """
let level =
    now in 19:00..23:00     -> 100% - 96% * (age(19:00) / 4h)
    else                    -> 100%
"""
    d = err(host, src + "let x = age(level) > 1h")
    assert d.message.startswith("`cascade{…}` changes continuously"), d.message


def test_latest_condition_reads_bind_as_written(host):
    p = ok(host, "switch.pump := latest\n    sunset - 30min -> on\n    sunrise -> off\n")
    cond = p.bindings[0].expr.arms[0].cond
    assert cond.text == "sunset - 30min" and cond.prec == PREC["-"]
    assert p.bindings[0].text == "switch.pump := latest{sunset - 30min -> on; sunrise -> off}"


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
    d = err(host, "light.hall := on(brightness: 128)")
    assert "takes a percentage" in d.message and d.hint is None


def test_number_times_percent_hints_to_put_the_percent_first(host):
    # The hint quotes the source, with `let` names, and keeps the parentheses it needs.
    src = """
let start = 19:00
let ramp = 100% - (age(start) / 3h) * 96%
light.hall := on(brightness: ramp)
"""
    d = err(host, src)
    assert "takes a percentage" in d.message
    assert d.hint == (
        "`age(start) / 3h * 96%` is a number, not a percentage: a percentage stays one only "
        "when it comes first. Write `96% * (age(start) / 3h)`"
    )
    ok(host, "light.hall := on(brightness: 100% - 96% * (age(19:00) / 3h))")


def test_volume_hints_to_put_the_percent_first(host):
    d = err(host, "media_player.tv.volume := 50% - age(19:00) / 1h * 10%")
    assert "Write `10% * (age(19:00) / 1h)`" in d.hint


def test_percent_order_hint_swaps_nested_products_in_one_go(host):
    d = err(host, "light.hall := on(brightness: 100% - 2 * (age(19:00) / 1h * 10%))")
    assert d.hint.startswith("`2 * (age(19:00) / 1h * 10%)` is a number")
    assert d.hint.endswith("Write `10% * (age(19:00) / 1h) * 2`")
    ok(host, "light.hall := on(brightness: 100% - 10% * (age(19:00) / 1h) * 2)")


def test_percent_order_hint_names_every_product_to_swap(host):
    src = """
let ramp = age(19:00) / 1h * 10%
light.hall := on(brightness: 100% - 2 * ramp)
"""
    assert err(host, src).hint == (
        "`age(19:00) / 1h * 10%` and `2 * ramp` are numbers: a percentage stays one only when "
        "it comes first. Write `10% * (age(19:00) / 1h)` and `ramp * 2`"
    )


def test_percent_order_hint_for_values_typed_at_run_time(host):
    d = err(host, "light.hall := on(brightness: sun.sun.elevation * 2%)")
    assert d.hint.endswith("Write `2% * sun.sun.elevation`")
    ok(host, "light.hall := on(brightness: 2% * sun.sun.elevation)")


def test_no_percent_order_hint_when_swapping_would_not_help(host):
    # `5 + …` is a number whichever way the product is written.
    assert err(host, "light.hall := on(brightness: 5 + age(19:00) / 1h * 10%)").hint is None


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
