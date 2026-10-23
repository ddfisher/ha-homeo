"""Edge cases from a review of the runtime: flapping, polling, dropouts and restarts.

Tests marked `xfail(strict=True)` describe known bugs. They fail today; once a bug is fixed,
its test passes, strict mode turns that into a failure, and the marker should be removed.
"""

import pytest

from custom_components.homeo.core.values import INF

from .fakehost import DEFAULT_START as DEFAULT_T
from .fakehost import standard_world, world
from .rig import Rig

MIN = 60
H = 3600


# ----------------------------------------------------------- no flapping, no polling


def test_flickering_condition_with_for_never_writes():
    """`x for D` restarts its wait each time x flickers, so a noisy sensor can't flap a
    device; the write comes once the condition has held for D."""
    r = Rig("light.porch := sensor.lux < 20 for 5min")
    for _ in range(15):  # 1 h of flicker: 2 min below 20, 2 min above
        r.h.set_state("sensor.lux", "10")
        r.h.advance(2 * MIN)
        r.h.set_state("sensor.lux", "30")
        r.h.advance(2 * MIN)
    assert r.new_calls() == []
    r.h.set_state("sensor.lux", "10")
    r.h.advance(5 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["light.turn_on light.porch"]


def test_attribute_churn_on_a_bound_entity_writes_nothing():
    """A thermostat reports its current temperature often; that must neither re-write the
    setpoint nor count as a manual change."""
    r = Rig(
        """
climate.living.temperature :=
    age(manual(climate.living)) < 1h -> keep
    else                             -> 21
"""
    )
    assert r.new_calls() == ["climate.set_temperature climate.living temperature=21"]
    for i in range(100):
        r.h.advance(30)
        r.h.set_state("climate.living", "heat", {"current_temperature": 19 + i / 100}, merge=True)
    assert r.new_calls() == []
    assert r.rt.cells["manual:climate.living"].extra["t"].hi == float("-inf")


def test_rules_that_dont_depend_on_time_schedule_no_timers():
    h = world(**{"sensor.co2": ("500", {"unit_of_measurement": "ppm"}), "switch.pump": "off"})
    r = Rig("switch.pump := sensor.co2 > 800", host=h)
    for i in range(50):
        h.advance(1)
        h.set_state("sensor.co2", str(500 + i * 20))
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    assert h.pending_timers() == []


def test_one_host_timer_covers_every_wake_up():
    """The runtime keeps a single host timer, armed for the earliest instant at which time
    alone could change a result (no polling, no timer per rule)."""
    h = world(
        **{
            "sensor.lux": ("100", {"unit_of_measurement": "lx"}),
            "input_button.boost": "unknown",
            "light.porch": "off",
            "switch.pump": "off",
            "fan.bathroom": "off",
        }
    )
    Rig(
        """
light.porch := sensor.lux < 20 for 5min
switch.pump := now in 22:00..06:00
fan.bathroom := age(input_button.boost) < 30min
on every 15min:
    notify.mobile_app_phone(message: "tick")
""",
        host=h,
    )
    start = h.t  # Monday 08:00
    assert h.pending_timers() == [start + 15 * MIN]  # the next `every` tick
    h.advance(MIN)
    h.press("input_button.boost")  # boost ends at 08:31
    h.advance(MIN)
    h.set_state("sensor.lux", "10")  # `for` settles at 08:07
    for i in range(20):
        h.set_state("sensor.lux", str(10 + i % 3))  # still below 20
    assert h.pending_timers() == [start + 7 * MIN]


# ----------------------------------------------------------------- dropouts (D16)


def test_action_on_bool_doesnt_refire_after_a_dropout():
    """Change times skip unknown periods, so on → unavailable → on isn't a new occurrence."""
    r = Rig('on binary_sensor.bathroom_window:\n    notify.mobile_app_phone(message: "open")')
    r.h.advance(5)
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["notify.mobile_app_phone message=open"]
    r.h.advance(MIN)
    r.h.set_state("binary_sensor.bathroom_window", "unavailable")
    r.h.advance(MIN)
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == []
    r.h.set_state("binary_sensor.bathroom_window", "off")
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["notify.mobile_app_phone message=open"]


def test_action_doesnt_fire_when_a_sensor_reconnects_already_on():
    h = standard_world()
    h.set_state("binary_sensor.bathroom_window", "unavailable", notify=False)
    r = Rig('on binary_sensor.bathroom_window:\n    notify.mobile_app_phone(message: "open")', h)
    r.h.advance(MIN)
    r.h.set_state("binary_sensor.bathroom_window", "on")  # reconnects; not an opening
    assert r.new_calls() == []


def test_action_with_unknown_guard_marks_the_occurrence_handled():
    """D14: the body runs only if the guard is true; the occurrence is handled either way,
    so a guard that becomes known later doesn't fire it after the fact."""
    r = Rig(
        """
on input_button.doorbell if sensor.bathroom_co2 > 800:
    notify.mobile_app_phone(message: "ding")
"""
    )
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    r.h.advance(5)
    r.h.press("input_button.doorbell")
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == []
    r.h.advance(5)
    r.h.press("input_button.doorbell")
    assert r.new_calls() == ["notify.mobile_app_phone message=ding"]


# -------------------------------------------------------------- latest, count (I7, I13)


def test_latest_group_without_a_matching_sub_arm_falls_back_to_the_next_arm():
    r = Rig(
        """
switch.pump := latest
    input_button.desk_on
        | binary_sensor.bathroom_window -> on
    input_button.desk_off               -> off
    else                                -> keep
"""
    )
    r.h.advance(5)
    r.h.press("input_button.desk_off")
    r.h.advance(5)
    r.h.press("input_button.desk_on")  # most recent, but its only sub-arm doesn't match
    assert r.new_calls() == []  # desk_off still wins: off, as the pump already is
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_count_window_excludes_an_occurrence_exactly_d_ago():
    """I7: the window is (now − D, now]."""
    r = Rig("switch.pump := count(input_button.boost, 10min) >= 2")
    r.h.advance(5)
    t0 = r.h.t
    r.h.press("input_button.boost")
    r.h.advance(5 * MIN)
    r.h.press("input_button.boost")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance_to(t0 + 10 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance_to(t0 + 10 * MIN)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


# ------------------------------------------------------------------ machines (D13)


def test_selection_that_chains_on_writes_nothing_in_between():
    """Selecting a state from the UI settles the machine before bindings run, so a state it
    passes through at once never reaches a device."""
    r = Rig(
        """
state house = "home":
    house == "home" and fresh(input_button.goodnight) -> "away"
    house == "away" and binary_sensor.hall_motion     -> "home"
switch.pump := house == "away"
"""
    )
    r.h.set_state("binary_sensor.hall_motion", "on")
    r.rt.set_machine("house", "away")
    assert r.machine("house") == "home"
    assert r.new_calls() == []


def test_selecting_another_state_revives_a_stopped_machine():
    r = Rig(
        """
state m = "a":
    m == "a" and fresh(input_button.boost) -> "b"
    m == "b" and binary_sensor.bathroom_window -> "a"
"""
    )
    r.h.set_state("binary_sensor.bathroom_window", "on")
    r.h.advance(5)
    r.h.press("input_button.boost")  # a → b → a in one step: reported as a loop (D13b)
    assert any("loops" in msg for level, msg in r.h.logs if level == "error")
    assert r.h.stopped == {"m": "a → b → a"}  # shown as a Repairs issue
    r.rt.set_machine("m", "a")
    assert r.machine("m") == "a"
    assert r.h.stopped == {}
    r.h.set_state("binary_sensor.bathroom_window", "off")
    r.h.advance(5)
    r.h.press("input_button.boost")
    assert r.machine("m") == "b"  # running again


def test_reloading_restarts_a_stopped_machine():
    rules = """
state m = "a":
    m == "a" and fresh(input_button.boost) -> "b"
    m == "b" and binary_sensor.bathroom_window -> "a"
"""
    r = Rig(rules)
    r.h.set_state("binary_sensor.bathroom_window", "on")
    r.h.advance(5)
    r.h.press("input_button.boost")
    assert r.h.stopped == {"m": "a → b → a"}
    r.reload(rules)
    assert r.h.stopped == {}
    assert not r.rt.machines["m"].failed


def test_reselecting_the_current_state_revives_a_stopped_machine():
    r = Rig(
        """
state m = "a":
    m == "a" and fresh(input_button.boost) -> "b"
    m == "b" and binary_sensor.bathroom_window -> "a"
"""
    )
    r.h.set_state("binary_sensor.bathroom_window", "on")
    r.h.advance(5)
    r.h.press("input_button.boost")
    assert r.machine("m") == "b"
    r.h.set_state("binary_sensor.bathroom_window", "off")
    r.rt.set_machine("m", "b")  # stuck in b; pick b again
    assert r.h.stopped == {}
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.machine("m") == "a"  # its rules apply again


# --------------------------------------------------------- timestamps in the future


ALARM = {"device_class": "timestamp"}


def alarm_world(at: float):
    h = standard_world()
    h.set_state("sensor.next_alarm", h.iso(at), ALARM, notify=False)
    return h


def test_action_on_a_future_timestamp_fires_when_the_time_comes():
    """A next-alarm sensor hasn't happened until its time comes (§3: t(x) is when x last
    happened), so moving the alarm doesn't fire the action."""
    h = alarm_world(at=DEFAULT_T + H)
    r = Rig('on sensor.next_alarm:\n    notify.mobile_app_phone(message: "wake up")', host=h)
    assert r.new_calls() == []
    r.h.advance(MIN)
    r.h.set_state("sensor.next_alarm", h.iso(h.t + 2 * H), ALARM)  # alarm moved later
    assert r.new_calls() == []
    r.h.advance(2 * H - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["notify.mobile_app_phone message=wake up"]


def test_action_before_a_future_timestamp():
    h = alarm_world(at=DEFAULT_T + H)
    r = Rig(
        'on sensor.next_alarm - 30min:\n    notify.mobile_app_phone(message: "heating on")',
        host=h,
    )
    r.h.advance(30 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["notify.mobile_app_phone message=heating on"]


def test_time_until_a_future_timestamp_still_reads_its_value():
    h = alarm_world(at=DEFAULT_T + H)
    r = Rig("switch.pump := sensor.next_alarm - now < 30min", host=h)
    assert r.new_calls() == []
    r.h.advance(30 * MIN)
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_age_of_a_future_timestamp_is_unknown_until_settled():
    """Before any occurrence has been seen, the last one is only known to be before load,
    as for change times from before a restart (D17): `age() < 10min` is unknown for 10 min
    (no write), then false."""
    h = alarm_world(at=DEFAULT_T + H)
    h.set_state("switch.pump", "on", notify=False)
    r = Rig("switch.pump := age(sensor.next_alarm) < 10min", host=h)
    assert r.new_calls() == []
    r.h.advance(10 * MIN)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_past_occurrence_is_remembered_when_the_value_moves_on():
    """When the alarm rings, the sensor moves on to the next alarm; `age()` still counts
    from the one that rang."""
    h = alarm_world(at=DEFAULT_T + H)
    r = Rig("switch.pump := age(sensor.next_alarm) < 10min", host=h)
    r.h.advance(H)  # the alarm rings
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance(5)
    r.h.set_state("sensor.next_alarm", h.iso(h.t + 86400), ALARM)  # tomorrow's alarm
    assert r.rt.cells["state:sensor.next_alarm"].occurrence().lo == DEFAULT_T + H
    r.h.advance(10 * MIN - 5)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_never_happened_timestamp_is_still_never():
    r = Rig("switch.pump := age(input_button.boost) > 1h")  # never pressed
    assert r.rt.cells["state:input_button.boost"].occurrence().hi == -INF
    assert r.new_calls() == ["switch.turn_on switch.pump"]


# -------------------------------------------------------------------- dry run (I20)


def test_turning_dry_run_off_brings_devices_in_line():
    r = Rig("# mode: dry_run\nswitch.pump := sensor.bathroom_co2 > 800\n")
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == []
    r.reload("switch.pump := sensor.bathroom_co2 > 800\n")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.reload("switch.pump := sensor.bathroom_co2 > 800\n")
    assert r.new_calls() == []  # once


def test_turning_dry_run_off_leaves_matching_devices_alone():
    r = Rig("# mode: dry_run\nswitch.pump := sensor.bathroom_co2 > 800\n")
    r.h.set_state("sensor.bathroom_co2", "900")
    r.h.set_state("switch.pump", "on")  # someone already switched it on
    r.reload("switch.pump := sensor.bathroom_co2 > 800\n")
    assert r.new_calls() == []


def test_dry_run_writes_dont_count_for_manual_detection():
    """Nothing was written in dry run, so a person's change is manual even if it matches
    the would-be write."""
    r = Rig(
        """# mode: dry_run
switch.pump :=
    age(manual(switch.pump)) < 1h -> keep
    else                         -> sensor.bathroom_co2 > 800
"""
    )
    r.h.set_state("sensor.bathroom_co2", "900")  # would switch on
    r.h.advance(5)
    r.h.set_state("switch.pump", "on")  # a person switches it on
    assert r.rt.cells["manual:switch.pump"].extra["t"].lo == r.h.t


# ---------------------------------------------------------------- time of day (I3)


def test_now_at_or_after_midnight_is_always_true():
    r = Rig("switch.pump := now >= 00:00")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_now_before_midnight_is_always_false():
    h = standard_world()
    h.set_state("switch.pump", "on", notify=False)
    r = Rig("switch.pump := now < 00:00", host=h)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


# ----------------------------------------------------------------------- known bugs


@pytest.mark.xfail(
    strict=True,
    reason="I17 says oscillation through a state machine is caught by the runtime's loop "
    "detection, but that only sees one propagation; a loop through the device spans many",
)
def test_oscillation_through_a_device_and_a_machine_is_stopped():
    h = standard_world()
    h.simulate_devices = False
    r = Rig(
        """
state m = "a":
    m == "a" and switch.pump -> "b"
    m == "b" and not switch.pump -> "a"
switch.pump := m == "a"
""",
        host=h,
    )
    for _ in range(10):  # the switch reports each write back, as a real one would
        last = r.h.calls[-1]
        r.h.advance(0.5)
        r.h.set_state("switch.pump", "on" if last.service == "turn_on" else "off", ours=True)
    assert len(r.h.calls) < 5
    assert any("loop" in msg for level, msg in r.h.logs if level == "error")


@pytest.mark.xfail(
    strict=True,
    reason="an `any`-typed result is passed to OnOffWriter unconverted, which treats every "
    "value except False (including the string 'off' and 0) as on",
)
def test_any_typed_off_turns_an_on_off_device_off():
    h = standard_world()  # sensor.mystery is `unknown` at load, so its type is `any`
    h.set_state("switch.pump", "on", notify=False)
    r = Rig("switch.pump := sensor.mystery", host=h)
    r.h.set_state("sensor.mystery", "off")
    assert r.new_calls() == ["switch.turn_off switch.pump"]
