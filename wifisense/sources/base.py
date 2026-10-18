"""Thread lifecycle shared by the sources: start, stop, reconnect with capped back-off."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class Source:
    name = "source"

    def __init__(self, label: str):
        self.label = label
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._guarded, name=f"{self.name}-{label}", daemon=True)

    def start(self):
        self.thread.start()

    def close(self, timeout: float = 3):
        self.stopping.set()
        self._interrupt()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def _interrupt(self):
        """Unblock a pending read (close a socket or port); optional."""

    def _guarded(self):
        delay = 1.0
        while not self.stopping.is_set():
            try:
                self.run()
                delay = 1.0
            except Exception as exc:  # a source must never take the runtime down
                log.warning("%s %s failed (%s: %s); retrying in %.0fs", self.name, self.label, type(exc).__name__, exc, delay)
                self.on_error(exc)
            if self.stopping.wait(delay):
                return
            delay = min(delay * 2, 30)

    def on_error(self, exc: Exception):
        """Report the failure on the link; override."""

    def run(self):
        raise NotImplementedError
