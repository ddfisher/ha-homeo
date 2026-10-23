"""homeo.explain (§19): explanations of bindings, state machines and lets."""

import pytest

from custom_components.homeo.core.explain import NotFound, explain

from .rig import Rig

MIN = 60

FAN = """
let co2    = sensor.bathroom_co2
let stuffy = co2 > 800 or sensor.bathroom_humidity > 70% for 2min

fan.bathroom :=
    age(manual(fan.bathroom)) < 1h     -> keep
    age(input_button.boost) < 30min    -> on
    binary_sensor.bathroom_window      -> off
    else                               -> stuffy
"""


def one(rig: Rig, query: str) -> dict:
    (out,) = explain(rig.rt, query)
    return out


def test_cascade_binding():
    r = Rig(FAN)
    r.h.set_state("sensor.bathroom_humidity", "80")
    r.h.advance(3 * MIN)
    e = one(r, "fan.bathroom")
    assert e["binding"] == "fan.bathroom"
    assert e["rule"] == "test.homeo:5"
    assert e["result"] == "on"
    assert e["summary"] == (
        "fan.bathroom should be on, because no other arm applies (`else`, line 9)"
    )
    assert e["device"] == "on"
    assert e["device_matches"] is True
    arms = e["arms"]
    assert [a["arm"] for a in arms] == [
        "age(manual(fan.bathroom)) < 1h -> keep",
        "age(input_button.boost) < 30min -> on",
        "binary_sensor.bathroom_window -> off",
        "else -> stuffy",  # as written, with the let's name
    ]
    assert [a.get("condition") for a in arms] == [False, False, False, None]
    assert [a.get("active", False) for a in arms] == [False, False, False, True]
    assert arms[0]["values"] == {
        "age(manual(fan.bathroom))": "never happened",
        "manual(fan.bathroom)": "never",
    }
    # the else arm's result, with `for` expanded and let names kept
    assert arms[3]["result"] == "on"
    values = arms[3]["values"]
    assert values["co2 (sensor.bathroom_co2)"] == 500
    assert values["sensor.bathroom_humidity > 70% for 2min"] is True
    assert values["age(sensor.bathroom_humidity > 70%) >= 2min"] is True
    assert values["age(sensor.bathroom_humidity > 70%)"] == "3min"


def test_active_arm_and_ages():
    r = Rig(FAN)
    r.h.advance(MIN)
    r.h.press("input_button.boost")
    r.h.advance(12 * MIN)
    e = one(r, "fan.bathroom")
    assert e["summary"] == (
        "fan.bathroom should be on, because `age(input_button.boost) < 30min` (line 7)"
    )
    boost = e["arms"][1]
    assert boost["active"] is True
    assert boost["values"]["age(input_button.boost)"] == "12min"
    assert boost["values"]["input_button.boost"] == "08:01:00 (12min ago)"


def test_keep_and_manual_override():
    r = Rig(FAN)
    r.h.advance(MIN)
    r.h.set_state("fan.bathroom", "on")  # by hand
    r.h.advance(5 * MIN)
    e = one(r, "fan.bathroom")
    assert e["result"] == "keep"
    assert e["summary"] == (
        "fan.bathroom is left as it is (`keep`), because `age(manual(fan.bathroom)) < 1h` (line 6)"
    )
    assert "device_matches" not in e
    assert e["arms"][0]["values"]["manual(fan.bathroom)"] == "08:01:00 (5min ago)"


def test_unknown_result():
    r = Rig("switch.pump := sensor.bathroom_co2 > 800\n")
    r.h.set_state("sensor.bathroom_co2", "unavailable")
    e = one(r, "switch.pump")
    assert e["result"] == "unknown"
    assert e["summary"].startswith("switch.pump is left as it is: the result is unknown")
    assert e["values"] == {"sensor.bathroom_co2": "unknown"}


def test_light_settings_and_percentages():
    r = Rig("light.hall := on(brightness: 30% if now in 22:00..06:00 else 80%, kelvin: 2700)\n")
    e = one(r, "light.hall")
    assert e["result"] == "on(brightness: 80%, kelvin: 2700)"
    assert e["device"] == "on (brightness: 204, color_temp_kelvin: 2700)"
    assert e["values"]["30% if now in 22:00..06:00 else 80%"] == "80%"
    assert e["values"]["now in 22:00..06:00"] is False


def test_latest_let():
    src = """
let co2_high = latest
    sensor.bathroom_co2 > 800  -> true
    sensor.bathroom_co2 < 700  -> false
fan.bathroom := co2_high ?? false
"""
    r = Rig(src)
    r.h.advance(MIN)
    r.h.set_state("sensor.bathroom_co2", "900")
    r.h.advance(MIN)
    e = one(r, "co2_high")
    assert e["let"] == "co2_high" and e["file"] == "test.homeo"
    assert e["value"] is True
    high, low = e["latest"]
    assert high["happened"] == "08:01:00 (1min ago)"
    assert high["active"] is True
    assert low["happened"] == "never, or at or before 08:00:00 (2min ago)"
    assert "active" not in low


def test_machine():
    src = """
state mode = "auto":
    mode == "auto"
        | fresh(input_button.boost)    -> "boost"
    mode == "boost" for 30min          -> "auto"
"""
    r = Rig(src)
    r.h.advance(MIN)
    r.h.press("input_button.boost")
    r.h.advance(10 * MIN)
    for query in ("mode", "select.homeo_mode"):
        e = one(r, query)
        assert e["machine"] == "mode"
        assert e["entity"] == "select.homeo_mode"
        assert e["state"] == "boost"
        assert e["previous"] == "auto"
        assert e["since"] == "08:01:00 (10min ago)"
        assert e["summary"] == 'mode is "boost" since 08:01:00 (10min ago); no arm applies now'
        group, timeout = e["arms"]
        assert group["guard"] == 'mode == "auto"' and group["condition"] is False
        assert group["arms"][0]["arm"] == 'fresh(input_button.boost) -> "boost"'
        assert timeout["values"]['age(mode == "boost")'] == "10min"


def test_several_bindings_on_one_entity():
    src = 'climate.living := "heat"\nclimate.living.temperature := 21\n'
    r = Rig(src)
    keys = [e["binding"] for e in explain(r.rt, "climate.living")]
    assert keys == ["climate.living", "climate.living.temperature"]
    assert [e["binding"] for e in explain(r.rt, "climate.living.temperature")] == [
        "climate.living.temperature"
    ]


def test_not_found_suggests():
    r = Rig(FAN)
    with pytest.raises(NotFound, match="did you mean `fan.bathroom`"):
        explain(r.rt, "fan.bathrom")


def test_after_restart_shows_lower_bounds():
    r = Rig("light.hall := binary_sensor.hall_motion or age(binary_sensor.hall_motion) < 10min\n")
    r.restart(downtime=60)
    r.h.advance(3 * MIN)
    e = one(r, "light.hall")
    assert e["result"] == "unknown"
    assert e["values"]["age(binary_sensor.hall_motion)"] == "at least 3min"


def test_explaining_changes_nothing():
    def run(with_explain: bool) -> list[str]:
        r = Rig(FAN)
        r.h.press("input_button.boost")
        for _ in range(8):
            r.h.advance(5 * MIN)
            if with_explain:
                explain(r.rt, "fan.bathroom")
        r.h.set_state("sensor.bathroom_co2", "900")
        r.h.advance(MIN)
        return r.new_calls()

    assert run(True) == run(False)
