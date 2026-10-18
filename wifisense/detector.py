"""Empty-room baselines, per-link scores, human likelihood and the presence state machine."""

from __future__ import annotations

from collections import deque
import math

import numpy as np

from .features import profile_shift

# Lower bounds of the baseline spread, so a perfectly flat empty room does not make every flicker an alarm.
FLOORS = {"rssi_std": 0.2, "rssi_mean": 0.5, "csi_std": 0.0025, "csi_shift": 0.002}
ACTIVITY = ("rssi_std", "csi_std")


def robust(values, floor):
    values = np.asarray(values, dtype=np.float64)
    center = float(np.median(values))
    return {"center": center, "scale": max(1.4826 * float(np.median(np.abs(values - center))), floor)}


def quiet_baseline(values, floor):
    """Spread from the quiet lower part only: the room may have been occupied for part of the history."""
    values = np.asarray(values, dtype=np.float64)
    p5, p40 = np.percentile(values, [5, 40])
    return {"center": float(p40), "scale": max(float(p40 - p5) / 1.39, floor)}


class Baseline:
    def __init__(self, stats: dict, profile=None, origin="auto", created=None):
        self.stats = stats
        self.profile = None if profile is None else np.asarray(profile, dtype=np.float64)
        self.origin = origin
        self.created = created

    @classmethod
    def from_features(cls, feats: list, origin: str, created=None, quiet=False):
        stats = {}
        profile = None
        profiles = [f["csi_profile"] for f in feats if f.get("csi_profile") is not None]
        if profiles:
            width = len(profiles[-1])
            same = [p for p in profiles if len(p) == width]
            if quiet:
                # The profile of the quietest windows best describes the empty room.
                activity = [f.get("csi_std", 0) for f in feats if f.get("csi_profile") is not None
                            and len(f["csi_profile"]) == width]
                limit = np.percentile(activity, 40)
                same = [p for p, a in zip(same, activity) if a <= limit] or same
            profile = np.mean(same, axis=0)
        for key in ACTIVITY:
            values = [f[key] for f in feats if key in f]
            if len(values) >= 4:
                stats[key] = (quiet_baseline if quiet else robust)(values, FLOORS[key])
        if any("rssi_mean" in f for f in feats):
            selected = feats
            if quiet and "rssi_std" in stats:
                limit = np.percentile([f.get("rssi_std", 0) for f in feats], 40)
                selected = [f for f in feats if f.get("rssi_std", 0) <= limit] or feats
            values = [f["rssi_mean"] for f in selected if "rssi_mean" in f]
            if len(values) >= 4:
                stats["rssi_mean"] = robust(values, FLOORS["rssi_mean"])
        if profile is not None:
            shifts = [s for f in feats if (s := profile_shift(f.get("csi_profile"), profile)) is not None]
            if len(shifts) >= 4:
                stats["csi_shift"] = (quiet_baseline if quiet else robust)(shifts, FLOORS["csi_shift"])
        return cls(stats, profile, origin, created)

    def to_json(self):
        return {
            "stats": self.stats,
            "profile": None if self.profile is None else [round(float(v), 6) for v in self.profile],
            "origin": self.origin,
            "created": self.created,
        }

    @classmethod
    def from_json(cls, data):
        return cls(data["stats"], data.get("profile"), data.get("origin", "calibration"), data.get("created"))


class LinkDetector:
    """Scores one link against its baseline: motion (spread) and presence (level shift), 1.0 = threshold."""

    AUTO_REFRESH_SECONDS = 30

    def __init__(self, cfg: dict, baseline: Baseline | None = None):
        detection = cfg["detection"]
        self.tick = detection["tick_seconds"]
        self.motion_threshold = detection["motion_threshold"]
        self.presence_threshold = detection["presence_threshold"]
        self.alpha = self.tick / detection["adapt_seconds"]
        self.min_history = max(8, int(detection["warmup_seconds"] / self.tick))
        self.history = deque(maxlen=int(600 / self.tick))
        self.baseline = baseline
        self.since_refresh = 0.0

    @property
    def ready(self):
        return self.baseline is not None and bool(self.baseline.stats)

    @property
    def calibrated(self):
        return self.ready and self.baseline.origin == "calibration"

    def set_baseline(self, baseline: Baseline | None):
        self.baseline = baseline
        self.history.clear()

    def _z(self, key, value, absolute=False):
        stat = self.baseline.stats.get(key) if self.baseline else None
        if stat is None or value is None:
            return 0.0
        delta = value - stat["center"]
        return max(0.0, (abs(delta) if absolute else delta) / stat["scale"])

    def update(self, feats: dict | None, empty: bool, created=None) -> dict:
        if feats is None:
            return {"motion": 0.0, "presence": 0.0, "mech": 0.0, "valid": False}
        self.history.append(feats)
        if not self.calibrated:
            self.since_refresh += self.tick
            if len(self.history) >= self.min_history and (not self.ready or self.since_refresh >= self.AUTO_REFRESH_SECONDS):
                self.baseline = Baseline.from_features(list(self.history), "auto", created, quiet=True)
                self.since_refresh = 0.0
        if not self.ready:
            return {"motion": 0.0, "presence": 0.0, "mech": feats.get("mech", 0.0), "valid": False}
        shift = profile_shift(feats.get("csi_profile"), self.baseline.profile)
        z = {
            "rssi_std": self._z("rssi_std", feats.get("rssi_std")),
            "csi_std": self._z("csi_std", feats.get("csi_std")),
            "rssi_mean": self._z("rssi_mean", feats.get("rssi_mean"), absolute=True),
            "csi_shift": self._z("csi_shift", shift),
        }
        if empty and self.calibrated:
            self._adapt(feats, shift, z)
        return {
            "motion": max(z["rssi_std"], z["csi_std"]) / self.motion_threshold,
            "presence": max(z["rssi_mean"], z["csi_shift"]) / self.presence_threshold,
            "mech": feats.get("mech", 0.0),
            "valid": True,
            "z": {k: round(v, 2) for k, v in z.items()},
        }

    def reanchor(self, seconds: float = 30):
        """Accept the current level as the empty room after a long shift without any movement."""
        if not self.ready or not self.history:
            return
        recent = list(self.history)[-max(1, int(seconds / self.tick)):]
        values = [f["rssi_mean"] for f in recent if "rssi_mean" in f]
        if values and "rssi_mean" in self.baseline.stats:
            self.baseline.stats["rssi_mean"]["center"] = float(np.median(values))
        if self.baseline.profile is not None:
            width = len(self.baseline.profile)
            profiles = [f["csi_profile"] for f in recent if f.get("csi_profile") is not None and len(f["csi_profile"]) == width]
            if profiles:
                self.baseline.profile = np.mean(profiles, axis=0)

    def _adapt(self, feats, shift, z):
        """Follow slow drift (temperature, furniture, router power) while the room is known to be empty."""
        values = {"rssi_std": feats.get("rssi_std"), "csi_std": feats.get("csi_std"),
                  "rssi_mean": feats.get("rssi_mean"), "csi_shift": shift}
        for key, value in values.items():
            stat = self.baseline.stats.get(key)
            if stat is not None and value is not None and z[key] < 2:
                stat["center"] += self.alpha * (value - stat["center"])
        profile = feats.get("csi_profile")
        if self.baseline.profile is not None and profile is not None and len(profile) == len(self.baseline.profile):
            if z["csi_shift"] < 2:
                self.baseline.profile += self.alpha * (profile - self.baseline.profile)


class HumanScorer:
    """0..1 likelihood that the disturbance is a person: strong, sustained and not machine-periodic.

    WiFi cannot see a body; this is a heuristic that separates people from interference spikes, fans and
    (partly) pets. A large dog crossing the link can still look like a person.
    """

    def __init__(self, cfg: dict):
        detection = cfg["detection"]
        self.tick = detection["tick_seconds"]
        self.threshold = detection["human_threshold"]
        self.confirm_ticks = max(1, round(detection["confirm_seconds"] / self.tick))
        self.history = deque()
        self.streak = 0

    def update(self, now: float, motion: float, mech: float) -> dict:
        self.history.append((now, motion, mech))
        while self.history and self.history[0][0] < now - 6:
            self.history.popleft()
        recent = [m for t, m, _ in self.history if t >= now - 2]
        level = float(np.median(recent)) if recent else 0.0
        magnitude = min(max((level - 0.6) / 1.4, 0.0), 1.0)
        last4 = [m for t, m, _ in self.history if t >= now - 4]
        persistence = sum(m >= 0.8 for m in last4) / max(len(last4), round(4 / self.tick))
        machine = [k for t, m, k in self.history if t >= now - 3 and m >= 0.5]
        machine_share = float(np.mean(machine)) if machine else 0.0
        likelihood = min(max(0.55 * magnitude + 0.45 * persistence - 0.6 * machine_share, 0.0), 1.0)
        self.streak = self.streak + 1 if likelihood >= self.threshold else 0
        return {
            "likelihood": round(likelihood, 3),
            "confirmed": self.streak >= self.confirm_ticks,
            "machine": round(machine_share, 3),
        }

    def reset(self):
        self.history.clear()
        self.streak = 0


class PresenceTracker:
    """learning → empty → motion ⇄ presence → empty, with trigger persistence and a hold time.

    "presence" means somebody was moving recently or still shifts the link level: people who sit still
    keep the episode open through the level shift (strongest with CSI) until hold_seconds of calm.
    """

    def __init__(self, cfg: dict):
        detection = cfg["detection"]
        self.tick = detection["tick_seconds"]
        self.trigger = detection["trigger_seconds"]
        self.hold = detection["hold_seconds"]
        self.static_max = detection["static_max_seconds"]
        self.release = 0.6
        self.state = "learning"
        self.above_since = None
        self.last_active = None
        self.last_motion = None
        self.episode = None

    def update(self, now: float, motion: float, presence: float, human: dict, ready: bool, calibrating: bool):
        """Returns (started, ended): the episode dict that began or finished on this tick, or None."""
        started = ended = None
        if calibrating or not ready:
            if self.episode is not None:
                ended, self.episode = self.episode, None
                ended["end"] = now
                ended["reason"] = "calibrating" if calibrating else "no_signal"
            self.state = "calibrating" if calibrating else "learning"
            self.above_since = None
            return started, ended
        if self.state in ("learning", "calibrating"):
            self.state = "empty"
        above = motion >= 1.0
        if above:
            self.above_since = now if self.above_since is None else self.above_since
        else:
            self.above_since = None
        if self.state == "empty":
            # Each tick stands for one tick of time: trigger_seconds=1 at 0.5 s ticks means two ticks in a row.
            if above and now - self.above_since + self.tick >= self.trigger - 1e-9:
                self.state = "motion"
                self.last_active = self.last_motion = now
                started = self.episode = {
                    "start": self.above_since, "peak_motion": 0.0, "peak_presence": 0.0, "human": 0.0,
                    "confirmed": False, "links": {}, "alarm": False, "armed": False, "position": None,
                }
        elif self.state == "motion":
            if motion >= self.release:
                self.last_active = self.last_motion = now
            else:
                self.state = "presence"
        elif self.state == "presence":
            # A level shift alone keeps the episode only for static_max: after that it is the new empty room
            # (a door left open, moved furniture) and the caller re-anchors the baseline.
            static = presence >= 1.0 and now - self.last_motion < self.static_max
            if above:
                self.state = "motion"
                self.last_active = self.last_motion = now
            elif static:
                self.last_active = now
            elif now - self.last_active >= self.hold or presence >= 1.0:
                self.state = "empty"
                ended, self.episode = self.episode, None
                if ended is not None:
                    ended["end"] = now
                    ended["reason"] = "static" if presence >= 1.0 else "calm"
        if self.episode is not None:
            episode = self.episode
            episode["peak_motion"] = max(episode["peak_motion"], motion)
            episode["peak_presence"] = max(episode["peak_presence"], presence)
            episode["human"] = max(episode["human"], human["likelihood"])
            episode["confirmed"] = episode["confirmed"] or human["confirmed"]
        return started, ended


def finite(value, digits=3):
    return round(value, digits) if isinstance(value, (int, float)) and math.isfinite(value) else None
