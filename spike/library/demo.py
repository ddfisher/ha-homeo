"""Run example rules against a fake HA with a simulated clock, and check
that common mistakes are rejected when the rules are loaded."""

import heapq
from datetime import datetime, timedelta

import ha_dsl
from ha_dsl import *


class FakeHost:
    def __init__(self):
        self.states, self.log, self.timers = {}, [], []
        self.clock = datetime(2026, 1, 1, 12, 0)
        self.rt = None

    def now(self):
        return self.clock

    def read(self, entity_id, attr):
        v = self.states.get(entity_id if attr is None else f"{entity_id}#{attr}", "unknown")
        if v in ("unknown", "unavailable"):
            return UNKNOWN
        if v in ("on", "off"):
            return v == "on"
        try:
            return float(v)
        except (TypeError, ValueError):
            return v

    def write(self, entity_id, attr, value):
        if attr is None:
            self.states[entity_id] = "on" if value else "off"
            self.log.append(f"{'turn_on' if value else 'turn_off'}({entity_id})")
        else:
            self.states[f"{entity_id}#{attr}"] = value
            self.log.append(f"set({entity_id}.{attr}={value})")

    def schedule(self, when, cb):
        heapq.heappush(self.timers, (when, id(cb), cb))

    def set(self, entity_id, value):
        self.states[entity_id] = value
        self.rt.on_change(entity_id)

    def advance(self, delta):
        end = self.clock + delta
        while self.timers and self.timers[0][0] <= end:
            when, _, cb = heapq.heappop(self.timers)
            self.clock = max(self.clock, when)
            cb()
        self.clock = end


# ------------------------------------------------------------ the rules

fan.bathroom = (sensor.co2 > 800) | (sensor.humidity > 50 * percent)

fan.bedroom = (sensor.co2 > 800).until(sensor.co2 < 700)       # deadband

light.porch = (sensor.lux < 20).for_(5 * minutes)              # held for 5 min

with debounce(2 * minutes):
    switch.dehumidifier = sensor.humidity > 60

light.hall.brightness = when(binary_sensor.motion, 255, 0)     # attribute target


# ------------------------------------------------------------ scenario

host = FakeHost()
host.rt = rt = ha_dsl.Runtime(host)
rt.start()

steps = [
    ("set", "sensor.co2", "unavailable"),
    ("set", "sensor.humidity", "65"),     # bathroom: True | UNKNOWN = True
    ("set", "sensor.co2", "750"),         # bedroom: in deadband at startup -> off
    ("set", "sensor.co2", "850"),         # bedroom on
    ("set", "fan.bathroom", "off"),       # human override
    ("set", "sensor.co2", "750"),         # bedroom stays on (deadband); bathroom: still True, no write
    ("set", "sensor.co2", "650"),         # bedroom off
    ("set", "sensor.lux", "10"),          # porch: timer starts
    ("advance", 3 * minutes),             # dehumidifier: debounce elapsed at 2 min
    ("advance", 3 * minutes),             # porch: on at 5 min
    ("set", "binary_sensor.motion", "on"),
]
for step in steps:
    before = len(host.log)
    if step[0] == "set":
        host.set(step[1], step[2])
        label = f"{step[1]} = {step[2]}"
    else:
        host.advance(step[1])
        label = f"+{step[1]}"
    print(f"{host.clock:%H:%M}  {label:<28} -> {', '.join(host.log[before:]) or '(nothing)'}")


# ------------------------------------------------------------ mistakes caught at load time

print("\n--- mistakes ---")
mistakes = {
    "or instead of |": lambda: setattr(fan, "x", (sensor.co2 > 800) or (sensor.humidity > 50)),
    "missing parens (chained)": lambda: setattr(fan, "x", sensor.co2 > 800 | sensor.humidity > 50),
    "missing parens (num | bool)": lambda: setattr(fan, "x", binary_sensor.motion | sensor.lux < 20),
    "not instead of ~": lambda: setattr(fan, "x", not binary_sensor.motion),
    "if/else instead of when()": lambda: setattr(fan, "x", 1 if binary_sensor.motion else 0),
    "self-dependency": lambda: setattr(fan, "x", fan.x | binary_sensor.motion),
    "bound twice": lambda: setattr(fan, "bathroom", binary_sensor.motion),
}
for name, f in mistakes.items():
    try:
        f()
        print(f"NOT CAUGHT  {name}")
    except (TypeError, ValueError) as e:
        print(f"caught      {name}: {str(e)[:70]}...")
