"""Simulated room for development and the demo: a person, a fan and a cat disturb every configured link.

Physics is simplified but of the right kind: a line-of-sight path that the body shadows inside the Fresnel
zone, fixed wall reflections, and moving body scatterers (torso, head, swinging limbs) whose path lengths
set the phase per OFDM subcarrier. RSSI links get firmware-like smoothing and integer dBm; CSI links get
per-packet amplitudes with automatic gain control steps.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

from ..config import node_map

log = logging.getLogger(__name__)
C = 299_792_458.0
SUBCARRIERS = np.array([k for k in range(-26, 27) if k != 0])
STEP = 0.02  # 50 Hz world clock
RATES = {"rssi": 10, "esp32": 50}
ACTIONS = ("auto", "enter", "walk", "still", "leave", "fan", "cat")
# The automatic demo: (scene, duration range in seconds); "leave" lasts until the person is out.
AUTO_SCRIPT = [("enter", (14, 24)), ("still", (18, 32)), ("walk", (8, 14)), ("leave", None), ("absent", (40, 70))]


def segment_distance(p1, q1, p2, q2):
    """Closest distance between segments p1q1 and p2q2 and the parameter s along the first one."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = d1 @ d1, d2 @ d2, d2 @ r
    if a <= 1e-12:
        return float(np.linalg.norm(r)), 0.0
    c = d1 @ r
    if e <= 1e-12:
        s, t = min(max(-c / a, 0), 1), 0.0
    else:
        b = d1 @ d2
        denom = a * e - b * b
        s = min(max((b * f - c * e) / denom, 0), 1) if denom > 1e-12 else 0.0
        t = (b * s + f) / e
        if t < 0:
            t, s = 0.0, min(max(-c / a, 0), 1)
        elif t > 1:
            t, s = 1.0, min(max((b - c) / a, 0), 1)
    return float(np.linalg.norm(p1 + d1 * s - (p2 + d2 * t))), float(s)


class Walker:
    """A body that wanders between random waypoints inside the room."""

    def __init__(self, rng, room, height, radius, speed, door):
        self.rng, self.room = rng, room
        self.height, self.radius, self.speed = height, radius, speed
        self.door = np.array(door, dtype=float)
        self.pos = self.door.copy()
        self.target = self.door.copy()
        self.present = False
        self.moving = False
        self.leaving = False
        self.pause_until = 0.0
        self.gait = 0.0
        self.velocity = np.zeros(2)

    def waypoint(self):
        margin = 0.45
        return np.array([self.rng.uniform(margin, self.room["width"] - margin),
                         self.rng.uniform(margin, self.room["depth"] - margin)])

    def step(self, now, dt, pause_chance=0.15):
        self.velocity[:] = 0
        if not self.present or not self.moving or now < self.pause_until:
            return
        delta = self.target - self.pos
        distance = float(np.hypot(*delta))
        if distance < 0.1:
            if self.leaving:
                self.present = self.moving = self.leaving = False
                return
            self.target = self.waypoint()
            if self.rng.random() < pause_chance:
                self.pause_until = now + self.rng.uniform(0.4, 1.5)
            return
        speed = self.speed * (0.85 + 0.3 * math.sin(now * 0.7))
        move = delta / distance * min(speed * dt, distance)
        self.pos += move
        self.velocity = move / dt
        self.gait += 2 * math.pi * 1.8 * dt


class SimWorld:
    name = "sim"

    def __init__(self, cfg: dict, buffers: dict, clock=time.monotonic, seed=None):
        self.cfg = cfg
        self.buffers = buffers
        self.clock = clock
        seed = cfg["sim"].get("seed") if seed is None else seed
        self.rng = np.random.default_rng(seed)
        self.room = cfg["room"]
        nodes = node_map(cfg)
        door = self._door(nodes)
        self.person = Walker(self.rng, self.room, 1.75, 0.22, 1.1, door)
        self.cat = Walker(self.rng, self.room, 0.3, 0.1, 0.6, door)
        self.cat.pos = self.cat.waypoint()
        self.fan_on = False
        self.fan_pos = np.array([self.room["width"] * 0.8, 1.0, self.room["depth"] * 0.36])
        self.fan_angle = 0.0
        self.auto = bool(cfg["sim"]["auto"])
        self.scene = "absent"
        self.scene_until = None
        self.script_index = len(AUTO_SCRIPT) - 1
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="sim-world", daemon=True)
        self.links = []
        for link in cfg["links"]:
            tx = np.array(nodes[link["tx"]]["position"], dtype=float)
            rx = np.array(nodes[link["rx"]]["position"], dtype=float)
            ghz = float(link.get("ghz", 2.4))
            reflectors = []
            for _ in range(4):
                point = np.array([self.rng.uniform(0, self.room["width"]), self.rng.uniform(0.2, self.room["height"]),
                                  self.rng.uniform(0, self.room["depth"])])
                reflectors.append((point, self.rng.uniform(0.25, 0.55), self.rng.uniform(0, 2 * math.pi)))
            self.links.append({
                "id": link["id"], "kind": "csi" if link["source"] == "esp32" else "rssi", "tx": tx, "rx": rx,
                "length": float(np.linalg.norm(rx - tx)), "freqs": ghz * 1e9 + SUBCARRIERS * 312.5e3,
                "wavelength": C / (ghz * 1e9), "reflectors": reflectors, "smooth": None, "gain": 1.0,
                "next": 0.0, "p0": -38.0 if ghz < 3 else -44.0, "ghz": ghz,
            })

    def _door(self, nodes):
        door = next((n["position"] for n in nodes.values() if "door" in n["id"] or "ajto" in n["id"]), None)
        if door is not None:
            return [min(max(door[0], 0.4), self.room["width"] - 0.4), min(max(door[2], 0.4), self.room["depth"] - 0.4)]
        return [0.4, self.room["depth"] - 0.4]

    # Control -------------------------------------------------------------------------------------
    def start(self):
        self.next_tick = self.clock()
        self.thread.start()

    def close(self, timeout=3):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def action(self, name: str) -> dict:
        if name not in ACTIONS:
            raise ValueError(f"unknown action {name}")
        with self.lock:
            now = self.clock()
            if name == "auto":
                self.auto = True
                self.scene_until = now
            elif name == "fan":
                self.fan_on = not self.fan_on
            elif name == "cat":
                self.cat.present = not self.cat.present
                self.cat.moving = self.cat.present
            else:
                self.auto = False
                self._scene(name, now)
        return self.state()

    def _scene(self, name, now):
        person = self.person
        if name == "enter":
            if not person.present:
                person.pos = person.door.copy()
            person.present, person.moving, person.leaving = True, True, False
            person.target = person.waypoint()
            name = "walk"
        elif name == "walk":
            if not person.present:
                person.pos = person.door.copy()
            person.present, person.moving, person.leaving = True, True, False
            person.target = person.waypoint()
        elif name == "still":
            if not person.present:
                person.pos = person.waypoint()
            person.present, person.moving, person.leaving = True, False, False
        elif name == "absent":
            person.present = person.moving = person.leaving = False
        elif name == "leave":
            if person.present:
                person.moving, person.leaving = True, True
                person.target = person.door.copy()
                person.pause_until = 0
            else:
                name = "absent"
        self.scene = name

    def _auto_step(self, now):
        if not self.auto:
            return
        if self.scene_until is None:  # start with an empty room so the baseline can be learned
            self.scene_until = now + 30
        if now < self.scene_until or (self.scene == "leave" and self.person.present):
            return
        self.script_index = (self.script_index + 1) % len(AUTO_SCRIPT)
        name, span = AUTO_SCRIPT[self.script_index]
        self._scene(name, now)
        self.scene_until = now + (self.rng.uniform(*span) if span else 0)

    def state(self) -> dict:
        person, cat = self.person, self.cat
        return {
            "auto": self.auto,
            "scene": self.scene,
            "person": [round(float(person.pos[0]), 2), 0.0, round(float(person.pos[1]), 2)] if person.present else None,
            "moving": bool(person.present and person.moving),
            "fan": self.fan_on,
            "cat": [round(float(cat.pos[0]), 2), 0.0, round(float(cat.pos[1]), 2)] if cat.present else None,
        }

    # Physics -------------------------------------------------------------------------------------
    def _scatterers(self, now):
        """(position, reflectivity) of every moving body part, the cat and the fan blades."""
        points = []
        person = self.person
        if person.present:
            x, z = person.pos
            heading = person.velocity / (np.linalg.norm(person.velocity) + 1e-9)
            swing = math.sin(person.gait) * (0.25 if np.linalg.norm(person.velocity) > 0.05 else 0.0)
            breath = 0.006 * math.sin(2 * math.pi * 0.25 * now)
            side = np.array([-heading[1], heading[0]])
            parts = [
                (np.array([x + heading[0] * breath, 1.25, z + heading[1] * breath]), 0.30),
                (np.array([x, 1.65, z]), 0.12),
                (np.array([x + side[0] * 0.12 + heading[0] * swing, 0.5, z + side[1] * 0.12 + heading[1] * swing]), 0.14),
                (np.array([x - side[0] * 0.12 - heading[0] * swing, 0.5, z - side[1] * 0.12 - heading[1] * swing]), 0.14),
                (np.array([x + side[0] * 0.25 - heading[0] * swing * 0.8, 1.1, z + side[1] * 0.25 - heading[1] * swing * 0.8]), 0.08),
                (np.array([x - side[0] * 0.25 + heading[0] * swing * 0.8, 1.1, z - side[1] * 0.25 + heading[1] * swing * 0.8]), 0.08),
            ]
            points.extend(parts)
        cat = self.cat
        if cat.present:
            points.append((np.array([cat.pos[0], 0.2, cat.pos[1]]), 0.05))
        if self.fan_on:
            for blade in range(3):
                angle = self.fan_angle + blade * 2 * math.pi / 3
                points.append((self.fan_pos + np.array([0.25 * math.cos(angle), 0.25 * math.sin(angle), 0.0]), 0.05))
        return points

    def _shadow(self, link, body: Walker, depth_db):
        if not body.present:
            return 0.0
        bottom = np.array([body.pos[0], 0.0, body.pos[1]])
        top = np.array([body.pos[0], body.height, body.pos[1]])
        distance, s = segment_distance(link["tx"], link["rx"], bottom, top)
        d1, d2 = max(s * link["length"], 0.05), max((1 - s) * link["length"], 0.05)
        fresnel = math.sqrt(link["wavelength"] * d1 * d2 / (d1 + d2))
        return depth_db * math.exp(-((distance / (fresnel + body.radius)) ** 2))

    def _channel(self, link, scatterers):
        tx, rx, freqs = link["tx"], link["rx"], link["freqs"]
        shadow_db = self._shadow(link, self.person, 6.5 if link["ghz"] < 3 else 8.5) + self._shadow(link, self.cat, 1.2)
        h = np.full(len(freqs), 10 ** (-shadow_db / 20), dtype=np.complex128) * np.exp(-2j * np.pi * freqs * link["length"] / C)
        for point, rho, phase in link["reflectors"]:
            path = np.linalg.norm(point - tx) + np.linalg.norm(rx - point)
            h += rho * link["length"] / path * np.exp(-2j * np.pi * freqs * path / C + 1j * phase)
        for point, rho in scatterers:
            d1, d2 = np.linalg.norm(point - tx), np.linalg.norm(rx - point)
            amplitude = min(rho * link["length"] / max(d1 * d2, 0.05), 0.6)
            h += amplitude * np.exp(-2j * np.pi * freqs * (d1 + d2) / C)
        return h

    def _sample(self, link, now, scatterers):
        h = self._channel(link, scatterers)
        power_db = 10 * math.log10(float(np.mean(np.abs(h) ** 2)) + 1e-12)
        level = link["p0"] - 20 * math.log10(max(link["length"], 0.3)) + power_db
        buffer = self.buffers[link["id"]]
        if link["kind"] == "rssi":
            # Firmware RSSI is an average over recent frames: slow, then rounded to whole dBm.
            link["smooth"] = level if link["smooth"] is None else link["smooth"] + 0.22 * (level - link["smooth"])
            value = link["smooth"] + self.rng.normal(0, 0.45)
            if self.rng.random() < 0.002:  # occasional interference glitch
                value += self.rng.choice([-1, 1]) * self.rng.uniform(2, 4)
            buffer.append(float(round(value)), t=now)
        else:
            link["gain"] = min(max(link["gain"] * math.exp(self.rng.normal(0, 0.01)), 0.8), 1.25)
            amps = np.abs(h) * 22 * link["gain"] + self.rng.normal(0, 0.35, len(h))
            buffer.append(float(round(level + self.rng.normal(0, 0.8))), np.clip(amps, 0, None), t=now)

    def _advance(self, now):
        self._auto_step(now)
        self.person.step(now, STEP)
        self.cat.step(now, STEP, pause_chance=0.6)
        if self.cat.present and now >= self.cat.pause_until and self.rng.random() < 0.01:
            self.cat.pause_until = now + self.rng.uniform(2, 6)  # cats sit down a lot
        if self.fan_on:
            self.fan_angle += 2 * math.pi * 3.0 * STEP  # three blades at 3 rev/s: 9 Hz
        scatterers = self._scatterers(now)
        for link in self.links:
            if now >= link["next"]:
                self._sample(link, now, scatterers)
                rate = RATES["esp32" if link["kind"] == "csi" else "rssi"]
                # Due on the world step nearest to the rate; a few frames get lost, as over the air.
                link["next"] = now + 1 / rate - STEP / 2 + (STEP if self.rng.random() < 0.04 else 0)

    def _run(self):
        for link in self.links:
            self.buffers[link["id"]].set_state("starting", "szimuláció", kind=link["kind"], transport="sim")
        while not self.stopping.is_set():
            with self.lock:
                self._advance(self.clock())
            self.next_tick += STEP
            delay = self.next_tick - self.clock()
            if delay < -1:
                self.next_tick = self.clock()
            if self.stopping.wait(max(0.0, delay)):
                return

    def run_for(self, seconds: float, start: float = 0.0) -> float:
        """Advance the world synchronously on virtual time (tests); returns the new time."""
        now = start
        while now < start + seconds:
            self._advance(now)
            now += STEP
        return now
