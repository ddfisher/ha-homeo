"""HomeoManager: owns the runtime, watches rule files, compiles and reloads them atomically."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started

from .const import (
    BOOT_TIME_KEY,
    CONF_DRY_RUN,
    DOMAIN,
    ENTITY_DEBOUNCE,
    FILE_DEBOUNCE,
    ISSUE_PREFIX,
    RULES_DIR,
    RULES_SUFFIX,
)
from .core.checker import Globals, check
from .core.errors import CompileError, Diagnostic
from .core.explain import NotFound, explain
from .core.ir import Program
from .core.parser import parse
from .core.runtime import Runtime
from .hass_host import HassHost
from .watcher import RulesWatcher

if TYPE_CHECKING:
    from .select import MachineSelect

_LOGGER = logging.getLogger(__package__)


@dataclass
class RuleFile:
    name: str
    text: str


def _read_rule_files(directory: Path) -> dict[str, RuleFile]:
    """Runs in the executor."""
    directory.mkdir(exist_ok=True)
    out = {}
    for path in sorted(directory.glob(f"*{RULES_SUFFIX}")):
        out[path.name] = RuleFile(path.name, path.read_text("utf-8"))
    return out


def issue_id(file: str) -> str:
    return ISSUE_PREFIX + re.sub(r"[^a-z0-9_]", "_", file.lower())


class HomeoManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.directory = Path(hass.config.path(RULES_DIR))
        self.host = HassHost(hass, self)
        self.runtime = Runtime(self.host)
        self.files: dict[str, RuleFile] = {}
        self.programs: dict[str, Program] = {}
        self.sources: dict[str, str] = {}  # the text each running program was compiled from
        self.errors: dict[str, list[Diagnostic]] = {}
        self.add_select_entities: Callable[[list[Any]], None] | None = None
        self.machine_entities: dict[str, MachineSelect] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._started = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._debounce: Callable[[], None] | None = None
        self._stop_unsub: Callable[[], None] | None = None
        self.watcher = RulesWatcher(self.directory, self._file_changed)

    @property
    def dry_run(self) -> bool:
        return bool(self.entry.options.get(CONF_DRY_RUN, False))

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        # Compile and run once HA has started (D17 rule 1), so entities from integrations
        # that load after us exist and have states to infer types from.
        self._stop_unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_on_stop
        )
        self._unsubs.append(async_at_started(self.hass, self._async_started))

    async def _async_started(self, _hass: HomeAssistant) -> None:
        # Change times before the first start in this HA run mean "some time before the
        # restart" (D17 rule 2). Kept across config-entry reloads, which aren't restarts.
        boot = self.hass.data.setdefault(BOOT_TIME_KEY, self.host.now())
        self.host.boot_time = boot
        self._started = True
        await self.async_reload()
        self.runtime.start()
        await self.hass.async_add_executor_job(self.watcher.start)
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._entity_appeared, event_filter=self._is_new_entity
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._entity_appeared,
                event_filter=self._is_registry_create,
            )
        )

    async def _async_on_stop(self, _event: Event) -> None:
        self._stop_unsub = None
        self._closed = True
        await self.hass.async_add_executor_job(self.watcher.stop)

    async def async_stop(self) -> None:
        self._closed = True
        await self.hass.async_add_executor_job(self.watcher.stop)
        for u in self._unsubs:
            u()
        self._unsubs = []
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        if self._debounce is not None:
            self._debounce()
            self._debounce = None
        self.runtime.stop()

    # --------------------------------------------------------------- reloads

    def _file_changed(self) -> None:
        """Called from the watcher thread."""
        self.hass.loop.call_soon_threadsafe(self._schedule_reload, FILE_DEBOUNCE)

    @callback
    def _is_new_entity(self, data: Mapping[str, Any]) -> bool:
        return bool(self.errors) and data["old_state"] is None

    @callback
    def _is_registry_create(self, data: Mapping[str, Any]) -> bool:
        return bool(self.errors) and data.get("action") == "create"

    @callback
    def _entity_appeared(self, _event: Event) -> None:
        # Files with errors are retried when entities appear (e.g. a late integration).
        self._schedule_reload(ENTITY_DEBOUNCE)

    @callback
    def _schedule_reload(self, delay: float) -> None:
        """Reload after `delay` s without further changes (editors save in several steps)."""
        if self._closed:  # an event that arrived after we stopped
            return
        if self._debounce is not None:
            self._debounce()
        self._debounce = async_call_later(self.hass, delay, self._async_debounced_reload)

    async def _async_debounced_reload(self, _now: datetime) -> None:
        self._debounce = None
        await self.async_reload()

    async def async_reload(self) -> None:
        """Re-read and recompile all files. A file with errors keeps its previous version
        running and gets a repairs issue (§2, §19)."""
        if not self._started:
            return
        async with self._lock:
            files = await self.hass.async_add_executor_job(_read_rule_files, self.directory)
            self.files = files
            self.apply(self.compile_all(files))

    def compile_all(self, files: dict[str, RuleFile]) -> dict[str, Program]:
        g = Globals()
        programs: dict[str, Program] = {}
        sources: dict[str, str] = {}
        # Unchanged files first, so a conflict between files (two bindings for one target,
        # two machines with one name) is reported in the file that was just edited.
        order = sorted(files.items(), key=lambda kv: (self.sources.get(kv[0]) != kv[1].text, kv[0]))
        broken: list[str] = []
        for name, f in order:
            try:
                program, reads = check(parse(f.text, name), self.host, g)
            except CompileError as err:
                self.report(name, err.diagnostics, f.text)
                broken.append(name)
                continue
            if self.dry_run:
                program.dry_run = True
            self.clear_report(name)
            for w in program.warnings:
                _LOGGER.warning(w.format(f.text))
            programs[name] = program
            sources[name] = f.text
            g.add(program, reads)
        # A file with errors keeps running its previous version, unless that would now clash
        # with a file that compiled: two rules must never drive one device (D5).
        for name in broken:
            old = self.programs.get(name)
            if old is None:
                continue
            if clashes := g.conflicts(old):
                _LOGGER.error(
                    "stopped the previous version of %s, which conflicts with other files: %s",
                    name,
                    "; ".join(clashes),
                )
                continue
            programs[name] = old
            sources[name] = self.sources[name]
            g.add(old, {})
        for name in list(self.errors):
            if name not in files:
                self.clear_report(name)
        self.sources = sources
        return programs

    def apply(self, programs: dict[str, Program]) -> None:
        self.programs = programs
        machines = {m.name for p in programs.values() for m in p.machines}
        self.runtime.load(programs)
        self.remove_stale_machines(machines)
        _LOGGER.info(
            "loaded %d rule file(s): %s",
            len(programs),
            ", ".join(sorted(programs)) or "none",
        )

    def report(self, file: str, diagnostics: list[Diagnostic], source: str) -> None:
        self.errors[file] = diagnostics
        text = "\n\n".join(d.format(source) for d in diagnostics)
        _LOGGER.error("errors in %s; the previous version keeps running:\n%s", file, text)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(file),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="compile_error",
            translation_placeholders={"file": file, "errors": text},
        )

    def clear_report(self, file: str) -> None:
        if self.errors.pop(file, None) is not None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(file))

    # --------------------------------------------------------------- explain

    @callback
    def explain(self, call: ServiceCall) -> ServiceResponse:
        """`homeo.explain` (§19): why a binding, state machine or `let` has its value."""
        target = call.data["target"]
        if not self._started:
            raise ServiceValidationError("Homeo hasn't loaded its rules yet")
        try:
            return {"explanations": explain(self.runtime, target)}
        except NotFound as err:
            raise ServiceValidationError(str(err)) from err

    # -------------------------------------------------------------- machines

    def publish_machine(self, name: str, value: str, changed_at: float, options: list[str]) -> None:
        from .select import MachineSelect

        ent = self.machine_entities.get(name)
        if ent is not None:
            ent.update_machine(value, changed_at, options)
            return
        if self.add_select_entities is None:
            return
        ent = MachineSelect(self, name, value, changed_at, options)
        self.machine_entities[name] = ent
        self.add_select_entities([ent])

    def remove_stale_machines(self, keep: set[str]) -> None:
        registry = er.async_get(self.hass)
        for name in list(self.machine_entities):
            if name in keep:
                continue
            ent = self.machine_entities.pop(name)
            if ent.hass is not None:
                self.hass.async_create_task(ent.async_remove())
            if ent.entity_id and registry.async_get(ent.entity_id):
                registry.async_remove(ent.entity_id)

    def select_option(self, name: str, option: str) -> None:
        self.runtime.set_machine(name, option)
