"""Run compiler output in plain Python against a fake Home Assistant.

Stubs pyscript's decorators and the small runtime the generated code imports,
then replays a sequence of state changes. Shows the generated code can be
tested without a real HA instance.
"""

import sys
import types

from compile import compile_source

states, calls, triggers = {}, [], []


class Unknown(Exception):
    pass


def _val(eid):
    v = states.get(eid, "unknown")
    if v in ("unknown", "unavailable"):
        raise Unknown(eid)
    if v in ("on", "off"):
        return v == "on"
    try:
        return float(v)
    except ValueError:
        return v


def _write(eid, value):
    calls.append(f"{'turn_on' if value else 'turn_off'}({eid})")
    states[eid] = "on" if value else "off"


runtime = types.ModuleType("ha_dsl_runtime")
runtime._val, runtime._write, runtime.Unknown = _val, _write, Unknown
sys.modules["ha_dsl_runtime"] = runtime


def state_trigger(*deps):
    def deco(fn):
        triggers.append((set(deps), fn))
        return fn
    return deco


def time_trigger(_when):
    return lambda fn: fn


def set_state(eid, value):
    states[eid] = value
    for deps, fn in triggers:
        if eid in deps:
            fn()


src = "fan.bathroom = sensor.co2 > 800 or sensor.humidity > 50\n"
code = compile_source(src)
print(code)
exec(code, {"state_trigger": state_trigger, "time_trigger": time_trigger})

steps = [
    ("sensor.co2", "unavailable"),  # unknown input -> no action
    ("sensor.co2", "600"),
    ("sensor.humidity", "40"),      # both known, false -> turn_off
    ("sensor.co2", "900"),          # -> turn_on
    ("fan.bathroom", "off"),        # human turns fan off by hand
    ("sensor.co2", "950"),          # still true: respect the manual override
    ("sensor.co2", "700"),          # false again -> turn_off
    ("sensor.humidity", "65"),      # -> turn_on
]
print("--- simulation ---")
for eid, v in steps:
    before = len(calls)
    set_state(eid, v)
    print(f"{eid:>16} = {v:<12} -> {', '.join(calls[before:]) or '(nothing)'}")
