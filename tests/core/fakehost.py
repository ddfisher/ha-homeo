"""A fake Home Assistant for core tests: in-memory states, a simulated clock and recorded
service calls. Mimics the HA behaviour the core relies on (research notes §3–§6)."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.homeo.core.host import EntityState, ServiceCall, StateChange

# Monday 2026-01-05 08:00 UTC
DEFAULT_START = datetime(2026, 1, 5, 8, 0, tzinfo=ZoneInfo("UTC")).timestamp()


class FakeHost:
    def __init__(
        self,
        start: float = DEFAULT_START,
        tz: str = "UTC",
        boot_time: float | None = None,
        simulate_devices: bool = True,
    ):
        self.t = start
        self.tz = ZoneInfo(tz)
        # By default HA has been running for two days when homeo loads.
        self.boot_time = start - 2 * 86400 if boot_time is None else boot_time
        self.states: dict[str, EntityState] = {}
        self.registry: set[str] = set()  # entities known without a state
        self.subs: dict[str, list[Callable[[StateChange], None]]] = {}
        self.bus_subs: dict[str, list[Callable[[dict[str, Any], float], None]]] = {}
        self.timers: list[tuple[float, int, list]] = []
        self.seq = itertools.count()
        self.calls: list[ServiceCall] = []
        self.call_times: list[float] = []
        self.logs: list[tuple[str, str]] = []
        self.machines: dict[str, tuple[str, float, list[str]]] = {}
        self.restored: dict[str, tuple[str, float]] = {}
        self.services: dict[tuple[str, str], bool] = {}
        self.simulate_devices = simulate_devices
        self.solar_times = {
            "sunrise": time(7, 0),
            "sunset": time(19, 0),
            "dawn": time(6, 30),
            "dusk": time(19, 30),
        }

    # ------------------------------------------------------------- catalog

    def get_state(self, entity_id: str) -> EntityState | None:
        return self.states.get(entity_id)

    def entity_exists(self, entity_id: str) -> bool:
        return entity_id in self.states or entity_id in self.registry

    def entity_ids(self, domain: str | None = None) -> list[str]:
        ids = set(self.states) | self.registry
        return sorted(i for i in ids if domain is None or i.split(".")[0] == domain)

    def domains(self) -> set[str]:
        return {i.split(".")[0] for i in set(self.states) | self.registry}

    def has_service(self, domain: str, service: str) -> bool | None:
        return self.services.get((domain, service))

    # ---------------------------------------------------------------- host

    def now(self) -> float:
        return self.t

    def subscribe_states(self, entity_ids, callback):
        for e in entity_ids:
            self.subs.setdefault(e, []).append(callback)

        def unsub():
            for e in entity_ids:
                if callback in self.subs.get(e, []):
                    self.subs[e].remove(callback)

        return unsub

    def subscribe_bus(self, event_type, callback):
        self.bus_subs.setdefault(event_type, []).append(callback)
        return lambda: self.bus_subs[event_type].remove(callback)

    def schedule(self, when, callback):
        entry = [callback, False]
        heapq.heappush(self.timers, (when, next(self.seq), entry))

        def cancel():
            entry[1] = True

        return cancel

    def call_service(self, call: ServiceCall) -> None:
        self.calls.append(call)
        self.call_times.append(self.t)
        if self.simulate_devices and call.entity_id:
            self.simulate(call)

    # ------------------------------------------------------------ sun.sun

    SUN_ATTRS = {
        "sunrise": "next_rising",
        "sunset": "next_setting",
        "dawn": "next_dawn",
        "dusk": "next_dusk",
    }

    def solar_at(self, event: str, day: date) -> float:
        """When `event` happens on a local date, in this fake world (fixed local times)."""
        return datetime.combine(day, self.solar_times[event], tzinfo=self.tz).timestamp()

    def start_sun(self) -> None:
        """Run a `sun.sun` entity like HA's: it shows the *next* occurrence of each event and
        recomputes them when one passes."""
        self._sun_cancel = None
        self._update_sun(self.t)

    def _update_sun(self, _when: float) -> None:
        today = self.local_now().date()
        nexts = {}
        for event in self.SUN_ATTRS:
            nexts[event] = min(
                t
                for d in range(-1, 3)
                if (t := self.solar_at(event, today + timedelta(days=d))) > self.t
            )
        up = nexts["sunset"] < nexts["sunrise"]
        attrs = {attr: self.iso(nexts[e]) for e, attr in self.SUN_ATTRS.items()}
        self.set_state("sun.sun", "above_horizon" if up else "below_horizon", attrs)
        self._sun_cancel = self.schedule(min(nexts.values()), self._update_sun)

    def stop_sun(self) -> None:
        """Make sun.sun unavailable (and stop updating it)."""
        if self._sun_cancel:
            self._sun_cancel()
        self.set_state("sun.sun", "unavailable")

    def publish_machine(self, name, value, changed_at, options):
        self.machines[name] = (value, changed_at, list(options))

    def restore_machine(self, name):
        return self.restored.get(name)

    def log(self, level, message):
        self.logs.append((level, message))

    # ------------------------------------------------------------- driving

    def set_state(
        self,
        entity_id: str,
        state: Any,
        attributes: dict[str, Any] | None = None,
        *,
        ours: bool = False,
        last_changed: float | None = None,
        notify: bool = True,
        merge: bool = False,
    ) -> None:
        if isinstance(state, bool):
            state = "on" if state else "off"
        state = str(state)
        old = self.states.get(entity_id)
        attrs = dict(old.attributes) if (merge and old) else {}
        attrs.update(attributes or {})
        if old is not None and old.state == state and old.attributes == attrs:
            return  # HA fires state_reported, which the core doesn't track
        if last_changed is None:
            last_changed = old.last_changed if (old is not None and old.state == state) else self.t
        new = EntityState(entity_id, state, attrs, last_changed, self.t)
        self.states[entity_id] = new
        if notify:
            change = StateChange(entity_id, old, new, self.t, ours)
            for cb in list(self.subs.get(entity_id, [])):
                cb(change)

    def remove(self, entity_id: str) -> None:
        old = self.states.pop(entity_id)
        for cb in list(self.subs.get(entity_id, [])):
            cb(StateChange(entity_id, old, None, self.t, False))

    def fire(self, event_type: str, data: dict[str, Any]) -> None:
        for cb in list(self.bus_subs.get(event_type, [])):
            cb(data, self.t)

    def press(self, entity_id: str) -> None:
        """Press a button / fire an event entity (state = ISO time of the press)."""
        self.set_state(entity_id, self.iso(self.t), merge=True)

    def event(self, entity_id: str, event_type: str) -> None:
        old = self.states.get(entity_id)
        attrs = dict(old.attributes) if old else {}
        attrs["event_type"] = event_type
        self.set_state(entity_id, self.iso(self.t), attrs)

    def iso(self, t: float) -> str:
        return datetime.fromtimestamp(t, ZoneInfo("UTC")).isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.advance_to(self.t + seconds)

    def advance_to(self, target: float) -> None:
        while self.timers and self.timers[0][0] <= target:
            when, _, entry = heapq.heappop(self.timers)
            if entry[1]:
                continue
            self.t = max(self.t, when)
            entry[0](when)
        self.t = target

    def local(self, y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> float:
        return datetime(y, mo, d, h, mi, s, tzinfo=self.tz).timestamp()

    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self.t, self.tz)

    def pending_timers(self) -> list[float]:
        return sorted(w for w, _, e in self.timers if not e[1])

    # ------------------------------------------------------ device simulation

    def simulate(self, call: ServiceCall) -> None:
        e = call.entity_id
        assert e is not None
        old = self.states.get(e)
        attrs = dict(old.attributes) if old else {}
        d, s, data = call.domain, call.service, call.data
        if s == "turn_off":
            for a in ("brightness", "color_temp_kelvin", "percentage"):
                attrs.pop(a, None)
            self.set_state(e, "off", attrs, ours=True)
        elif s == "turn_on":
            if "brightness_pct" in data:
                attrs["brightness"] = round(255 * data["brightness_pct"] / 100)
            elif d == "light":
                attrs.setdefault("brightness", 255)
            if "color_temp_kelvin" in data:
                attrs["color_temp_kelvin"] = data["color_temp_kelvin"]
            if "percentage" in data:
                attrs["percentage"] = data["percentage"]
            self.set_state(e, "on", attrs, ours=True)
        elif s == "set_hvac_mode":
            self.set_state(e, data["hvac_mode"], attrs, ours=True)
        elif s == "set_temperature":
            attrs["temperature"] = data["temperature"]
            self.set_state(e, old.state if old else "heat", attrs, ours=True)
        elif s == "set_cover_position":
            attrs["current_position"] = data["position"]
            self.set_state(e, "open" if data["position"] > 0 else "closed", attrs, ours=True)
        elif s in ("open_cover", "close_cover"):
            self.set_state(e, "open" if s == "open_cover" else "closed", attrs, ours=True)
        elif s in ("lock", "unlock"):
            self.set_state(e, "locked" if s == "lock" else "unlocked", attrs, ours=True)
        elif s == "set_value":
            self.set_state(e, data["value"], attrs, ours=True)
        elif s == "select_option":
            self.set_state(e, data["option"], attrs, ours=True)
        elif s == "volume_set":
            attrs["volume_level"] = data["volume_level"]
            self.set_state(e, old.state if old else "on", attrs, ours=True)


def world(host: FakeHost | None = None, **entities: Any) -> FakeHost:
    """Create a host with entities: world(**{"sensor.co2": 500, "light.hall": "off"})."""
    host = host or FakeHost()
    for eid, v in entities.items():
        if isinstance(v, tuple):
            host.set_state(eid, v[0], v[1], notify=False, last_changed=host.t - 86400)
        else:
            host.set_state(eid, v, notify=False, last_changed=host.t - 86400)
    return host


def standard_world(host: FakeHost | None = None) -> FakeHost:
    """Entities used across tests."""
    h = world(
        host,
        **{
            "sensor.bathroom_co2": (
                "500",
                {"unit_of_measurement": "ppm", "state_class": "measurement"},
            ),
            "sensor.bathroom_humidity": (
                "50",
                {"unit_of_measurement": "%", "state_class": "measurement"},
            ),
            "sensor.outdoor_temp": ("12", {"unit_of_measurement": "°C"}),
            "sensor.lux": ("100", {"unit_of_measurement": "lx"}),
            "sensor.last_boot": ("2026-01-01T00:00:00+00:00", {"device_class": "timestamp"}),
            "sensor.mystery": "unknown",
            "binary_sensor.bathroom_window": "off",
            "binary_sensor.bathroom_motion": "off",
            "binary_sensor.hall_motion": "off",
            "binary_sensor.holiday": "off",
            "binary_sensor.washer_door": "off",
            "fan.bathroom": ("off", {"percentage_step": 1}),
            "fan.bedroom": "off",
            "light.hall": "off",
            "light.bathroom": "off",
            "light.porch": "off",
            "light.desk": "off",
            "switch.pump": "off",
            "climate.living": (
                "heat",
                {
                    "temperature": 20,
                    "current_temperature": 19.5,
                    "target_temp_step": 0.5,
                    "hvac_modes": ["off", "heat"],
                },
            ),
            "cover.blinds": ("open", {"current_position": 100}),
            "cover.garage": "closed",
            "lock.front": "locked",
            "media_player.tv": ("on", {"volume_level": 0.2}),
            "input_number.target": ("5", {"step": 1}),
            "input_select.mode": ("eco", {"options": ["eco", "comfort", "away"]}),
            "input_text.note": "",
            "input_boolean.guest": "off",
            "input_button.boost": "unknown",
            "input_button.warm_up": "unknown",
            "input_button.goodnight": "unknown",
            "input_button.desk_on": "unknown",
            "input_button.desk_off": "unknown",
            "input_button.doorbell": "unknown",
            "event.bathroom_remote": (
                "unknown",
                {"event_types": ["press", "long_press", "double_press"]},
            ),
            "person.ann": "home",
            "sensor.washer_power": ("0", {"unit_of_measurement": "W"}),
            "group.family": "home",
            "select.homeo_other": ("a", {"options": ["a", "b"]}),
        },
    )
    h.start_sun()
    h.registry.add("light.disabled_one")
    h.services[("notify", "mobile_app_phone")] = True
    h.services[("light", "turn_on")] = True
    return h


def at(host: FakeHost, delta: timedelta) -> float:
    return host.t + delta.total_seconds()
