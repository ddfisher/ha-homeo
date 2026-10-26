"""Runtime scenarios, each tied to a section of the design."""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.homeo.core.host import EntityState
from custom_components.homeo.core.runtime import NOTHING

from .fakehost import FakeHost, standard_world
from .rig import Rig

MIN = 60
H = 3600

FAN_RULE = """
fan.bathroom :=
    age(input_button.boost) < 30min          -> on
    binary_sensor.bathroom_window            -> off
    else                                     -> sensor.bathroom_co2 > 800
"""


# ------------------------------------------------------------ cascades (§8)


def test_cascade_writes_on_result_change():
    r = Rig(FAN_RULE)
    assert r.new_calls() == []  # fan already off, matches
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.set_state("sensor.bathroom_co2", "950")
    assert r.new_calls() == []  # same result: no write
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]


def test_temporary_override_expires_exactly():
    r = Rig(FAN_RULE)
    r.h.advance(5)
    r.h.press("input_button.boost")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.advance(30 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]


def test_guard_groups_and_on_settings():
    r = Rig(
        """
let night = now in 22:00..07:00
light.hall :=
    binary_sensor.hall_motion
        | night                        -> on(brightness: 10%, kelvin: 2200)
        | else                         -> on(brightness: 100%)
    else                               -> off
"""
    )
    r.h.set_state("binary_sensor.hall_motion", "on")
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=100"]
    r.h.advance_to(r.h.local(2026, 1, 5, 22, 0))
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=10 color_temp_kelvin=2200"]
    r.h.set_state("binary_sensor.hall_motion", "off")
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_unknown_condition_falls_through():
    r = Rig(
        """
switch.pump :=
    sensor.bathroom_co2 > 800   -> on
    else                        -> off
"""
    )
    r.h.set_state("switch.pump", "on")
    r.new_calls()
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    assert r.new_calls() == []  # result off is the same as before (off): no write


# -------------------------------------------------------- unknown handling (D16)


def test_unknown_periods_are_skipped():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800")
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    r.h.set_state("sensor.bathroom_co2", "950")
    assert r.new_calls() == []  # on → unknown → on writes nothing
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    r.h.set_state("sensor.bathroom_co2", "500")
    assert r.new_calls() == ["switch.turn_off switch.pump"]  # on → unknown → off


def test_kleene_or():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800 or binary_sensor.bathroom_window")
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_change_times_skip_dropouts():
    r = Rig("light.porch := sensor.lux < 20 for 5min")
    r.h.set_state("sensor.lux", "10")
    r.h.advance(3 * MIN)
    r.h.set_state("sensor.lux", "unavailable")
    r.h.advance(30)
    r.h.set_state("sensor.lux", "12")
    assert r.new_calls() == []
    r.h.advance(2 * MIN - 30)
    assert r.new_calls() == ["light.turn_on light.porch"]  # dropout didn't restart the 5 min


def test_within():
    # D11: `x within D` = `x or age(x) < D`: true now, or until less than D ago
    r = Rig("light.hall := binary_sensor.hall_motion within 5min")
    r.h.advance(MIN)
    r.h.set_state("binary_sensor.hall_motion", "on")
    assert r.new_calls() == ["light.turn_on light.hall"]
    r.h.advance(10 * MIN)  # still true while it's on
    assert r.new_calls() == []
    r.h.set_state("binary_sensor.hall_motion", "off")
    r.h.advance(5 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance(1)  # exactly 5 min after it turned off
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_within_after_a_restart_waits_before_deciding():
    # A change from before the restart could have been just before it: unknown for D, then
    # false, so a reboot doesn't switch the light on (D17 rule 2)
    r = Rig("light.hall := binary_sensor.hall_motion within 10min")
    r.h.set_state("light.hall", "on")
    r.restart(downtime=60)
    r.h.advance(10 * MIN - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_within_retrigger_restarts_the_window():
    r = Rig("light.hall := binary_sensor.hall_motion within 5min")
    m = "binary_sensor.hall_motion"
    r.h.advance(MIN)
    r.h.set_state(m, "on")
    r.h.set_state(m, "off")
    r.new_calls()
    r.h.advance(4 * MIN)
    r.h.set_state(m, "on")
    r.h.advance(10)
    r.h.set_state(m, "off")
    r.h.advance(5 * MIN - 1)  # 9 min after the first, < 5 min after the second
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_within_true_at_a_restart():
    r = Rig("light.hall := binary_sensor.hall_motion within 10min")
    r.h.set_state("binary_sensor.hall_motion", "on")
    r.new_calls()
    r.restart(downtime=60)
    assert r.h.states["light.hall"].state == "on"
    r.h.set_state("binary_sensor.hall_motion", "off")
    r.h.advance(10 * MIN)
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_within_sensor_unavailable_after_turning_off():
    # D16: once D has passed, `x or age(x) < D` with x unknown is unknown, so the light is
    # left as it is until the sensor is back
    r = Rig("light.hall := binary_sensor.hall_motion within 5min")
    m = "binary_sensor.hall_motion"
    r.h.advance(MIN)
    r.h.set_state(m, "on")
    r.h.set_state(m, "off")
    r.h.set_state(m, "unavailable")
    r.new_calls()
    r.h.advance(10 * MIN)
    assert r.new_calls() == []
    r.h.set_state(m, "off")
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_within_in_guards_latest_and_actions():
    src = """
let m = binary_sensor.hall_motion
light.hall :=
    m within 5min
        | sensor.lux < 50   -> on(brightness: 40%)
        | else              -> off
    else                    -> off
fan.bathroom := latest
    m within 5min           -> on
    input_button.boost      -> off
on m within 5min:
    notify.mobile_app_phone(message: "motion")
"""
    r = Rig(src)
    m = "binary_sensor.hall_motion"
    r.h.set_state("sensor.lux", "20")
    r.h.advance(MIN)
    r.new_calls()
    r.h.set_state(m, "on")
    assert sorted(r.new_calls()) == [
        "light.turn_on light.hall brightness_pct=40",
        "notify.mobile_app_phone message=motion",
    ]
    assert r.h.states["fan.bathroom"].state == "on"
    r.h.advance(1)
    r.h.press("input_button.boost")
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]
    r.h.set_state(m, "off")
    r.h.advance(MIN)
    r.h.set_state(m, "on")  # the window never closed: `on` doesn't fire again
    assert r.new_calls() == []
    r.h.set_state(m, "off")
    r.h.advance(5 * MIN)
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_not_within_and_not_for_kept_apart():
    # Found in review: these two used to share one history cell, so one got the other's result.
    src = """
let m = binary_sensor.hall_motion
light.hall  := ((not m) within 5min) for 1min
light.porch := (not (m within 5min)) for 1min
"""
    r = Rig(src)
    m = "binary_sensor.hall_motion"
    r.h.advance(20 * MIN)
    r.h.set_state(m, "on")
    r.h.advance(2 * MIN)
    assert r.h.states["light.porch"].state == "off"  # motion: not (m within 5min) is false
    assert r.h.states["light.hall"].state == "on"  # m was off until 2 min ago


def test_coalesce_and_available():
    r = Rig("switch.pump := (sensor.bathroom_co2 ?? 0) > 800 or not available(sensor.lux)")
    r.h.set_state("sensor.lux", "unavailable")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


# --------------------------------------------------------------- manual (D15, D21)


def test_manual_change_sticks_until_result_changes():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800")
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.set_state("switch.pump", "off")  # by hand
    r.h.set_state("sensor.bathroom_co2", "1000")
    assert r.new_calls() == []  # still "on": no fight
    r.h.set_state("sensor.bathroom_co2", "500")
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_timed_override_with_keep():
    r = Rig(
        """
fan.bathroom :=
    age(manual(fan.bathroom)) < 1h   -> keep
    else                             -> sensor.bathroom_co2 > 800
"""
    )
    r.h.advance(2 * H)  # get past the post-load uncertainty of manual()
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.advance(60)
    r.h.set_state("fan.bathroom", "off")  # by hand
    r.h.advance(30 * MIN)
    assert r.new_calls() == []
    r.h.advance(30 * MIN)
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]  # device state stood in: re-synced


def test_late_echo_of_our_write_is_not_manual():
    h = standard_world()
    h.simulate_devices = False
    r = Rig("fan.bathroom :=\n    age(manual(fan.bathroom)) < 1h -> keep\n    else -> on\n", host=h)
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    h.advance(10)
    h.set_state("fan.bathroom", "on", {"percentage_step": 1})  # fresh context, matches our write
    cell = r.rt.cells["manual:fan.bathroom"]
    assert cell.extra["t"].hi < h.t


def test_availability_is_not_manual():
    r = Rig("switch.pump := on")
    r.new_calls()
    r.h.set_state("switch.pump", "unavailable")
    r.h.set_state("switch.pump", "off")
    assert r.rt.cells.get("manual:switch.pump") is None
    sink = r.rt.binding("switch.pump")
    assert sink.last_known is True


def test_manual_ignores_unrelated_attributes():
    r = Rig(
        """
climate.living.temperature :=
    age(manual(climate.living)) < 1h -> keep
    else -> 21
"""
    )
    assert r.new_calls() == ["climate.set_temperature climate.living temperature=21"]
    t = r.rt.cells["manual:climate.living"].extra["t"]
    r.h.set_state("climate.living", "heat", {"current_temperature": 20}, merge=True)
    assert r.rt.cells["manual:climate.living"].extra["t"] == t


# ---------------------------------------------------------------- latest (D19)


def test_latest_buttons():
    r = Rig(
        """
light.desk := latest
    input_button.desk_on       -> on
    input_button.desk_off      -> off
    else                       -> keep
"""
    )
    assert r.new_calls() == []
    r.h.advance(1)
    r.h.press("input_button.desk_on")
    assert r.new_calls() == ["light.turn_on light.desk"]
    r.h.advance(1)
    r.h.press("input_button.desk_off")
    assert r.new_calls() == ["light.turn_off light.desk"]


def test_latest_hysteresis():
    r = Rig(
        """
fan.bedroom := latest
    sensor.bathroom_co2 > 800  -> on
    sensor.bathroom_co2 < 700  -> off
    else                       -> keep
"""
    )
    for v, expect in [
        ("750", []),
        ("850", ["fan.turn_on fan.bedroom"]),
        ("750", []),
        ("650", ["fan.turn_off fan.bedroom"]),
        ("750", []),
    ]:
        r.h.advance(10)
        r.h.set_state("sensor.bathroom_co2", v)
        assert r.new_calls() == expect, v


def test_latest_tie_first_arm_wins():
    r = Rig(
        """
light.desk := latest
    input_button.desk_off      -> off
    input_button.desk_on       -> on
    else                       -> keep
"""
    )
    r.h.set_state("light.desk", "on")
    r.h.advance(1)
    iso = r.h.iso(r.h.t)
    r.h.set_state("input_button.desk_on", iso)
    r.h.set_state("input_button.desk_off", iso)
    assert r.new_calls()[-1] == "light.turn_off light.desk"


def test_latest_let_without_else_is_unknown():
    r = Rig(
        """
let hi = latest
    sensor.bathroom_co2 > 800 -> true
    sensor.bathroom_co2 < 700 -> false
switch.pump := hi
"""
    )
    assert r.rt.binding("switch.pump").last_known is NOTHING
    r.h.advance(1)
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


# --------------------------------------------------------------- time (§10, §12)


def test_age_wakes_at_exact_instant():
    r = Rig("light.porch := binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 5min")
    r.h.set_state("binary_sensor.hall_motion", "on")
    assert r.new_calls() == ["light.turn_on light.porch"]
    r.h.advance(20)
    r.h.set_state("binary_sensor.hall_motion", "off")
    assert r.h.t + 300 in r.h.pending_timers()  # a wake-up at the exact instant, no polling
    r.h.advance(299)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["light.turn_off light.porch"]


def test_time_range_across_midnight():
    r = Rig("switch.pump := now in 22:00..06:00")
    assert r.new_calls() == []  # 08:00
    r.h.advance_to(r.h.local(2026, 1, 5, 22, 0))
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance_to(r.h.local(2026, 1, 6, 5, 59))
    assert r.new_calls() == []
    r.h.advance_to(r.h.local(2026, 1, 6, 6, 0))
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_now_comparison_and_days():
    r = Rig("switch.pump := now >= 18:00 and today in weekday")
    r.h.advance_to(r.h.local(2026, 1, 5, 18, 0))  # Monday
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance_to(r.h.local(2026, 1, 6, 0, 0))
    assert r.new_calls() == ["switch.turn_off switch.pump"]
    r.h.advance_to(r.h.local(2026, 1, 10, 17, 59))
    calls = r.new_calls()
    assert len(calls) == 8 and calls[-1] == "switch.turn_off switch.pump"  # Tue..Fri evenings
    r.h.advance_to(r.h.local(2026, 1, 10, 18, 0))  # Saturday
    assert r.new_calls() == []


def test_solar_time_point_action():
    r = Rig('on sunset - 30min:\n    notify.mobile_app_phone(message: "dusk")\n')
    r.h.advance_to(r.h.local(2026, 1, 5, 18, 29))
    assert r.new_calls() == []
    r.h.advance_to(r.h.local(2026, 1, 5, 18, 30))
    assert r.new_calls() == ["notify.mobile_app_phone message=dusk"]
    r.h.advance_to(r.h.local(2026, 1, 6, 18, 30))
    assert r.new_calls() == ["notify.mobile_app_phone message=dusk"]


def test_every_is_clock_aligned():
    h = standard_world(FakeHost(start=FakeHost().local(2026, 1, 5, 8, 7)))
    r = Rig('on every 15min:\n    notify.mobile_app_phone(message: "tick")\n', host=h)
    times = []
    for _ in range(3):
        h.advance_to(h.pending_timers()[0])
        times.append(h.local_now().strftime("%H:%M"))
    assert times == ["08:15", "08:30", "08:45"]
    assert len(r.new_calls()) == 3


def test_dst_spring_forward_time_point():
    h = standard_world(
        FakeHost(
            start=FakeHost(tz="Europe/Amsterdam").local(2026, 3, 28, 12), tz="Europe/Amsterdam"
        )
    )
    r = Rig('on 02:30:\n    notify.mobile_app_phone(message: "x")\n', host=h)
    h.advance(36 * H)  # through the night of 29 March (02:00 → 03:00)
    assert len(r.new_calls()) == 1
    fired = datetime.fromtimestamp(h.call_times[-1], ZoneInfo("Europe/Amsterdam"))
    assert (fired.day, fired.hour, fired.minute) == (29, 3, 0)  # first instant after the gap


def test_dst_fall_back_fires_once():
    tz = "Europe/Amsterdam"
    h = standard_world(FakeHost(start=FakeHost(tz=tz).local(2026, 10, 24, 12), tz=tz))
    r = Rig('on 02:30:\n    notify.mobile_app_phone(message: "x")\n', host=h)
    h.advance(36 * H)  # night of 25 October: 02:30 happens twice on the wall clock
    assert len(r.new_calls()) == 1
    fired = datetime.fromtimestamp(h.call_times[-1], ZoneInfo(tz))
    assert (fired.hour, fired.minute, fired.utcoffset().total_seconds()) == (2, 30, 7200)  # first


# -------------------------------------------------------- history functions (§10)


def test_count_triple_press():
    r = Rig(
        """
let triple = count(event.bathroom_remote.press, 10s) >= 3
switch.pump := triple
"""
    )
    for _ in range(3):
        r.h.advance(2)
        r.h.event("event.bathroom_remote", "press")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance(10)
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_event_type_filter():
    r = Rig("switch.pump := age(event.bathroom_remote.double_press) < 1min")
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "press")
    assert r.new_calls() == []
    r.h.event("event.bathroom_remote", "double_press")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance(10)
    r.h.event("event.bathroom_remote", "press")  # a later single press doesn't hide it
    assert r.new_calls() == []


REMOTE_RULE = """
light.desk := latest
    event.bathroom_remote.press        -> on(brightness: 10%)
    event.bathroom_remote.double_press -> on(brightness: 100%)
    else                               -> off
"""


def remote_pressed(h: FakeHost, event_type: str, ago: float) -> None:
    types = {"event_types": ["press", "long_press", "double_press"], "event_type": event_type}
    h.set_state("event.bathroom_remote", h.iso(h.t - ago), types, notify=False)


def test_latest_over_event_types_of_one_entity_at_load():
    """The entity's last event was a double press, so a single press, if any, came strictly
    before it: the double press wins (it used to be unknown until the next press)."""
    h = standard_world()
    remote_pressed(h, "double_press", ago=6 * H)
    r = Rig(REMOTE_RULE, host=h)
    assert r.new_calls() == ["light.turn_on light.desk brightness_pct=100"]
    # and after a restart, with the lamp turned off while HA was down
    r.restart(changes={"light.desk": ("off", {})})
    assert r.new_calls() == ["light.turn_on light.desk brightness_pct=100"]


def test_latest_over_event_types_of_one_entity_single_press_last():
    h = standard_world()
    remote_pressed(h, "press", ago=6 * H)
    r = Rig(REMOTE_RULE, host=h)
    assert r.new_calls() == ["light.turn_on light.desk brightness_pct=10"]


def test_latest_in_binding_without_else_keeps():
    """Issue #10: a `latest` binding without `else` leaves the device alone until one of its
    conditions has happened."""
    h = standard_world()
    h.set_state("light.desk", "on", notify=False)
    r = Rig(
        """
light.desk := latest
    input_button.desk_on       -> on
    input_button.desk_off      -> off
""",
        host=h,
    )
    assert r.new_calls() == []  # neither has been pressed: keep
    r.h.advance(1)
    r.h.press("input_button.desk_off")
    assert r.new_calls() == ["light.turn_off light.desk"]


def test_toggle_and_previous():
    r = Rig(
        """
switch.pump := toggle(input_button.boost, initial: off)
input_boolean.guest := previous(person.ann) == "away"
"""
    )
    for expect in (["switch.turn_on switch.pump"], ["switch.turn_off switch.pump"]):
        r.h.advance(1)
        r.h.press("input_button.boost")
        assert r.new_calls() == expect
    r.h.set_state("person.ann", "away")
    r.h.set_state("person.ann", "home")
    assert r.new_calls() == ["input_boolean.turn_on input_boolean.guest"]


# ------------------------------------------------------------ state machines (§13)

BATH = """
let remote = event.bathroom_remote
state bath_mode = "auto":
    fresh(remote.long_press)                    -> "off"
    fresh(remote.press)
        | bath_mode == "auto"                   -> "boost"
        | else                                  -> "auto"
    bath_mode == "boost" for 30min              -> "auto"
    bath_mode == "off" for 2h                   -> "auto"

fan.bathroom :=
    bath_mode == "boost"   -> on
    bath_mode == "off"     -> off
    else                   -> sensor.bathroom_co2 > 800
"""


def test_machine_transitions_and_timeouts():
    r = Rig(BATH)
    assert r.machine("bath_mode") == "auto"
    assert r.h.machines["bath_mode"][0] == "auto"
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "press")
    assert r.machine("bath_mode") == "boost"
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.advance(60)
    r.h.event("event.bathroom_remote", "press")  # pressing again cancels
    assert r.machine("bath_mode") == "auto"
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]
    r.h.event("event.bathroom_remote", "press")
    r.h.advance(30 * MIN)
    assert r.machine("bath_mode") == "auto"  # timeout
    r.h.event("event.bathroom_remote", "long_press")
    assert r.machine("bath_mode") == "off"
    r.h.advance(2 * H)
    assert r.machine("bath_mode") == "auto"


def test_fresh_is_relative_to_entering_the_state():
    r = Rig(
        """
state m = "a":
    m == "a" and fresh(input_button.goodnight) -> "b"
    m == "b" and fresh(input_button.boost)     -> "c"
"""
    )
    r.h.advance(1)
    r.h.press("input_button.boost")  # before entering b: not fresh later
    r.h.advance(1)
    r.h.press("input_button.goodnight")
    assert r.machine("m") == "b"
    r.h.advance(1)
    r.h.press("input_button.boost")
    assert r.machine("m") == "c"


def test_machine_chains_apply_in_one_step():
    r = Rig(
        """
state m = "a":
    m == "a" and switch.pump -> "b"
    m == "b" -> "c"
light.porch := m == "c"
"""
    )
    r.h.set_state("switch.pump", "on")
    assert r.machine("m") == "c"
    assert r.new_calls() == ["light.turn_on light.porch"]


def test_machine_loop_is_detected():
    r = Rig(
        """
state m = "a":
    m == "a" and switch.pump -> "b"
    m == "b" -> "a"
"""
    )
    r.h.set_state("switch.pump", "on")
    assert any("loops" in msg for level, msg in r.h.logs if level == "error")
    assert r.machine("m") == "b"


def test_glitch_free_snapshot():
    r = Rig(
        """
state m = "idle":
    fresh(sensor.lux > 800) and not fresh(sensor.lux >= 801) -> "glitch"
    fresh(sensor.lux > 800)                                  -> "ok"
"""
    )
    r.h.advance(1)
    r.h.set_state("sensor.lux", "900")
    assert r.machine("m") == "ok"


def test_select_entity_drives_machine():
    r = Rig(BATH)
    r.h.set_state("sensor.bathroom_co2", "900")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.advance(60)
    r.rt.set_machine("bath_mode", "off")
    assert r.machine("bath_mode") == "off"
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]
    r.h.advance(2 * H)
    assert r.machine("bath_mode") == "auto"  # age reset at the manual selection


def test_previous_state():
    r = Rig(
        """
state house = "home":
    house != "movie" and fresh(input_button.goodnight) -> "movie"
    house == "movie" and fresh(input_button.boost)     -> previous(house)
"""
    )
    r.rt.set_machine("house", "home")
    r.h.advance(1)
    r.h.press("input_button.goodnight")
    assert r.machine("house") == "movie"
    r.h.advance(1)
    r.h.press("input_button.boost")
    assert r.machine("house") == "home"


# ----------------------------------------------------------------- actions (§14)


def test_action_once_per_press_and_not_at_load():
    h = standard_world()
    h.set_state("input_button.doorbell", h.iso(h.t - 60), notify=False)
    r = Rig('on input_button.doorbell:\n    notify.mobile_app_phone(message: "ding")\n', host=h)
    assert r.new_calls() == []
    h.advance(1)
    h.press("input_button.doorbell")
    assert r.new_calls() == ["notify.mobile_app_phone message=ding"]
    h.advance(1)
    h.press("input_button.doorbell")
    assert r.new_calls() == ["notify.mobile_app_phone message=ding"]


def test_action_if_vs_and():
    rules = """
let night = now in 22:00..06:00
on person.ann == "home" if night:
    notify.mobile_app_phone(message: "if")
on person.ann == "home" and night:
    notify.mobile_app_phone(message: "and")
"""
    r = Rig(rules)
    r.h.set_state("person.ann", "away")
    r.h.advance(60)
    r.h.set_state("person.ann", "home")  # daytime arrival
    assert r.new_calls() == []
    r.h.advance_to(r.h.local(2026, 1, 5, 22, 0))  # night falls while she's home
    assert r.new_calls() == ["notify.mobile_app_phone message=and"]
    r.h.set_state("person.ann", "away")
    r.h.advance(60)
    r.h.set_state("person.ann", "home")  # arrives at night
    assert sorted(r.new_calls()) == [
        "notify.mobile_app_phone message=and",
        "notify.mobile_app_phone message=if",
    ]


def test_action_data_reads_signals():
    r = Rig(
        'on input_button.doorbell:\n    notify.mobile_app_phone(message: "co2", data: sensor.bathroom_co2)\n'
    )
    r.h.advance(1)
    r.h.press("input_button.doorbell")
    assert r.new_calls() == ["notify.mobile_app_phone data=500 message=co2"]


def test_bus_event_action():
    r = Rig('on bus.zha_event(command: "on"):\n    notify.mobile_app_phone(message: "zha")\n')
    r.h.advance(1)
    r.h.fire("zha_event", {"command": "off"})
    assert r.new_calls() == []
    r.h.fire("zha_event", {"command": "on", "device_ieee": "x"})
    assert r.new_calls() == ["notify.mobile_app_phone message=zha"]


def test_machine_entry_action():
    r = Rig(BATH + '\non bath_mode == "boost":\n    notify.mobile_app_phone(message: "boost")\n')
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "press")
    assert "notify.mobile_app_phone message=boost" in r.new_calls()


# ------------------------------------------------------------ startup (D16, D17)


def test_first_result_compared_with_device_state():
    h = standard_world()
    h.set_state("switch.pump", "on", notify=False)
    r = Rig("switch.pump := on", host=h)
    assert r.new_calls() == []
    r2 = Rig("light.porch := on")
    assert r2.new_calls() == ["light.turn_on light.porch"]


def test_brightness_read_back_tolerance():
    h = standard_world()
    h.set_state("light.hall", "on", {"brightness": 203}, notify=False)
    r = Rig("light.hall := on(brightness: 80%)", host=h)
    assert r.new_calls() == []  # 80% → 204, within tolerance


def test_restart_age_lower_bounds():
    r = Rig("switch.pump := age(binary_sensor.holiday) > 1h")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.set_state("switch.pump", "off")
    r.h.advance(10 * MIN)
    r.restart(downtime=60)
    assert r.rt.binding("switch.pump").last_known is NOTHING  # unknown right after boot
    r.h.advance(H - 1)
    assert r.new_calls() == []
    r.h.advance(1)
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_restart_motion_light_not_switched_on():
    r = Rig("light.hall := binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 10min")
    r.h.set_state("binary_sensor.hall_motion", "on")
    r.h.advance(60)
    r.h.set_state("binary_sensor.hall_motion", "off")
    r.h.advance(15 * MIN)
    r.new_calls()
    r.h.set_state("light.hall", "off")
    r.restart(downtime=30)
    assert r.new_calls() == []
    r.h.advance(10 * MIN)
    assert r.new_calls() == []  # settled to off, which matches


def test_restart_for_waits_full_duration():
    r = Rig("light.porch := sensor.lux < 20 for 10min")
    r.h.set_state("sensor.lux", "5")
    r.h.advance(20 * MIN)
    assert r.new_calls() == ["light.turn_on light.porch"]
    r.h.set_state("light.porch", "off", ours=False)  # e.g. a power cut
    r.restart(downtime=30)
    assert r.new_calls() == []
    r.h.advance(10 * MIN)
    assert r.new_calls() == ["light.turn_on light.porch"]


def test_restart_catches_up_time_based_results():
    r = Rig("cover.blinds.position := 100% if now in 07:00..20:00 else 0%")
    r.h.advance_to(r.h.local(2026, 1, 5, 21, 0))
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=0"]
    r.restart(downtime=11 * H)  # down over 07:00
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=100"]


def test_restart_machine_restored_and_missed_time_point_is_fresh():
    rules = """
state house = "home":
    house == "home" and fresh(input_button.goodnight) -> "night"
    house == "night" and fresh(07:00)                 -> "home"
"""
    r = Rig(rules)
    r.h.advance_to(r.h.local(2026, 1, 5, 23, 0))
    r.h.press("input_button.goodnight")
    assert r.machine("house") == "night"
    r.restart(downtime=9 * H)  # boots at 08:00, after 07:00
    assert r.machine("house") == "home"


def test_restart_machine_keeps_change_time():
    rules = BATH.replace(
        'bath_mode == "off" for 2h                   -> "auto"',
        'bath_mode == "off" and age(bath_mode) > 2h -> "auto"',
    )
    r = Rig(rules)
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "long_press")
    assert r.machine("bath_mode") == "off"
    r.h.advance(H)
    r.restart(downtime=10 * MIN)
    assert r.machine("bath_mode") == "off"
    r.h.advance(50 * MIN - 1)
    assert r.machine("bath_mode") == "off"
    r.h.advance(2)
    assert r.machine("bath_mode") == "auto"  # 2 h after the real entry, not after boot


def test_restart_for_on_machine_waits_full_duration():
    # `for` tracks when the expression changed, which isn't kept across restarts (D13b, §17.5)
    r = Rig(BATH)
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "long_press")
    r.h.advance(H)
    r.restart(downtime=10 * MIN)
    r.h.advance(2 * H - 1)
    assert r.machine("bath_mode") == "off"
    r.h.advance(1)
    assert r.machine("bath_mode") == "auto"


def test_restart_actions_dont_catch_up():
    r = Rig('on input_button.doorbell:\n    notify.mobile_app_phone(message: "ding")\n')
    r.rt.stop()
    r.h.advance(1)
    r.h.press("input_button.doorbell")  # while HA is down... (state restored at boot)
    r.restart(downtime=10)
    assert r.new_calls() == []


def test_restart_latest_unordered_is_unknown():
    r = Rig(
        """
switch.pump := latest
    binary_sensor.bathroom_window -> on
    binary_sensor.washer_door     -> off
    else                          -> keep
"""
    )
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance(1)
    r.h.set_state("binary_sensor.washer_door", "on")
    assert r.new_calls() == ["switch.turn_off switch.pump"]
    r.restart()
    assert r.rt.binding("switch.pump").last_known is NOTHING
    r.h.advance(1)
    r.h.set_state("binary_sensor.bathroom_window", "off")
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


# ---------------------------------------------------------------- reloads (D17.6)


def test_reload_unrelated_edit_leaves_devices_alone():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800")
    r.h.set_state("sensor.bathroom_co2", "900")
    r.new_calls()
    r.h.set_state("switch.pump", "off")  # manual
    r.reload("switch.pump := sensor.bathroom_co2 > 800\nlet unused = on\n")
    assert r.new_calls() == []


def test_reload_changed_rule_applies_immediately():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800")
    r.reload("switch.pump := sensor.bathroom_co2 > 400")
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_reload_keeps_machine_state_and_resets_removed_states():
    r = Rig(BATH)
    r.h.advance(1)
    r.h.event("event.bathroom_remote", "long_press")
    r.reload(BATH + "\nlet x = on\n")
    assert r.machine("bath_mode") == "off"
    r.reload(BATH.replace('"off"', '"quiet"'))
    assert r.machine("bath_mode") == "auto"
    assert any("no longer exists" in m for _, m in r.h.logs)


def test_reload_keeps_age_history():
    r = Rig("light.porch := sensor.lux < 20 for 10min")
    r.h.set_state("sensor.lux", "5")
    r.h.advance(5 * MIN)
    r.reload("light.porch := sensor.lux < 20 for 10min\n")
    r.h.advance(5 * MIN)
    assert r.new_calls() == ["light.turn_on light.porch"]


def test_reload_keeps_action_last_handled():
    src = 'on input_button.doorbell:\n    notify.mobile_app_phone(message: "ding")\n'
    r = Rig(src)
    r.h.advance(1)
    r.h.press("input_button.doorbell")
    r.new_calls()
    r.reload(src + "\nlet x = on\n")
    assert r.new_calls() == []


# ------------------------------------------------------------------ dry run (I20)


def test_dry_run_logs_instead_of_calling():
    r = Rig("# mode: dry_run\nlight.porch := on\n")
    assert r.new_calls() == []
    assert any("[dry run]" in m and "light.turn_on" in m for _, m in r.h.logs)


# ------------------------------------------------------------- full example (§21)


def test_full_example_scripted_day():
    import re
    from pathlib import Path

    src = (Path(__file__).parents[2] / "docs" / "design.md").read_text()
    full = re.findall(r"```python\n(# bathroom.homeo.*?)```", src, re.S)[0]
    r = Rig({"bathroom.homeo": full})
    h = r.h
    assert r.new_calls() == []
    # co2_high is unknown until co2 crosses 700 or 800 (no else in its latest block)
    h.set_state("sensor.bathroom_co2", "750")
    h.advance(1)
    h.set_state("sensor.bathroom_co2", "650")
    # humid shower: humidity > 70% for 2 min → stuffy → fan on
    h.advance(MIN)
    h.set_state("sensor.bathroom_humidity", "80")
    h.advance(2 * MIN)
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    # motion turns the light on at full brightness (daytime)
    h.set_state("binary_sensor.bathroom_motion", "on")
    assert r.new_calls() == ["light.turn_on light.bathroom brightness_pct=100"]
    h.set_state("binary_sensor.bathroom_motion", "off")
    h.advance(5 * MIN)
    assert r.new_calls() == ["light.turn_off light.bathroom"]
    # humidity drops: fan off; a remote press boosts for 30 min
    h.set_state("sensor.bathroom_humidity", "50")
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]
    h.event("event.bathroom_remote", "press")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    h.advance(30 * MIN)
    assert r.new_calls() == ["fan.turn_off fan.bathroom"]
    # very humid for 30 min → one notification
    h.set_state("sensor.bathroom_humidity", "95")
    h.advance(30 * MIN)
    calls = r.new_calls()
    assert (
        calls.count("notify.mobile_app_phone message=Bathroom still very humid. Window open?") == 1
    )


def test_restart_overrides_pre_restart_manual_change():
    # D17 rule 3, accepted cost: manual() history isn't kept, so the rule takes over again.
    r = Rig("fan.bathroom :=\n    age(manual(fan.bathroom)) < 1h -> keep\n    else -> on\n")
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]
    r.h.advance(60)
    r.h.set_state("fan.bathroom", "off")  # by hand
    assert r.new_calls() == []
    r.restart(downtime=60)
    assert r.new_calls() == ["fan.turn_on fan.bathroom"]


def test_change_since_boot_keeps_exact_time():
    h = standard_world()
    h.states["binary_sensor.hall_motion"] = EntityState(
        "binary_sensor.hall_motion", "off", {}, h.t - 120, h.t - 120
    )
    r = Rig(
        "light.hall := binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 10min", host=h
    )
    assert r.new_calls() == ["light.turn_on light.hall"]  # motion ended 2 min ago
    h.advance(8 * MIN)
    assert r.new_calls() == ["light.turn_off light.hall"]


def test_device_reconnecting_after_boot_is_not_a_change():
    r = Rig("light.hall := binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 10min")
    r.h.set_state("binary_sensor.hall_motion", "on")
    r.h.advance(60)
    r.h.set_state("binary_sensor.hall_motion", "off")
    r.h.advance(15 * MIN)
    r.new_calls()
    r.restart(downtime=30, changes={"binary_sensor.hall_motion": ("unavailable", {})})
    r.h.advance(20)
    r.h.set_state("binary_sensor.hall_motion", "off")  # integration reconnects
    r.h.advance(10 * MIN)
    assert r.new_calls() == []


# ------------------------------------------------------------ review fixes (PR #1)


def test_write_while_offline_catches_up_when_device_returns():
    r = Rig("switch.pump := binary_sensor.bathroom_window")
    r.h.simulate_devices = False
    r.h.set_state("switch.pump", "unavailable")
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["switch.turn_on switch.pump"]  # sent, but the plug is offline
    r.h.advance(60)
    r.h.set_state("switch.pump", "off")  # back online, still off
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_slow_cover_progress_is_not_manual():
    r = Rig(
        """
cover.blinds.position :=
    age(manual(cover.blinds)) < 1h -> keep
    binary_sensor.bathroom_window  -> 30%
    else                           -> 100%
"""
    )
    r.h.simulate_devices = False
    r.h.set_state("binary_sensor.bathroom_window", "on")
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=30"]
    r.h.advance(8)  # later reports arrive with a fresh context
    r.h.set_state("cover.blinds", "closing", {"current_position": 60})
    r.h.advance(8)
    r.h.set_state("cover.blinds", "open", {"current_position": 30})
    assert r.rt.cells["manual:cover.blinds"].extra["t"].hi == float("-inf")
    r.h.set_state("binary_sensor.bathroom_window", "off")
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=100"]


def test_manual_change_detected_per_binding():
    r = Rig(
        """
let m = age(manual(climate.living)) < 1h
climate.living := "off" if m else "heat"
climate.living.temperature := 20
"""
    )
    r.h.set_state("climate.living", "off", merge=True)  # mode binding writes "heat" back...
    r.h.advance(2 * H)
    r.new_calls()
    t = r.rt.cells["manual:climate.living"].extra["t"]
    r.h.set_state("climate.living", "heat", {"temperature": 23}, merge=True)  # setpoint by hand
    assert r.rt.cells["manual:climate.living"].extra["t"] != t


def test_today_has_a_value():
    r = Rig("switch.pump := age(today) < 1h")  # the first hour of each day
    r.h.advance_to(r.h.local(2026, 1, 6, 0, 0))
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance_to(r.h.local(2026, 1, 6, 1, 0))
    assert r.new_calls() == ["switch.turn_off switch.pump"]


# --------------------------------------------------------------- sun.sun (I5)


def test_night_range_follows_sun_entity():
    r = Rig("switch.pump := now in sunset..sunrise")  # fake sun: sunrise 07:00, sunset 19:00
    r.h.advance_to(r.h.local(2026, 1, 5, 18, 59))
    assert r.new_calls() == []
    r.h.advance_to(r.h.local(2026, 1, 5, 19, 0))
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.advance_to(r.h.local(2026, 1, 6, 7, 0))
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_positive_offset_after_sun_entity_moved_on():
    # sun.sun shows tomorrow's sunset from 19:00, but sunset + 30min is still due today
    r = Rig('on sunset + 30min:\n    notify.mobile_app_phone(message: "x")\n')
    r.h.advance_to(r.h.local(2026, 1, 5, 19, 29))
    assert r.new_calls() == []
    r.h.advance_to(r.h.local(2026, 1, 5, 19, 30))
    assert r.new_calls() == ["notify.mobile_app_phone message=x"]


def test_sun_unavailable_is_unknown():
    r = Rig("switch.pump := now in sunset..sunrise")
    r.h.stop_sun()
    r.h.advance_to(r.h.local(2026, 1, 5, 20, 0))
    assert r.new_calls() == []  # unknown: the pump is left alone
    r.h.start_sun()
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_elevation_updates_dont_count_as_changes():
    r = Rig("switch.pump := now in sunset..sunrise")
    sun = r.rt.cells["sun"]
    before = (sun.value, sun.ct)
    r.h.advance(60)
    r.h.set_state("sun.sun", r.state("sun.sun"), {"elevation": 12.5}, merge=True)
    assert (sun.value, sun.ct) == before


def test_restart_estimates_missed_sunset():
    rules = """
state house = "day":
    house == "day" and fresh(sunset) -> "evening"
on sunset:
    notify.mobile_app_phone(message: "sunset")
"""
    r = Rig(rules)
    r.h.advance_to(r.h.local(2026, 1, 5, 18, 0))
    r.restart(downtime=2 * H)  # down over sunset (19:00)
    assert r.machine("house") == "evening"  # the missed sunset is still fresh (D17 rule 5)
    assert r.new_calls() == []  # actions don't catch up (D17 rule 4)


# ------------------------------------------------------------- percentages (D4)


def test_volume_is_compared_as_a_percentage():
    # HA reports volume from 0 to 1, so 0.2 is 20%: it used to be compared with 30 as is.
    r = Rig("switch.pump := media_player.tv.volume_level > 30%")
    assert r.new_calls() == []
    r.h.set_state("media_player.tv", "on", {"volume_level": 0.4})
    assert r.new_calls() == ["switch.turn_on switch.pump"]


def test_brightness_is_compared_as_a_percentage():
    r = Rig("switch.pump := light.hall.brightness > 40%")
    r.h.set_state("light.hall", "on", {"brightness": 128})  # 50%
    assert r.new_calls() == ["switch.turn_on switch.pump"]
    r.h.set_state("light.hall", "on", {"brightness": 77})  # 30%
    assert r.new_calls() == ["switch.turn_off switch.pump"]


def test_explain_shows_a_scaled_percentage_rounded():
    from custom_components.homeo.core.explain import explain

    r = Rig("switch.pump := light.hall.brightness > 40%")
    r.h.set_state("light.hall", "on", {"brightness": 128})
    (e,) = explain(r.rt, "switch.pump")
    assert e["values"] == {"light.hall.brightness": "50.2%"}
