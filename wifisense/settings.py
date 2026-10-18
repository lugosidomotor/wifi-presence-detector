"""UI-changeable state that survives restarts: arming. Written atomically to data/wifi-settings.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


class Settings:
    def __init__(self, path: Path, arm_delay: float, clock=time.time):
        self.path = Path(path)
        self.arm_delay = arm_delay
        self.clock = clock
        self.lock = threading.Lock()
        self.armed = False
        self.armed_at = None
        try:
            data = json.loads(self.path.read_text())
            self.armed = bool(data.get("armed", False))
            self.armed_at = float(data["armed_at"]) if self.armed and data.get("armed_at") is not None else None
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if self.armed and self.armed_at is None:
            self.armed_at = self.clock()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"armed": self.armed, "armed_at": self.armed_at}))
        os.replace(tmp, self.path)

    def set_armed(self, armed: bool) -> dict:
        with self.lock:
            if armed != self.armed:
                self.armed = armed
                self.armed_at = self.clock() if armed else None
                self._save()
        return self.arming()

    def arming(self) -> dict:
        """armed: switched on; active: the exit delay has passed; active_at: epoch when it does."""
        with self.lock:
            armed, armed_at = self.armed, self.armed_at
        active_at = armed_at + self.arm_delay if armed else None
        return {
            "armed": armed,
            "active": bool(armed and self.clock() >= active_at),
            "armed_at": armed_at,
            "active_at": active_at,
            "delay_seconds": self.arm_delay,
        }
