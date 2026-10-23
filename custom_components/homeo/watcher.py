"""Watch the rules directory for changes to `.homeo` files (watchdog: inotify on Linux)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .const import RULES_SUFFIX

_LOGGER = logging.getLogger(__package__)

# Events that can change a file's contents or existence ("opened" and "closed_no_write" can't).
_RELEVANT = {"created", "modified", "deleted", "moved", "closed"}


class _Handler(FileSystemEventHandler):
    def __init__(self, notify: Callable[[], None]):
        self.notify = notify

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in _RELEVANT:
            return
        paths = (event.src_path, getattr(event, "dest_path", "") or "")
        if any(str(p).endswith(RULES_SUFFIX) for p in paths):
            self.notify()


class RulesWatcher:
    """Calls `notify` (from the watcher thread) whenever a rule file changes.

    `start` and `stop` block, so call them in the executor. They may run concurrently on
    different executor threads (HA stopping while we start); a stop wins over a later start.
    """

    def __init__(self, directory: Path, notify: Callable[[], None]):
        self.directory = directory
        self.notify = notify
        self.observer: BaseObserver | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def start(self) -> None:
        with self._lock:
            if self._stopped or self.observer is not None:
                return
            self.directory.mkdir(exist_ok=True)
            try:
                observer: BaseObserver = Observer()
                observer.schedule(_Handler(self.notify), str(self.directory), recursive=False)
                observer.start()
            except OSError as err:  # e.g. inotify watch limit reached
                _LOGGER.warning(
                    "can't watch %s for changes (%s); checking it every 2 s instead",
                    self.directory,
                    err,
                )
                observer = PollingObserver(timeout=2)
                observer.schedule(_Handler(self.notify), str(self.directory), recursive=False)
                observer.start()
            self.observer = observer

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self.observer is not None:
                self.observer.stop()
                self.observer.join()
                self.observer = None
