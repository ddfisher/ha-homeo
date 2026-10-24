"""Time-of-day entities (I24) and the light `transition` setting (I25)."""

import pytest

from custom_components.homeo.core.checker import compile_source
from custom_components.homeo.core.errors import CompileError
from custom_components.homeo.core.types import T

from .fakehost import standard_world
from .rig import Rig

MIN = 60
HOUR = 3600


def world(start="20:00:00", end="23:00:00"):
    """Monday 08:00 UTC, with time-only helpers for an evening window."""
    h = standard_world()
    attrs = {"has_date": False, "has_time": True}
    h.set_state("input_datetime.dim_start", start, attrs, notify=False)
    h.set_state("input_datetime.dim_end", end, attrs, notify=False)
    h.set_state(
        "input_datetime.wake",
        "2026-01-05 07:00:00",
        {"has_date": True, "has_time": True},
        notify=False,
    )
    h.set_state("time.alarm", "06:30:00", {}, notify=False)
    return h


def until(r: Rig, hh: int, mm: int = 0) -> None:
    """Advance to the next local hh:mm (the fake world is in UTC)."""
    target = (hh * 60 + mm) * 60
    r.h.advance((target - r.h.t % 86400) % 86400 or 86400)


def errors(src: str) -> list[str]:
    with pytest.raises(CompileError) as e:
        compile_source(src, "test.homeo", world())
    return [d.message for d in e.value.diagnostics]


# ------------------------------------------------------------------ checking


def test_time_only_helpers_and_time_entities_are_times_of_day():
    p, _ = compile_source(
        "let a = input_datetime.dim_start\nlet b = time.alarm\nlet c = input_datetime.wake\n",
        "test.homeo",
        world(),
    )
    assert [p.lets[n].type for n in "abc"] == [T.TIMEOFDAY, T.TIMEOFDAY, T.STRING]


def test_where_times_of_day_from_entities_are_accepted():
    compile_source(
        """
let start = input_datetime.dim_start
let end   = input_datetime.dim_end
switch.pump := now in start..end
light.porch := now >= start + 30min
light.hall  := on(brightness: 100% - 96% * ((every 5min - start) / (end - start)))
fan.bathroom := age(time.alarm) < 10min
on end:
    notify.mobile_app_phone(message: "late")
""",
        "test.homeo",
        world(),
    )


def test_time_of_day_errors():
    assert errors("switch.pump := now in input_datetime.wake..23:00\n") == [
        "a time range needs times of day, e.g. `22:00..06:00`"
    ]
    assert errors("let d = input_datetime.dim_start + sensor.lux * 1min\n") == [
        "a time of day can only be shifted by a fixed duration"
    ]
    assert errors("let d = now - sunset\n") == [
        "`sunset` moves from day to day, so it can't be subtracted"
    ]


def test_transition_takes_a_duration():
    compile_source("light.hall := on(brightness: 50%, transition: 5min)\n", "test.homeo", world())
    assert errors("light.hall := on(brightness: 50%, transition: 5)\n") == [
        "`transition` takes a duration, e.g. `transition: 5min`"
    ]
    assert errors("fan.bathroom := on(transition: 5s)\n") == [
        "`fan.bathroom` has no setting `transition`"
    ]


# ------------------------------------------------------------------ running


def test_range_follows_the_helpers():
    r = Rig(
        "switch.pump := now in input_datetime.dim_start..input_datetime.dim_end\n", host=world()
    )
    r.new_calls()
    until(r, 20)
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    until(r, 21)
    # moving the end earlier ends the window at once
    r.h.set_state("input_datetime.dim_end", "20:30:00", {"has_date": False, "has_time": True})
    assert r.new_calls() == ["switch.turn_off switch.pump"]
    r.h.set_state("input_datetime.dim_end", "unavailable", {})
    r.h.set_state("input_datetime.dim_end", "23:00:00", {"has_date": False, "has_time": True})
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_time_point_from_a_helper():
    src = 'on input_datetime.dim_end:\n    notify.mobile_app_phone(message: "night")\n'
    r = Rig(src, host=world())
    until(r, 22, 59)
    assert r.new_calls() == []
    r.h.advance(MIN)
    assert r.new_calls() == ["notify.mobile_app_phone message=night"]
    r.h.set_state("input_datetime.dim_end", "23:30:00", {"has_date": False, "has_time": True})
    r.h.advance(30 * MIN)  # moved later: it happens again at the new time
    assert r.new_calls() == ["notify.mobile_app_phone message=night"]


def test_dimming_ramp():
    src = """
let start = input_datetime.dim_start
let end   = input_datetime.dim_end
light.hall :=
    now in start..end -> on(brightness: 100% - 96% * ((every 30min - start) / (end - start)), transition: 30min)
    else              -> off
"""
    r = Rig(src, host=world())
    r.new_calls()
    until(r, 20)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=100 transition=1800"]
    until(r, 21, 30)
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=84 transition=1800",
        "light.turn_on light.hall brightness_pct=68 transition=1800",
        "light.turn_on light.hall brightness_pct=52 transition=1800",
    ]
    until(r, 23)
    assert r.new_calls()[-1] == "light.turn_off light.hall"


def test_time_since_a_time_of_day_moves_with_the_clock():
    src = "switch.pump := now - input_datetime.dim_start < 1h\n"
    r = Rig(src, host=world())
    r.new_calls()
    until(r, 20)
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    assert r.h.t + HOUR in r.h.pending_timers()
    until(r, 21)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_window_across_midnight():
    r = Rig(
        "switch.pump := now in input_datetime.dim_start..input_datetime.dim_end\n",
        host=world(start="23:00:00", end="01:00:00"),
    )
    r.new_calls()
    until(r, 23)
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    until(r, 1)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_transition_is_sent_not_read_back():
    r = Rig(
        "light.hall :=\n"
        "    age(manual(light.hall)) < 1h  -> keep\n"
        "    switch.pump                   -> on(brightness: 20%, transition: 2min)\n"
        "    else                          -> on(brightness: 80%, transition: 2min)\n",
        host=world(),
    )
    r.h.set_state("light.hall", "on", {"brightness": 204})
    r.new_calls()  # matches: transition isn't compared with the device
    r.h.set_state("switch.pump", "on")
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=20 transition=120"]
    # the light reports its progress with a fresh context: not a person
    r.h.advance(30)
    r.h.set_state("light.hall", "on", {"brightness": 150})
    assert r.rt.cells["manual:light.hall"].extra["t"].lo == float("-inf")
    r.h.advance(90)
    r.h.set_state("light.hall", "on", {"brightness": 51})
    # after the transition, a change away from what was written is a person's
    r.h.advance(60)
    r.h.set_state("light.hall", "on", {"brightness": 255})
    assert r.rt.cells["manual:light.hall"].extra["t"].lo == r.h.t


def test_explain_shows_the_helpers():
    from custom_components.homeo.core.explain import explain

    r = Rig(
        "switch.pump := now in input_datetime.dim_start..input_datetime.dim_end\n", host=world()
    )
    until(r, 21)
    (e,) = explain(r.rt, "switch.pump")
    assert e["result"] == "on"
    assert e["values"]["input_datetime.dim_start"] == "20:00"
    assert e["values"]["input_datetime.dim_end"] == "23:00"
    assert e["values"]["now"] == "21:00:00 (now)"
