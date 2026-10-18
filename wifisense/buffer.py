"""Thread-safe per-link sample ring buffer: sources append, the detector reads time windows."""

from __future__ import annotations

from collections import Counter, deque
from itertools import takewhile
import threading
import time

import numpy as np


class LinkBuffer:
    """Monotonic-time samples of one link: RSSI in dBm and optionally CSI subcarrier amplitudes."""

    def __init__(self, seconds: float = 120, max_rate: float = 200, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.samples = deque(maxlen=int(seconds * max_rate))
        self.total = 0
        self.last_time = None
        self.state = "starting"
        self.message = ""
        self.meta = {}

    def append(self, rssi: float | None, amps=None, t: float | None = None):
        t = self.clock() if t is None else t
        if amps is not None:
            amps = np.asarray(amps, dtype=np.float32)
        with self.lock:
            self.samples.append((t, rssi, amps))
            self.total += 1
            self.last_time = t
            if self.state != "ok":
                self.state, self.message = "ok", ""

    def set_state(self, state: str, message: str = "", **meta):
        with self.lock:
            self.state, self.message = state, message
            self.meta.update(meta)

    def window(self, seconds: float, now: float | None = None):
        """Times, RSSI values and the CSI matrix of the newest consistent subcarrier count (or None)."""
        now = self.clock() if now is None else now
        start = now - seconds
        with self.lock:
            # Samples are time-ordered: walk back from the newest and stop at the window start.
            selected = list(takewhile(lambda s: s[0] >= start, reversed(self.samples)))
        selected.reverse()
        times = np.array([s[0] for s in selected if s[1] is not None], dtype=np.float64)
        rssi = np.array([s[1] for s in selected if s[1] is not None], dtype=np.float64)
        amps = None
        csi = [s[2] for s in selected if s[2] is not None]
        if csi:
            # The most common subcarrier count wins (ties: the newest); odd frame types are left out.
            counts = Counter(len(a) for a in csi)
            width = max(counts, key=lambda w: (counts[w], w == len(csi[-1])))
            same = [a for a in csi if len(a) == width]
            amps = np.vstack(same) if len(same) >= 2 else None
        return times, rssi, amps

    def rate(self, seconds: float = 5, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        with self.lock:
            count = sum(1 for _ in takewhile(lambda s: s[0] >= now - seconds, reversed(self.samples)))
        return count / seconds

    def info(self, stale_seconds: float, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        with self.lock:
            last, state, message, meta = self.last_time, self.state, self.message, dict(self.meta)
        if state == "ok" and (last is None or now - last > stale_seconds):
            state, message = "stale", "Nincs friss mérés"
        return {"state": state, "message": message, "rate": round(self.rate(now=now), 1), **meta}
