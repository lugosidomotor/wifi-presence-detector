import pytest

from wifisense.config import load_config
from wifisense.runtime import Runtime


class VirtualClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def demo_cfg(tmp_path):
    cfg = load_config(None, environ={"WIFI_DATA_DIR": str(tmp_path)}, demo=True)
    cfg["sim"]["auto"] = False
    cfg["sim"]["seed"] = 7
    return cfg


class Harness:
    """Runtime on virtual time: the simulated world and the detector advance in lockstep, no threads."""

    def __init__(self, cfg, wall_start=1_800_000_000.0):
        self.clock = VirtualClock()
        self.wall_offset = wall_start - self.clock.now
        self.rt = Runtime(cfg, clock=self.clock, wall=lambda: self.clock.now + self.wall_offset)
        self.tick = cfg["detection"]["tick_seconds"]
        self.states = []

    def run(self, seconds):
        for _ in range(round(seconds / self.tick)):
            self.clock.now = self.rt.sim.run_for(self.tick, self.clock.now)
            self.rt.step(self.clock.now)
            self.states.append(self.rt.latest["state"])
        return self.rt.latest

    def calibrate(self, seconds=30):
        self.rt.start_calibration(seconds)
        self.run(seconds + 1)
        assert self.rt.calibration_result["ok"], self.rt.calibration_result


@pytest.fixture
def harness(demo_cfg):
    h = Harness(demo_cfg)
    yield h
    h.rt.store.close()
