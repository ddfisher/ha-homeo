"""HomeoManager: owns the runtime, watches rule files, compiles and reloads them atomically."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .const import CONF_DRY_RUN, DOMAIN, ISSUE_PREFIX, POLL_INTERVAL, RULES_DIR, RULES_SUFFIX
from .core.checker import Globals, check
from .core.errors import CompileError, Diagnostic
from .core.ir import Program
from .core.parser import parse
from .core.runtime import Runtime
from .hass_host import HassHost

if TYPE_CHECKING:
    from .select import MachineSelect

_LOGGER = logging.getLogger(__package__)


@dataclass
class RuleFile:
    name: str
    mtime: float
    size: int
    text: str


def _read_rule_files(directory: Path) -> dict[str, RuleFile]:
    """Runs in the executor."""
    directory.mkdir(exist_ok=True)
    out = {}
    for path in sorted(directory.glob(f"*{RULES_SUFFIX}")):
        st = path.stat()
        out[path.name] = RuleFile(path.name, st.st_mtime, st.st_size, path.read_text("utf-8"))
    return out


def _stat_rule_files(directory: Path) -> dict[str, tuple[float, int]]:
    """Runs in the executor."""
    if not directory.is_dir():
        return {}
    return {
        p.name: (p.stat().st_mtime, p.stat().st_size) for p in directory.glob(f"*{RULES_SUFFIX}")
    }


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
        self.errors: dict[str, list[Diagnostic]] = {}
        self.add_select_entities: Callable[[list[Any]], None] | None = None
        self.machine_entities: dict[str, MachineSelect] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._reloading = False
        self._started = False
        self._entity_count = 0  # entities known at the last compile

    @property
    def dry_run(self) -> bool:
        return bool(self.entry.options.get(CONF_DRY_RUN, False))

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        # Compile and run once HA has started (D17 rule 1), so entities from integrations
        # that load after us exist and have states to infer types from.
        self._unsubs.append(async_at_started(self.hass, self._async_started))

    async def _async_started(self, _hass: HomeAssistant) -> None:
        self.host.boot_time = self.host.now()
        self._started = True
        await self.async_reload()
        self.runtime.start()
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_poll, POLL_INTERVAL, cancel_on_shutdown=True
            )
        )

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
        self.runtime.stop()

    # --------------------------------------------------------------- reloads

    async def _async_poll(self, _now: datetime) -> None:
        if self._reloading:
            return
        stats = await self.hass.async_add_executor_job(_stat_rule_files, self.directory)
        known = {name: (f.mtime, f.size) for name, f in self.files.items()}
        # Files with errors are retried when entities appear (e.g. a late integration).
        if stats != known or (self.errors and self._count_entities() != self._entity_count):
            await self.async_reload()

    def _count_entities(self) -> int:
        return len(self.hass.states.async_entity_ids()) + len(er.async_get(self.hass).entities)

    async def async_reload(self) -> None:
        """Re-read and recompile all files. A file with errors keeps its previous version
        running and gets a repairs issue (§2, §19)."""
        if not self._started:
            return
        self._reloading = True
        try:
            files = await self.hass.async_add_executor_job(_read_rule_files, self.directory)
        finally:
            self._reloading = False
        self.files = files
        self._entity_count = self._count_entities()
        self.apply(self.compile_all(files))

    def compile_all(self, files: dict[str, RuleFile]) -> dict[str, Program]:
        g = Globals()
        programs: dict[str, Program] = {}
        for name, f in sorted(files.items()):
            try:
                program, reads = check(parse(f.text, name), self.host, g)
            except CompileError as err:
                self.report(name, err.diagnostics, f.text)
                old = self.programs.get(name)
                if old is not None:
                    programs[name] = old
                    g.add(old, {})
                continue
            if self.dry_run:
                program.dry_run = True
            self.clear_report(name)
            for w in program.warnings:
                _LOGGER.warning(w.format(f.text))
            programs[name] = program
            g.add(program, reads)
        for name in list(self.errors):
            if name not in files:
                self.clear_report(name)
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
