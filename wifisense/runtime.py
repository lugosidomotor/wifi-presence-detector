"""Wires sources, detection, alarm and storage together; one tick thread owns all detection state."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from .buffer import LinkBuffer
from .config import public_layout
from .detector import Baseline, HumanScorer, LinkDetector, PresenceTracker, finite
from .events import EventStore
from .features import extract
from .localize import Localizer
from .notifier import Notifier
from .settings import Settings
from .sources import SimWorld, build_sources
from .utils import TaskWorker

log = logging.getLogger(__name__)
HISTORY_SECONDS = 600
CLEANUP_SECONDS = 3600
PERSIST_SECONDS = 10


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class Runtime:
    def __init__(self, cfg: dict, sources=None, clock=time.monotonic, wall=time.time):
        self.cfg = cfg
        self.clock, self.wall = clock, wall
        self.detection = cfg["detection"]
        self.tick = self.detection["tick_seconds"]
        self.simulated = cfg["source"]["mode"] == "sim"
        self.data_dir = Path(cfg["data_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.data_dir)
        self.settings = Settings(self.data_dir / "wifi-settings.json", cfg["alarm"]["arm_delay_seconds"], clock=wall)
        self.layout = public_layout(cfg)
        self.links = cfg["links"]
        self.kinds = {link["id"]: "csi" if link["source"] == "esp32" else "rssi" for link in self.links}
        self.names = {link["id"]: link["name"] for link in self.links}
        self.buffers = {link["id"]: LinkBuffer(clock=clock) for link in self.links}
        self.sources = build_sources(cfg, self.buffers, clock=clock) if sources is None else sources
        self.sim = next((s for s in self.sources if isinstance(s, SimWorld)), None)
        # Simulated and real calibrations never overwrite each other.
        self.calibration_path = self.data_dir / ("wifi-calibration-sim.json" if self.simulated else "wifi-calibration.json")
        self.detectors = {link["id"]: LinkDetector(cfg) for link in self.links}
        self._load_calibration()
        self.human = HumanScorer(cfg)
        self.tracker = PresenceTracker(cfg)
        self.localizer = Localizer(cfg)
        self.notifier = Notifier(cfg, self.names)
        self.worker = TaskWorker("wifi-notify", self._deliver)
        self.lock = threading.Lock()
        self.history = deque(maxlen=int(HISTORY_SECONDS / self.tick))
        self.latest = None
        self.seq = 0
        self.calibration = None
        self.calibration_request = None
        self.calibration_result = None
        self.event = None  # the open episode's stored payload
        self.last_alarm_sent = float("-inf")
        self.last_cleanup = float("-inf")
        self.last_persist = 0.0
        self.started = clock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="wifi-detector", daemon=True)

    # Lifecycle ------------------------------------------------------------------------------------
    def start(self):
        interrupted = self.store.close_interrupted()
        if interrupted:
            log.info("closed %d interrupted episode(s)", interrupted)
        for source in self.sources:
            source.start()
        self.worker.start()
        self.thread.start()
        log.info("wifi sensing started: %d link(s), mode=%s", len(self.links), self.cfg["source"]["mode"])

    def close(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        for source in self.sources:
            source.close()
        if self.event is not None and self.tracker.episode is not None:
            self._finish_event({**self.tracker.episode, "end": self.clock(), "reason": "shutdown"})
        self.worker.close()
        self.store.close()

    def _run(self):
        next_tick = self.clock()
        while not self.stopping.is_set():
            try:
                self.step(self.clock())
            except Exception:
                log.exception("detector tick failed")
            next_tick += self.tick
            delay = next_tick - self.clock()
            if delay < -2 * self.tick:  # overloaded or suspended: skip, do not burst
                next_tick = self.clock()
                delay = 0
            if self.stopping.wait(max(0.0, delay)):
                return

    # Calibration ----------------------------------------------------------------------------------
    def _load_calibration(self):
        try:
            data = json.loads(self.calibration_path.read_text())
        except (OSError, ValueError):
            return
        for link_id, entry in (data.get("links") or {}).items():
            if link_id in self.detectors and entry.get("kind") == self.kinds[link_id]:
                try:
                    self.detectors[link_id].set_baseline(Baseline.from_json(entry["baseline"]))
                except (KeyError, TypeError, ValueError):
                    log.warning("calibration of %s unreadable; learning automatically", link_id)

    def _save_calibration(self):
        payload = {"created": iso(self.wall()), "links": {
            link_id: {"kind": self.kinds[link_id], "baseline": detector.baseline.to_json()}
            for link_id, detector in self.detectors.items() if detector.calibrated
        }}
        tmp = self.calibration_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.calibration_path)

    def start_calibration(self, seconds: float | None = None) -> dict:
        seconds = float(seconds or self.cfg["calibration"]["seconds"])
        if not 10 <= seconds <= 600:
            raise ValueError("seconds must be between 10 and 600")
        with self.lock:
            self.calibration_request = seconds
            self.calibration_result = None
        return {"seconds": seconds}

    def cancel_calibration(self):
        with self.lock:
            self.calibration_request = "cancel"

    def _calibration_tick(self, now, feats):
        with self.lock:
            request, self.calibration_request = self.calibration_request, None
        if request == "cancel":
            self.calibration = None
            self.calibration_result = {"ok": False, "message": "Megszakítva"}
        elif request is not None:
            self.calibration = {"start": now, "end": now + request, "features": {i: [] for i in self.detectors}}
            log.info("calibration started for %.0fs", request)
        if self.calibration is None:
            return False
        for link_id, f in feats.items():
            if f is not None:
                self.calibration["features"][link_id].append(f)
        if now < self.calibration["end"]:
            return True
        done, missing = [], []
        for link_id, collected in self.calibration["features"].items():
            if len(collected) >= 10:
                baseline = Baseline.from_features(collected, "calibration", created=iso(self.wall()))
                self.detectors[link_id].set_baseline(baseline)
                done.append(link_id)
            else:
                missing.append(self.names[link_id])
        self.calibration = None
        if done:
            self._save_calibration()
        message = "Kész" if not missing else f"Kevés mérés: {', '.join(missing)}"
        self.calibration_result = {"ok": bool(done) and not missing, "message": message, "at": iso(self.wall())}
        self.human.reset()
        log.info("calibration finished: %s", message)
        return False

    # Detection tick -------------------------------------------------------------------------------
    def step(self, now: float):
        window = self.detection["window_seconds"]
        stale = self.detection["stale_seconds"]
        feats, infos = {}, {}
        for link_id, buffer in self.buffers.items():
            infos[link_id] = buffer.info(stale, now)
            times, rssi, amps = buffer.window(window, now)
            feats[link_id] = extract(times, rssi, amps) if infos[link_id]["state"] == "ok" else None
        calibrating = self._calibration_tick(now, feats)
        empty = self.tracker.state == "empty" and not calibrating
        scores = {}
        for link_id, detector in self.detectors.items():
            if calibrating:
                scores[link_id] = {"motion": 0.0, "presence": 0.0, "mech": 0.0, "valid": False}
            else:
                scores[link_id] = detector.update(feats[link_id], empty=empty, created=iso(self.wall()))
        valid = {i: s for i, s in scores.items() if s["valid"]}
        online = any(info["state"] == "ok" for info in infos.values())
        motion = max((s["motion"] for s in valid.values()), default=0.0)
        presence = max((s["presence"] for s in valid.values()), default=0.0)
        loudest = max(valid, key=lambda i: valid[i]["motion"], default=None)
        mech = valid[loudest]["mech"] if loudest else 0.0
        human = self.human.update(now, motion, mech)
        started, ended = self.tracker.update(now, motion, presence, human, ready=bool(valid), calibrating=calibrating)
        heat, position = self.localizer.update(valid) if valid else (None, None)
        arming = self.settings.arming()
        if started is not None:
            self._open_event(started, arming)
        episode = self.tracker.episode
        if episode is not None:
            if not arming["armed"]:
                episode["alarm"] = False  # disarming acknowledges it; re-arming needs fresh activity to alarm again
            for link_id, s in valid.items():
                episode["links"][link_id] = round(max(episode["links"].get(link_id, 0.0), s["motion"]), 2)
            if position is not None and motion >= episode.get("position_motion", 0):
                episode["position"], episode["position_motion"] = position, motion
            episode["armed"] = episode["armed"] or arming["active"]
            self._maybe_alarm(episode, human, motion, arming)
            # The list in the UI reloads when the label flips: store it right away, not at the next persist.
            if now - self.last_persist >= PERSIST_SECONDS or (episode["confirmed"] and self.event["label"] != "ember"):
                self._persist(episode, now)
        if ended is not None:
            self._finish_event(ended)
            if ended.get("reason") == "static":
                for link_id, s in valid.items():
                    if s["presence"] >= 1.0:
                        self.detectors[link_id].reanchor()
        wall = self.wall()
        if wall - self.last_cleanup >= CLEANUP_SECONDS:
            self.last_cleanup = wall
            removed = self.store.cleanup(self.cfg["storage"]["retention_days"])
            if removed:
                log.info("retention removed %d episode(s)", removed)
        self._publish(now, wall, scores, infos, motion, presence, human, heat, position, arming, online)

    def _open_event(self, episode, arming):
        wall_start = self.wall() - (self.clock() - episode["start"])
        episode["id"] = uuid.uuid4().hex[:16]
        self.event = {
            "id": episode["id"], "timestamp": iso(wall_start), "state": "active", "label": "mozgás",
            "human": 0.0, "peak_motion": 0.0, "peak_presence": 0.0, "links": {}, "position": None,
            "armed": arming["active"], "alarm": False, "alarm_at": None, "notification_status": None,
            "simulated": self.simulated,
        }
        self.store.add(self.event)
        self.last_persist = self.clock()
        log.info("episode %s started", episode["id"])

    def _fields(self, episode):
        return {
            "label": "ember" if episode["confirmed"] else "mozgás",
            "human": finite(episode["human"]),
            "peak_motion": finite(episode["peak_motion"], 2),
            "peak_presence": finite(episode["peak_presence"], 2),
            "links": dict(episode["links"]),
            "position": episode["position"],
            "armed": episode["armed"],
        }

    def _persist(self, episode, now):
        self.last_persist = now
        self.event.update(self._fields(episode))
        self.store.update(self.event["id"], **self._fields(episode))

    def _finish_event(self, ended):
        if self.event is None:
            return
        fields = self._fields(ended)
        duration = max(0.0, ended["end"] - ended["start"])
        fields.update(state="closed", ended_at=iso(self.wall()), duration_seconds=round(duration, 1),
                      end_reason=ended.get("reason", "calm"))
        self.store.update(self.event["id"], **fields)
        log.info("episode %s ended after %.0fs (%s)", self.event["id"], duration, fields.get("label", "?"))
        self.event = None

    def _maybe_alarm(self, episode, human, motion, arming):
        if episode["alarm"] or not arming["active"]:
            return
        # Only fresh activity after the exit delay counts: the current tick must itself qualify.
        if self.cfg["alarm"]["trigger"] == "human":
            triggered = human["confirmed"]
        else:
            triggered = motion >= 1.0 and self.tracker.state == "motion"
        if not triggered:
            return
        episode["alarm"] = True
        wall = self.wall()
        self.event.update(self._fields(episode), alarm=True, alarm_at=iso(wall))
        if wall - self.last_alarm_sent < self.cfg["alarm"]["cooldown_seconds"]:
            status = "cooldown"
        elif self.notifier.enabled:
            status = "pending"
            self.last_alarm_sent = wall
            self.worker.submit(dict(self.event))
        else:
            status = "disabled"
        self.event["notification_status"] = status
        self.store.update(self.event["id"], **self._fields(episode), alarm=True, alarm_at=iso(wall),
                          notification_status=status)
        log.warning("ALARM: episode %s (%s, human %.0f%%)", episode["id"], self.event["label"], 100 * episode["human"])

    def _deliver(self, event):
        result = self.notifier.send_alarm(event)
        self.store.update(event["id"], notification_status=result)
        with self.lock:
            if self.event is not None and self.event["id"] == event["id"]:
                self.event["notification_status"] = result

    # Snapshots for the API ------------------------------------------------------------------------
    def _publish(self, now, wall, scores, infos, motion, presence, human, heat, position, arming, online):
        state = self.tracker.state if online else "offline"
        links = {}
        for link_id, score in scores.items():
            buffer_info = infos[link_id]
            _, rssi, _ = self.buffers[link_id].window(1.0, now)
            links[link_id] = {
                "motion": finite(score["motion"], 2),
                "presence": finite(score["presence"], 2),
                "mech": finite(score.get("mech", 0.0), 2),
                "valid": score["valid"],
                "rssi": finite(float(rssi.mean()), 1) if len(rssi) else None,
                "baseline": self._baseline_label(link_id),
                **buffer_info,
            }
        episode = self.tracker.episode
        calibration = None
        if self.calibration is not None:
            total = self.calibration["end"] - self.calibration["start"]
            calibration = {"progress": round(min(1.0, (now - self.calibration["start"]) / total), 3),
                           "remaining": round(max(0.0, self.calibration["end"] - now))}
        snapshot = {
            "t": round(wall, 2),
            "state": state,
            "motion": finite(motion, 2),
            "presence": finite(presence, 2),
            "human": human,
            "arming": arming,
            "alarm": bool(episode and episode["alarm"] and arming["armed"]),
            "episode": None if episode is None else {
                "id": episode.get("id"), "started": round(wall - (now - episode["start"]), 2),
                "label": "ember" if episode["confirmed"] else "mozgás", "human": finite(episode["human"]),
                "alarm": episode["alarm"],
            },
            "links": links,
            "heat": None if heat is None else {"rows": len(heat), "cols": len(heat[0]),
                                               "values": [round(float(v), 2) for v in heat.ravel()]},
            "position": position,
            "calibration": calibration,
            "calibration_result": self.calibration_result,
            "sim": self.sim.state() if self.sim else None,
        }
        compact = {"t": snapshot["t"], "s": "alarm" if snapshot["alarm"] else state,
                   "m": {i: link["motion"] for i, link in links.items()},
                   "r": {i: link["rssi"] for i, link in links.items()}}
        with self.lock:
            self.latest = snapshot
            self.history.append(compact)
            self.seq += 1

    def _baseline_label(self, link_id):
        detector = self.detectors[link_id]
        if detector.calibrated:
            return "calibrated"
        return "auto" if detector.ready else "learning"

    def snapshot(self):
        with self.lock:
            return self.seq, self.latest

    def live(self, seconds: float = 120):
        with self.lock:
            items = list(self.history)
        if not items:
            return []
        cutoff = items[-1]["t"] - seconds
        return [item for item in items if item["t"] >= cutoff]

    def status(self) -> dict:
        _, latest = self.snapshot()
        day_start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {
            "layout": self.layout,
            "mode": self.cfg["source"]["mode"],
            "simulated": self.simulated,
            "uptime_seconds": round(self.clock() - self.started),
            "notifications": {"ntfy": self.cfg["notifications"]["enabled"],
                              "webhook": bool(self.cfg["notifications"]["webhook_url"])},
            "alarm_trigger": self.cfg["alarm"]["trigger"],
            "thresholds": {k: self.detection[k] for k in ("motion_threshold", "presence_threshold", "human_threshold",
                                                          "trigger_seconds", "hold_seconds", "window_seconds")},
            "calibration_seconds": self.cfg["calibration"]["seconds"],
            "today_events": self.store.count_since(day_start),
            "latest": latest,
        }

    # Controls -------------------------------------------------------------------------------------
    def arm(self, armed: bool) -> dict:
        result = self.settings.set_armed(armed)
        log.info("system %s", "armed" if armed else "disarmed")
        return result

    def sim_action(self, name: str) -> dict:
        if self.sim is None:
            raise LookupError("not simulated")
        return self.sim.action(name)

    def test_notification(self) -> str:
        return self.notifier.send_test()
