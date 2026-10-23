"""Test rig: compile rule files, run them on a FakeHost, inspect service calls."""

from __future__ import annotations

from custom_components.homeo.core.checker import Globals, compile_source
from custom_components.homeo.core.runtime import Runtime

from .fakehost import FakeHost, standard_world


class Rig:
    def __init__(
        self, files: str | dict[str, str], host: FakeHost | None = None, start: bool = True
    ):
        self.h = host or standard_world()
        self.files = {"test.homeo": files} if isinstance(files, str) else dict(files)
        self.rt = Runtime(self.h)
        self._seen = 0
        self.rt.load(self.compile())
        if start:
            self.rt.start()

    def compile(self):
        g = Globals()
        programs = {}
        for name, src in sorted(self.files.items()):
            p, reads = compile_source(src, name, self.h, g)
            g.add(p, reads)
            programs[name] = p
        return programs

    # ------------------------------------------------------------------ calls

    def new_calls(self) -> list[str]:
        """Service calls since the last check, as compact strings."""
        out = []
        for c in self.h.calls[self._seen :]:
            s = f"{c.domain}.{c.service}"
            if c.entity_id:
                s += f" {c.entity_id}"
            if c.data:
                s += " " + " ".join(f"{k}={v}" for k, v in sorted(c.data.items()))
            out.append(s)
        self._seen = len(self.h.calls)
        return out

    def state(self, entity_id: str) -> str:
        return self.h.states[entity_id].state

    def machine(self, name: str) -> str | None:
        return self.rt.machine_value(name)

    # --------------------------------------------------------------- lifecycle

    def reload(self, files: str | dict[str, str]) -> None:
        self.files = {"test.homeo": files} if isinstance(files, str) else dict(files)
        self.rt.load(self.compile())

    def restart(self, downtime: float = 60.0, changes: dict | None = None) -> None:
        """Simulate an HA restart: no timers fire while down; at boot every entity's
        last_changed is the boot time; machines are restored from their select entities."""
        self.rt.stop()
        self.h.timers.clear()
        self.h.subs.clear()
        self.h.bus_subs.clear()
        self.h.t += downtime
        self.h.boot_time = self.h.t
        for eid, (value, changed_at, _opts) in self.h.machines.items():
            self.h.restored[eid] = (value, changed_at)
        for eid, (state, attrs) in (changes or {}).items():
            self.h.set_state(eid, state, attrs, notify=False)
        for eid, st in list(self.h.states.items()):
            self.h.set_state(eid, st.state, st.attributes, notify=False, last_changed=self.h.t)
            self.h.states[eid] = type(st)(eid, st.state, st.attributes, self.h.t, self.h.t)
        self.rt = Runtime(self.h)
        self.rt.load(self.compile())
        self.rt.start()
