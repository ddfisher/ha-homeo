"""Behaviors: binding results that move with the clock (D23)."""

from custom_components.homeo.core.explain import explain

from .fakehost import standard_world
from .rig import Rig

MIN = 60
H = 3600

DIM = """
light.hall :=
    now in 19:00..23:00     -> on(brightness: 100% - 96% * (age(19:00) / 4h))
    now in 23:00..04:00     -> on(brightness: 4%)
    else                    -> keep
"""


def world(transition: bool = True, **states):
    h = standard_world()
    attrs = {"supported_features": 32} if transition else {}
    h.set_state("light.hall", "off", attrs, notify=False)
    for eid, (state, a) in states.items():
        h.set_state(eid, state, a, notify=False)
    return h


def at(r: Rig, hh: int, mm: int = 0, ss: int = 0) -> None:
    r.h.advance_to(r.h.local(2026, 1, 5, hh, mm, ss))


# ------------------------------------------------------------------ transitions


def test_ramp_follows_with_transitions_capped_at_5min():
    r = Rig(DIM, host=world())
    assert r.new_calls() == []  # keep during the day
    at(r, 19)
    # The light is off, so it's set to the start value first, then fades for 5 minutes.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=100",
        "light.turn_on light.hall brightness_pct=98 transition=300",
    ]
    at(r, 19, 4, 59)
    assert r.new_calls() == []
    at(r, 19, 5)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=96 transition=300"]
    at(r, 21)
    assert len(r.new_calls()) == 23  # one piece every 5 minutes
    assert r.h.call_times[-1] == r.h.local(2026, 1, 5, 21)


def test_ramp_ends_exactly_at_the_change_of_shape():
    r = Rig(DIM, host=world())
    at(r, 22, 57)
    r.new_calls()
    at(r, 22, 58)  # still in the piece that started at 22:55
    assert r.new_calls() == []
    at(r, 23)
    # The 22:55 piece already ended at 4% (at 23:00), so the constant arm writes nothing.
    assert r.new_calls() == []
    assert r.state("light.hall") == "on"
    assert r.h.states["light.hall"].attributes["brightness"] == round(255 * 0.04)
    at(r, 23, 30)
    assert r.new_calls() == []


def test_short_last_piece_ends_at_the_change_of_shape():
    rule = DIM.replace("19:00..23:00", "19:00..23:02").replace("23:00..04:00", "23:02..04:00")
    r = Rig(rule, host=world())
    at(r, 23)
    r.new_calls()
    at(r, 23, 0, 1)
    assert r.new_calls() == []
    at(r, 23, 2)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=4"]  # the ramp jumps


def test_first_piece_without_jump_when_device_matches():
    h = world(**{"light.hall": ("on", {"supported_features": 32, "brightness": 255})})
    r = Rig(DIM, host=h)
    at(r, 19)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=98 transition=300"]


def test_unrelated_reevaluation_mid_piece_writes_nothing():
    rule = """
light.hall :=
    sensor.bathroom_co2 > 2000  -> off
    now in 19:00..23:00         -> on(brightness: 100% - 96% * (age(19:00) / 4h))
    else                        -> keep
"""
    r = Rig(rule, host=world())
    at(r, 19)
    r.new_calls()
    r.h.advance(60)
    r.h.set_state("sensor.bathroom_co2", "900")
    r.h.advance(60)
    r.h.set_state("sensor.bathroom_co2", "950")
    assert r.new_calls() == []
    r.h.advance(3 * 60)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=96 transition=300"]
    r.h.set_state("sensor.bathroom_co2", "2500")
    assert r.new_calls() == ["light.turn_off light.hall"]
    r.h.advance(60)
    r.h.set_state("sensor.bathroom_co2", "500")
    # Back to the ramp, which has moved on: set the current value, then fade.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=97.6",
        "light.turn_on light.hall brightness_pct=95.6 transition=300",
    ]


def test_slope_change_is_a_new_shape():
    rule = """
light.hall :=
    input_boolean.guest_mode    -> on(brightness: 100% - 1% * (age(19:00) / 1min))
    now in 19:00..23:00         -> on(brightness: 100% - 96% * (age(19:00) / 4h))
    else                        -> keep
"""
    r = Rig(rule, host=world(**{"input_boolean.guest_mode": ("off", {})}))
    at(r, 19)
    r.new_calls()
    at(r, 19, 10)
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=96 transition=300",
        "light.turn_on light.hall brightness_pct=94 transition=300",
    ]
    r.h.set_state("input_boolean.guest_mode", "on")
    # A different line: 90% now, falling 1%/min. The light is on its way to 94%, not at 90%.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=90",
        "light.turn_on light.hall brightness_pct=85 transition=300",
    ]


# ------------------------------------------------------------------------ steps


def test_ramp_without_transitions_steps_by_resolution():
    r = Rig(DIM, host=world(transition=False))
    at(r, 19)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=100"]
    # 96% over 4h is 0.4%/min, so a 1% step every 2.5 minutes.
    at(r, 19, 2, 29)
    assert r.new_calls() == []
    at(r, 19, 2, 30)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=99"]
    at(r, 19, 5)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=98"]
    at(r, 22, 59)
    r.new_calls()
    at(r, 23)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=4"]


def test_fast_ramp_steps_at_most_every_5s():
    rule = """
cover.blinds.position :=
    age(input_button.boost) < 100s     -> 100% - 1% * (age(input_button.boost) / 1s)
    else                                -> keep
"""
    r = Rig(rule, host=world(**{"cover.blinds": ("open", {"current_position": 100})}))
    r.h.advance(10)
    r.h.press("input_button.boost")
    assert r.new_calls() == []  # already at 100
    r.h.advance(5)
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=95"]
    r.h.advance(5)
    assert r.new_calls() == ["cover.set_cover_position cover.blinds position=90"]


def test_stepping_uses_the_entity_step():
    rule = "climate.living.temperature := 18 + 2 * (age(06:00) / 1h) if now in 06:00..07:00 else 18"
    h = world(**{"climate.living": ("heat", {"temperature": 18, "target_temp_step": 0.5})})
    r = Rig(rule, host=h)
    r.h.advance_to(r.h.local(2026, 1, 6, 6))
    assert r.new_calls() == []  # already at 18
    r.h.advance(15 * MIN)
    assert r.new_calls() == ["climate.set_temperature climate.living temperature=18.5"]
    r.h.advance(15 * MIN)
    assert r.new_calls() == ["climate.set_temperature climate.living temperature=19"]


# ------------------------------------------------------------- manual (D15, D21)


def test_manual_change_stops_following_until_shape_changes():
    r = Rig(DIM, host=world())
    at(r, 20)
    r.new_calls()
    r.h.set_state("light.hall", "on", {"supported_features": 32, "brightness": 255})  # by hand
    at(r, 22, 59)
    assert r.new_calls() == []  # no fight
    at(r, 23)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=4"]  # the result changed


def test_in_between_reports_mid_transition_are_not_manual():
    h = world()
    h.simulate_devices = False
    rule = """
light.hall :=
    age(manual(light.hall)) < 1h -> keep
    now in 19:00..23:00     -> on(brightness: 100% - 96% * (age(19:00) / 4h))
    else                    -> keep
"""
    r = Rig(rule, host=h)
    h.advance(2 * H)  # get past the post-load uncertainty of manual()
    at(r, 19)
    r.new_calls()
    # A slow light reports in-between brightness, with a fresh context, mid-transition.
    for k, b in enumerate((254, 252, 251, 250)):
        h.advance(60 if k else 10)
        h.set_state("light.hall", "on", {"supported_features": 32, "brightness": b})
    at(r, 19, 5)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=96 transition=300"]
    h.advance(10)
    # A late report from the first piece still counts as ours.
    h.set_state("light.hall", "on", {"supported_features": 32, "brightness": 252})
    h.advance(20)
    h.set_state("light.hall", "on", {"supported_features": 32, "brightness": 128})  # by hand
    at(r, 19, 10)
    assert r.new_calls() == []  # yields for an hour


# ------------------------------------------------------- unknown, restarts, reloads


def test_inexact_behavior_is_unknown():
    r = Rig("input_number.target := age(sensor.lux) / 1min", host=world())
    r.new_calls()
    r.restart()
    r.h.advance(60)
    assert r.new_calls() == []  # sensor.lux changed some time before the restart
    r.h.set_state("sensor.lux", "50")
    assert r.new_calls() == ["input_number.set_value input_number.target value=0"]
    r.h.advance(60)  # one step of the default resolution (1) per minute
    assert r.new_calls() == ["input_number.set_value input_number.target value=1"]


def test_restart_resumes_following():
    r = Rig(DIM, host=world())
    at(r, 20)
    r.new_calls()
    r.restart(downtime=5 * MIN)  # back at 20:05, where the 20:00 piece took the light
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=72 transition=300"]


def test_reload_mid_piece_writes_nothing():
    r = Rig(DIM, host=world())
    at(r, 20, 1)
    r.new_calls()
    r.reload(DIM + "\nswitch.pump := off\n")
    assert r.new_calls() == []
    at(r, 20, 5)
    assert r.new_calls() == ["light.turn_on light.hall brightness_pct=72 transition=300"]


def test_keep_interrupts_following():
    rule = """
light.hall :=
    input_boolean.guest_mode    -> keep
    now in 19:00..23:00         -> on(brightness: 100% - 96% * (age(19:00) / 4h))
    else                        -> keep
"""
    r = Rig(rule, host=world(**{"input_boolean.guest_mode": ("off", {})}))
    at(r, 19)
    r.new_calls()
    r.h.set_state("input_boolean.guest_mode", "on")
    at(r, 20)
    assert r.new_calls() == []
    r.h.set_state("input_boolean.guest_mode", "off")
    # keep ended: the device (at 98%, where the 19:00 piece took it) stands in.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=76",
        "light.turn_on light.hall brightness_pct=74 transition=300",
    ]


def test_dry_run_logs_pieces():
    r = Rig("# mode: dry_run\n" + DIM, host=world())
    at(r, 19, 5)
    assert r.new_calls() == []
    assert sum("would call light.turn_on" in m for _, m in r.h.logs) == 3


def test_kelvin_and_brightness_follow_together():
    rule = """
light.hall := on(brightness: 50%, kelvin: 4000 - 1000 * (age(19:00) / 1h)) if now in 19:00..20:00 else keep
"""
    r = Rig(rule, host=world())
    at(r, 19)
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=50 color_temp_kelvin=4000",
        "light.turn_on light.hall brightness_pct=50 color_temp_kelvin=3917 transition=300",
    ]


# ---------------------------------------------------------------------- explain


def test_explain_shows_rate_and_piece():
    r = Rig(DIM, host=world())
    at(r, 20)
    (e,) = explain(r.rt, "light.hall")
    assert e["result"] == "on(brightness: 76%, falling 24%/h)"
    assert e["following"].startswith("following it; the current piece ends at 20:05:00")
    assert e["device_matches"] is True
    r.h.set_state("light.hall", "on", {"supported_features": 32, "brightness": 255})
    (e,) = explain(r.rt, "light.hall")
    assert e["following"] == "stopped by a manual change, until the result changes shape"


# ------------------------------------------------------------------ review fixes


def test_explain_of_inexact_behavior_is_unknown():
    r = Rig("light.hall := on(brightness: 100% - 1% * (age(sensor.lux) / 1min))", host=world())
    r.restart()
    r.h.advance(60)
    (e,) = explain(r.rt, "light.hall")
    assert e["result"] == "unknown"
    assert "device_matches" not in e


def test_piece_ends_at_a_cell_wake_up():
    h = world()
    h.advance_to(h.local(2026, 1, 5, 18, 57, 30))
    r = Rig("light.hall := on(brightness: 50% * (age(every 1h) / 1h))", host=h)
    # `every 1h` ticks at 19:00, where age() starts again: the piece ends there.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=47.92",
        "light.turn_on light.hall brightness_pct=50 transition=150",
    ]


def test_lapsed_piece_starts_again_from_the_device():
    rule = """
light.hall := on(brightness: 100% - 96% * (age(19:00) / 4h)) if sensor.lux < 500 else keep
"""
    h = world()
    h.advance_to(h.local(2026, 1, 5, 18, 59))
    r = Rig(rule, host=h)
    at(r, 19)
    r.new_calls()
    at(r, 19, 1)
    r.h.set_state("sensor.lux", "unavailable")
    at(r, 20)
    assert r.new_calls() == []
    r.h.set_state("sensor.lux", "100")
    # The light stopped at 98% (the 19:00 piece); the ramp is at 76% now.
    assert r.new_calls() == [
        "light.turn_on light.hall brightness_pct=76",
        "light.turn_on light.hall brightness_pct=74 transition=300",
    ]
    sink = r.rt.binding("light.hall")
    assert sink.last_written.get("brightness").lo == 74  # only this piece counts as ours


def test_coalesce_falls_back_for_inexact_behavior():
    r = Rig("input_number.target := (age(sensor.lux) / 1min) ?? 7", host=world())
    r.new_calls()
    r.restart()
    assert r.new_calls() == ["input_number.set_value input_number.target value=7"]


def test_values_are_clamped_to_the_setting_range():
    rule = """
light.hall :=
    now in 19:00..23:00     -> on(brightness: 100% - 96% * (age(19:00) / 1h))
    else                    -> keep
"""
    r = Rig(rule, host=world())
    at(r, 20)
    # The line reaches 0% at 20:02:30, so the piece ends there, and nothing more is sent.
    assert r.new_calls()[-1] == "light.turn_on light.hall brightness_pct=0 transition=150"
    at(r, 22)
    assert r.new_calls() == []


def test_numbers_are_clamped_to_min_and_max():
    h = world(**{"input_number.target": ("50", {"min": 0, "max": 60, "step": 1})})
    r = Rig("input_number.target := 50 + age(19:00) / 1min", host=h)
    at(r, 19, 30)
    assert r.new_calls()[-1] == "input_number.set_value input_number.target value=60"
    at(r, 21)
    assert r.new_calls() == []


def test_steps_are_written_at_least_once_a_day():
    rule = "input_number.target := 5 + (age(19:00) / 1h) * 0.000000000001"
    r = Rig(rule, host=world())
    assert max(r.h.pending_timers()) <= r.h.t + 86400


def test_fixed_values_are_clamped_too():
    h = world(
        **{
            "input_number.target": ("50", {"min": 0, "max": 100, "step": 1}),
            "climate.living": ("heat", {"temperature": 20, "min_temp": 7, "max_temp": 25}),
        }
    )
    r = Rig("input_number.target := 500\nclimate.living.temperature := 30\n", host=h)
    assert sorted(r.new_calls()) == [
        "climate.set_temperature climate.living temperature=25",
        "input_number.set_value input_number.target value=100",
    ]
