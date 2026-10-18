/* Demo backend for the standalone page: the simulated room and the whole detection pipeline, in the browser.
   tools/build_demo.py inlines this ahead of the unchanged wifisense/static UI. It mirrors wifisense/sources/sim.py,
   features.py, detector.py, localize.py and runtime.py, so the page behaves like `python -m wifisense.main --demo`. */
(() => {
"use strict";

// ---------------------------------------------------------------------------------------------------------------
// Configuration: config.DEFAULTS with config.DEMO_LAYOUT (only the exit delay is shorter, to try arming quickly).
// ---------------------------------------------------------------------------------------------------------------
const ROOM = {width: 6.0, depth: 4.5, height: 2.6};
const NODES = [
  {id: "router", name: "Router", kind: "ap", position: [0.35, 1.9, 0.35]},
  {id: "pi", name: "Raspberry Pi", kind: "sensor", position: [5.6, 1.1, 4.1]},
  {id: "esp-window", name: "ESP32 · ablak", kind: "sensor", position: [5.7, 0.9, 0.5]},
  {id: "esp-door", name: "ESP32 · ajtó", kind: "sensor", position: [0.4, 1.0, 4.1]},
];
const LINKS = [
  {id: "pi-router", name: "Pi ↔ router", kind: "rssi", tx: "router", rx: "pi", ghz: 2.4},
  {id: "window-router", name: "Ablak ↔ router", kind: "csi", tx: "router", rx: "esp-window", ghz: 2.4},
  {id: "door-router", name: "Ajtó ↔ router", kind: "csi", tx: "router", rx: "esp-door", ghz: 2.4},
];
const DET = {
  tick: 0.5, window: 3, warmup: 20, motionThreshold: 4, presenceThreshold: 5, trigger: 1, hold: 45,
  humanThreshold: 0.6, confirm: 2, adapt: 600, stale: 5, staticMax: 600,
};
const ARM_DELAY = 15, CALIBRATION = 60, COOLDOWN = 120;
const C = 299792458, LIGHT = 0.2998, STEP = 0.02;
const SUBCARRIERS = Array.from({length: 53}, (_, i) => i - 26).filter(k => k !== 0);
const RATES = {rssi: 10, csi: 50};
const FLOORS = {rssi_std: 0.2, rssi_mean: 0.5, csi_std: 0.0025, csi_shift: 0.002};
const nodeById = Object.fromEntries(NODES.map(n => [n.id, n]));
const wall = () => Date.now() / 1000;

// ---------------------------------------------------------------------------------------------------------------
// Small numeric helpers (numpy equivalents).
// ---------------------------------------------------------------------------------------------------------------
function mulberry(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry(20261019);
const uniform = (a, b) => a + (b - a) * rand();
function normal(mu = 0, sigma = 1) {
  const u = 1 - rand(), v = rand();
  return mu + sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const norm = a => Math.sqrt(dot(a, a));
const dist = (a, b) => norm(sub(a, b));
const mean = xs => xs.reduce((s, x) => s + x, 0) / xs.length;
const std = xs => { const m = mean(xs); return Math.sqrt(mean(xs.map(x => (x - m) ** 2))); };
const sorted = xs => Float64Array.from(xs).sort();
function percentile(xs, p) {
  const s = sorted(xs), i = (p / 100) * (s.length - 1), lo = Math.floor(i), hi = Math.ceil(i);
  return s[lo] + (s[hi] - s[lo]) * (i - lo);
}
const median = xs => percentile(xs, 50);
const round = (v, digits = 3) => (Number.isFinite(v) ? Math.round(v * 10 ** digits) / 10 ** digits : null);

// ---------------------------------------------------------------------------------------------------------------
// Simulated room (sources/sim.py): a person, a fan and a cat disturb every link.
// ---------------------------------------------------------------------------------------------------------------
function segmentDistance(p1, q1, p2, q2) {
  const d1 = sub(q1, p1), d2 = sub(q2, p2), r = sub(p1, p2);
  const a = dot(d1, d1), e = dot(d2, d2), f = dot(d2, r);
  if (a <= 1e-12) return [norm(r), 0];
  const c = dot(d1, r);
  let s, t;
  if (e <= 1e-12) { s = clamp(-c / a, 0, 1); t = 0; }
  else {
    const b = dot(d1, d2), denom = a * e - b * b;
    s = denom > 1e-12 ? clamp((b * f - c * e) / denom, 0, 1) : 0;
    t = (b * s + f) / e;
    if (t < 0) { t = 0; s = clamp(-c / a, 0, 1); }
    else if (t > 1) { t = 1; s = clamp((b - c) / a, 0, 1); }
  }
  const pa = [p1[0] + d1[0] * s, p1[1] + d1[1] * s, p1[2] + d1[2] * s];
  const pb = [p2[0] + d2[0] * t, p2[1] + d2[1] * t, p2[2] + d2[2] * t];
  return [dist(pa, pb), s];
}

class Walker {
  constructor(height, radius, speed, door) {
    Object.assign(this, {height, radius, speed, door: door.slice(), pos: door.slice(), target: door.slice()});
    this.present = this.moving = this.leaving = false;
    this.pauseUntil = 0; this.gait = 0; this.velocity = [0, 0];
  }
  waypoint() {
    const m = 0.45;
    return [uniform(m, ROOM.width - m), uniform(m, ROOM.depth - m)];
  }
  step(now, dt, pauseChance = 0.15) {
    this.velocity = [0, 0];
    if (!this.present || !this.moving || now < this.pauseUntil) return;
    const delta = [this.target[0] - this.pos[0], this.target[1] - this.pos[1]];
    const distance = Math.hypot(delta[0], delta[1]);
    if (distance < 0.1) {
      if (this.leaving) { this.present = this.moving = this.leaving = false; return; }
      this.target = this.waypoint();
      if (rand() < pauseChance) this.pauseUntil = now + uniform(0.4, 1.5);
      return;
    }
    const speed = this.speed * (0.85 + 0.3 * Math.sin(now * 0.7)), stepLength = Math.min(speed * dt, distance);
    const move = [delta[0] / distance * stepLength, delta[1] / distance * stepLength];
    this.pos = [this.pos[0] + move[0], this.pos[1] + move[1]];
    this.velocity = [move[0] / dt, move[1] / dt];
    this.gait += 2 * Math.PI * 1.8 * dt;
  }
}

const AUTO_SCRIPT = [["enter", [14, 24]], ["still", [18, 32]], ["walk", [8, 14]], ["leave", null], ["absent", [40, 70]]];

class SimWorld {
  constructor(buffers) {
    this.buffers = buffers;
    const doorNode = nodeById["esp-door"].position;
    const door = [clamp(doorNode[0], 0.4, ROOM.width - 0.4), clamp(doorNode[2], 0.4, ROOM.depth - 0.4)];
    this.person = new Walker(1.75, 0.22, 1.1, door);
    this.cat = new Walker(0.3, 0.1, 0.6, door);
    this.cat.pos = this.cat.waypoint();
    this.fanOn = false;
    this.fanPos = [ROOM.width * 0.8, 1.0, ROOM.depth * 0.36];
    this.fanAngle = 0;
    this.auto = true;
    this.scene = "absent";
    this.sceneUntil = null;
    this.scriptIndex = AUTO_SCRIPT.length - 1;
    this.faults = {door: false, all: false, powerSave: false};
    this.links = LINKS.map(link => {
      const tx = nodeById[link.tx].position, rx = nodeById[link.rx].position;
      const reflectors = Array.from({length: 4}, () => ({
        p: [uniform(0, ROOM.width), uniform(0.2, ROOM.height), uniform(0, ROOM.depth)],
        rho: uniform(0.25, 0.55), phase: uniform(0, 2 * Math.PI),
      }));
      return {
        id: link.id, kind: link.kind, tx, rx, length: dist(tx, rx), ghz: link.ghz, reflectors,
        freqs: SUBCARRIERS.map(k => link.ghz * 1e9 + k * 312.5e3), wavelength: C / (link.ghz * 1e9),
        smooth: null, gain: 1, next: 0, p0: link.ghz < 3 ? -38 : -44,
      };
    });
  }

  action(name) {
    const now = wall();
    if (name === "auto") { this.auto = true; this.sceneUntil = now; }
    else if (name === "fan") this.fanOn = !this.fanOn;
    else if (name === "cat") { this.cat.present = !this.cat.present; this.cat.moving = this.cat.present; }
    else { this.auto = false; this.setScene(name); }
    return this.state();
  }

  setScene(name) {
    const person = this.person;
    if (name === "enter" || name === "walk") {
      if (!person.present) person.pos = person.door.slice();
      person.present = person.moving = true; person.leaving = false;
      person.target = person.waypoint();
      name = "walk";
    } else if (name === "still") {
      if (!person.present) person.pos = person.waypoint();
      person.present = true; person.moving = person.leaving = false;
    } else if (name === "absent") {
      person.present = person.moving = person.leaving = false;
    } else if (name === "leave") {
      if (person.present) { person.moving = person.leaving = true; person.target = person.door.slice(); person.pauseUntil = 0; }
      else name = "absent";
    }
    this.scene = name;
  }

  autoStep(now) {
    if (!this.auto) return;
    if (this.sceneUntil === null) this.sceneUntil = now + 30;
    if (now < this.sceneUntil || (this.scene === "leave" && this.person.present)) return;
    this.scriptIndex = (this.scriptIndex + 1) % AUTO_SCRIPT.length;
    const [name, span] = AUTO_SCRIPT[this.scriptIndex];
    this.setScene(name);
    this.sceneUntil = now + (span ? uniform(span[0], span[1]) : 0);
  }

  state() {
    const p = this.person, c = this.cat;
    return {
      auto: this.auto, scene: this.scene,
      person: p.present ? [round(p.pos[0], 2), 0, round(p.pos[1], 2)] : null,
      moving: p.present && p.moving, fan: this.fanOn,
      cat: c.present ? [round(c.pos[0], 2), 0, round(c.pos[1], 2)] : null,
    };
  }

  scatterers(now) {
    const points = [], person = this.person;
    if (person.present) {
      const [x, z] = person.pos, speed = Math.hypot(person.velocity[0], person.velocity[1]);
      const heading = [person.velocity[0] / (speed + 1e-9), person.velocity[1] / (speed + 1e-9)];
      const swing = Math.sin(person.gait) * (speed > 0.05 ? 0.25 : 0);
      const breath = 0.006 * Math.sin(2 * Math.PI * 0.25 * now);
      const side = [-heading[1], heading[0]];
      points.push(
        [[x + heading[0] * breath, 1.25, z + heading[1] * breath], 0.30],
        [[x, 1.65, z], 0.12],
        [[x + side[0] * 0.12 + heading[0] * swing, 0.5, z + side[1] * 0.12 + heading[1] * swing], 0.14],
        [[x - side[0] * 0.12 - heading[0] * swing, 0.5, z - side[1] * 0.12 - heading[1] * swing], 0.14],
        [[x + side[0] * 0.25 - heading[0] * swing * 0.8, 1.1, z + side[1] * 0.25 - heading[1] * swing * 0.8], 0.08],
        [[x - side[0] * 0.25 + heading[0] * swing * 0.8, 1.1, z - side[1] * 0.25 + heading[1] * swing * 0.8], 0.08],
      );
    }
    if (this.cat.present) points.push([[this.cat.pos[0], 0.2, this.cat.pos[1]], 0.05]);
    if (this.fanOn) {
      for (let blade = 0; blade < 3; blade++) {
        const angle = this.fanAngle + blade * 2 * Math.PI / 3;
        points.push([[this.fanPos[0] + 0.25 * Math.cos(angle), this.fanPos[1] + 0.25 * Math.sin(angle), this.fanPos[2]], 0.05]);
      }
    }
    return points;
  }

  shadow(link, body, depthDb) {
    if (!body.present) return 0;
    const [distance, s] = segmentDistance(link.tx, link.rx, [body.pos[0], 0, body.pos[1]], [body.pos[0], body.height, body.pos[1]]);
    const d1 = Math.max(s * link.length, 0.05), d2 = Math.max((1 - s) * link.length, 0.05);
    const fresnel = Math.sqrt(link.wavelength * d1 * d2 / (d1 + d2));
    return depthDb * Math.exp(-((distance / (fresnel + body.radius)) ** 2));
  }

  sample(link, now, scatterers) {
    const n = link.freqs.length, re = new Float64Array(n), im = new Float64Array(n);
    const shadowDb = this.shadow(link, this.person, link.ghz < 3 ? 6.5 : 8.5) + this.shadow(link, this.cat, 1.2);
    const add = (amplitude, path, phase = 0) => {
      for (let k = 0; k < n; k++) {
        const angle = -2 * Math.PI * link.freqs[k] * path / C + phase;
        re[k] += amplitude * Math.cos(angle);
        im[k] += amplitude * Math.sin(angle);
      }
    };
    add(10 ** (-shadowDb / 20), link.length);
    for (const r of link.reflectors) {
      const path = dist(r.p, link.tx) + dist(link.rx, r.p);
      add(r.rho * link.length / path, path, r.phase);
    }
    for (const [point, rho] of scatterers) {
      const d1 = dist(point, link.tx), d2 = dist(link.rx, point);
      add(Math.min(rho * link.length / Math.max(d1 * d2, 0.05), 0.6), d1 + d2);
    }
    let power = 0;
    const amplitude = new Float64Array(n);
    for (let k = 0; k < n; k++) { const p = re[k] * re[k] + im[k] * im[k]; power += p; amplitude[k] = Math.sqrt(p); }
    const level = link.p0 - 20 * Math.log10(Math.max(link.length, 0.3)) + 10 * Math.log10(power / n + 1e-12);
    const buffer = this.buffers[link.id];
    if (link.kind === "rssi") {
      // Firmware RSSI is an average over recent frames: slow, then rounded to whole dBm.
      link.smooth = link.smooth === null ? level : link.smooth + 0.22 * (level - link.smooth);
      let value = link.smooth + normal(0, 0.45);
      if (rand() < 0.002) value += (rand() < 0.5 ? -1 : 1) * uniform(2, 4);
      buffer.append(Math.round(value), null, now);
    } else {
      link.gain = clamp(link.gain * Math.exp(normal(0, 0.01)), 0.8, 1.25);
      const amps = amplitude.map(a => Math.max(0, a * 22 * link.gain + normal(0, 0.35)));
      buffer.append(Math.round(level + normal(0, 0.8)), amps, now);
    }
  }

  advance(now) {
    this.autoStep(now);
    this.person.step(now, STEP);
    this.cat.step(now, STEP, 0.6);
    if (this.cat.present && now >= this.cat.pauseUntil && rand() < 0.01) this.cat.pauseUntil = now + uniform(2, 6);
    if (this.fanOn) this.fanAngle += 2 * Math.PI * 3 * STEP;
    const scatterers = this.scatterers(now);
    for (const link of this.links) {
      if (now < link.next) continue;
      link.next = now + 1 / RATES[link.kind] - STEP / 2 + (rand() < 0.04 ? STEP : 0);
      const buffer = this.buffers[link.id];
      if (this.faults.all || (this.faults.door && link.id === "door-router")) {
        buffer.setState("error", link.id === "door-router" && !this.faults.all
          ? "USB-soros port nem elérhető (/dev/ttyUSB0)" : "wlan0: nincs WiFi-kapcsolat");
        continue;
      }
      this.sample(link, now, scatterers);
    }
    this.buffers["pi-router"].meta.power_save = this.faults.powerSave || null;
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Sample buffer (buffer.py) and window features (features.py).
// ---------------------------------------------------------------------------------------------------------------
class LinkBuffer {
  constructor(kind) {
    this.samples = []; this.last = null; this.state = "starting"; this.message = "";
    this.meta = {kind, transport: "sim", interface: kind === "rssi" ? "wlan0" : undefined};
  }
  append(rssi, amps, t) {
    this.samples.push({t, rssi, amps});
    this.last = t;
    if (this.state !== "ok") { this.state = "ok"; this.message = ""; }
    if (this.samples.length > 2000 && this.samples[0].t < t - 20) this.samples = this.samples.filter(s => s.t >= t - 12);
  }
  setState(state, message = "") { this.state = state; this.message = message; }
  window(seconds, now) {
    const picked = [];
    for (let i = this.samples.length - 1; i >= 0 && this.samples[i].t >= now - seconds; i--) picked.push(this.samples[i]);
    picked.reverse();
    const withRssi = picked.filter(s => s.rssi !== null);
    let amps = null;
    const csi = picked.filter(s => s.amps).map(s => s.amps);
    if (csi.length) {
      const counts = new Map();
      for (const a of csi) counts.set(a.length, (counts.get(a.length) || 0) + 1);
      const newest = csi[csi.length - 1].length;
      let width = newest;
      for (const [w, count] of counts) if (count > counts.get(width) || (count === counts.get(width) && w === newest)) width = w;
      const same = csi.filter(a => a.length === width);
      amps = same.length >= 2 ? same : null;
    }
    return {times: withRssi.map(s => s.t), rssi: withRssi.map(s => s.rssi), amps};
  }
  rate(now, seconds = 5) {
    let count = 0;
    for (let i = this.samples.length - 1; i >= 0 && this.samples[i].t >= now - seconds; i--) count++;
    return count / seconds;
  }
  info(now) {
    let {state, message} = this;
    if (state === "ok" && (this.last === null || now - this.last > DET.stale)) { state = "stale"; message = "Nincs friss mérés"; }
    return {state, message, rate: round(this.rate(now), 1), ...this.meta};
  }
}

function uniformSeries(times, values) {
  const duration = times[times.length - 1] - times[0];
  if (duration <= 0) return [values, 0];
  const n = times.length, out = new Float64Array(n);
  let j = 0;
  for (let i = 0; i < n; i++) {
    const t = times[0] + duration * i / (n - 1);
    while (j < n - 2 && times[j + 1] < t) j++;
    const span = times[j + 1] - times[j];
    out[i] = span > 0 ? values[j] + (values[j + 1] - values[j]) * clamp((t - times[j]) / span, 0, 1) : values[j];
  }
  return [out, (n - 1) / duration];
}

function mechanical(times, values, minHz = 1.5) {
  const n = values.length;
  if (n < 16) return 0;
  const [series, rate] = uniformSeries(times, values);
  if (rate <= 2 * minHz) return 0;
  // Linear detrend (numpy polyfit, degree 1), Hann window, power spectrum.
  const mx = (n - 1) / 2, my = mean(Array.from(series));
  let sxy = 0, sxx = 0;
  for (let i = 0; i < n; i++) { sxy += (i - mx) * (series[i] - my); sxx += (i - mx) ** 2; }
  const slope = sxy / sxx, x = new Float64Array(n);
  for (let i = 0; i < n; i++) x[i] = (series[i] - (my + slope * (i - mx))) * (0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)));
  let everything = 0, fastSum = 0, fastMax = 0, fastCount = 0;
  for (let k = 0; k <= Math.floor(n / 2); k++) {
    let re = 0, im = 0;
    for (let i = 0; i < n; i++) { const a = 2 * Math.PI * k * i / n; re += x[i] * Math.cos(a); im -= x[i] * Math.sin(a); }
    const power = re * re + im * im, freq = k * rate / n;
    if (freq > 0.1) everything += power;
    if (freq >= minHz) { fastSum += power; fastMax = Math.max(fastMax, power); fastCount++; }
  }
  if (fastCount < 4 || everything <= 1e-12) return 0;
  const flat = 3 / fastCount;
  if (flat >= 0.4) return 0;
  return (fastSum / everything) * clamp((fastMax / fastSum - flat) / (0.4 - flat), 0, 1);
}

function extract(times, rssi, amps) {
  if (rssi.length < 5 && (!amps || amps.length < 5)) return null;
  const feats = {samples: Math.max(rssi.length, amps ? amps.length : 0)};
  let mechTimes = null, mechValues = null;
  if (rssi.length >= 5) {
    feats.rssi_mean = mean(rssi);
    feats.rssi_std = std(rssi);
    mechTimes = times; mechValues = rssi;
  }
  if (amps && amps.length >= 5) {
    const rows = amps.length, k = amps[0].length;
    // Per-packet normalisation removes the receiver's automatic gain control steps.
    const normalized = amps.map(row => { let level = mean(Array.from(row)); if (level <= 1e-6) level = 1; return row.map(v => v / level); });
    const profile = new Float64Array(k);
    for (const row of normalized) for (let j = 0; j < k; j++) profile[j] += row[j] / rows;
    let spread = 0;
    for (let j = 0; j < k; j++) {
      let s = 0;
      for (const row of normalized) s += (row[j] - profile[j]) ** 2;
      spread += Math.sqrt(s / rows);
    }
    feats.csi_std = spread / k;
    feats.csi_profile = profile;
    // First principal component by power iteration (the SVD's leading vector in features.py).
    const centered = normalized.map(row => row.map((v, j) => v - profile[j]));
    const cov = Array.from({length: k}, () => new Float64Array(k));
    for (const row of centered) for (let i = 0; i < k; i++) { const ri = row[i]; for (let j = 0; j < k; j++) cov[i][j] += ri * row[j]; }
    let v = new Float64Array(k).fill(1 / Math.sqrt(k));
    for (let iteration = 0; iteration < 30; iteration++) {
      const w = new Float64Array(k);
      for (let i = 0; i < k; i++) { let s = 0; for (let j = 0; j < k; j++) s += cov[i][j] * v[j]; w[i] = s; }
      const length = Math.hypot(...w) || 1;
      v = w.map(x => x / length);
    }
    const principal = centered.map(row => row.reduce((s, x, j) => s + x * v[j], 0));
    mechTimes = times.length >= 2
      ? principal.map((_, i) => times[0] + (times[times.length - 1] - times[0]) * i / Math.max(principal.length - 1, 1))
      : principal.map((_, i) => i / 50);
    mechValues = principal;
  }
  feats.mech = mechValues ? mechanical(mechTimes, mechValues) : 0;
  return feats;
}

function profileShift(profile, reference) {
  if (!profile || !reference || profile.length !== reference.length) return null;
  const limit = 0.05 * Math.max(mean(Array.from(reference)), 1e-9);
  const a = [], b = [];
  for (let i = 0; i < reference.length; i++) if (reference[i] > limit) { a.push(profile[i]); b.push(reference[i]); }
  if (a.length < 4) return null;
  const ma = mean(a), mb = mean(b);
  let ab = 0, aa = 0, bb = 0;
  for (let i = 0; i < a.length; i++) { ab += (a[i] - ma) * (b[i] - mb); aa += (a[i] - ma) ** 2; bb += (b[i] - mb) ** 2; }
  const denominator = Math.sqrt(aa * bb);
  return denominator <= 1e-12 ? null : 1 - ab / denominator;
}

// ---------------------------------------------------------------------------------------------------------------
// Detection (detector.py): baselines, link scores, human likelihood, presence state machine.
// ---------------------------------------------------------------------------------------------------------------
const robust = (values, floor) => {
  const center = median(values);
  return {center, scale: Math.max(1.4826 * median(values.map(v => Math.abs(v - center))), floor)};
};
const quietBaseline = (values, floor) => {
  const p5 = percentile(values, 5), p40 = percentile(values, 40);
  return {center: p40, scale: Math.max((p40 - p5) / 1.39, floor)};
};

function baselineFrom(feats, origin, quiet) {
  const stats = {};
  let profile = null;
  const withProfile = feats.filter(f => f.csi_profile);
  if (withProfile.length) {
    const width = withProfile[withProfile.length - 1].csi_profile.length;
    let same = withProfile.filter(f => f.csi_profile.length === width);
    if (quiet) {
      const limit = percentile(same.map(f => f.csi_std ?? 0), 40);
      const quietOnes = same.filter(f => (f.csi_std ?? 0) <= limit);
      if (quietOnes.length) same = quietOnes;
    }
    profile = new Float64Array(width);
    for (const f of same) for (let j = 0; j < width; j++) profile[j] += f.csi_profile[j] / same.length;
  }
  for (const key of ["rssi_std", "csi_std"]) {
    const values = feats.filter(f => key in f).map(f => f[key]);
    if (values.length >= 4) stats[key] = (quiet ? quietBaseline : robust)(values, FLOORS[key]);
  }
  if (feats.some(f => "rssi_mean" in f)) {
    let selected = feats;
    if (quiet && stats.rssi_std) {
      const limit = percentile(feats.map(f => f.rssi_std ?? 0), 40);
      selected = feats.filter(f => (f.rssi_std ?? 0) <= limit);
      if (!selected.length) selected = feats;
    }
    const values = selected.filter(f => "rssi_mean" in f).map(f => f.rssi_mean);
    if (values.length >= 4) stats.rssi_mean = robust(values, FLOORS.rssi_mean);
  }
  if (profile) {
    const shifts = feats.map(f => profileShift(f.csi_profile, profile)).filter(s => s !== null);
    if (shifts.length >= 4) stats.csi_shift = (quiet ? quietBaseline : robust)(shifts, FLOORS.csi_shift);
  }
  return {stats, profile, origin};
}

class LinkDetector {
  constructor() {
    this.alpha = DET.tick / DET.adapt;
    this.minHistory = Math.max(8, Math.floor(DET.warmup / DET.tick));
    this.history = [];
    this.baseline = null;
    this.sinceRefresh = 0;
  }
  get ready() { return !!this.baseline && Object.keys(this.baseline.stats).length > 0; }
  get calibrated() { return this.ready && this.baseline.origin === "calibration"; }
  setBaseline(baseline) { this.baseline = baseline; this.history = []; }
  z(key, value, absolute = false) {
    const stat = this.baseline?.stats[key];
    if (!stat || value === null || value === undefined) return 0;
    const delta = value - stat.center;
    return Math.max(0, (absolute ? Math.abs(delta) : delta) / stat.scale);
  }
  update(feats, empty) {
    if (!feats) return {motion: 0, presence: 0, mech: 0, valid: false};
    this.history.push(feats);
    if (this.history.length > 600 / DET.tick) this.history.shift();
    if (!this.calibrated) {
      this.sinceRefresh += DET.tick;
      if (this.history.length >= this.minHistory && (!this.ready || this.sinceRefresh >= 30)) {
        this.baseline = baselineFrom(this.history, "auto", true);
        this.sinceRefresh = 0;
      }
    }
    if (!this.ready) return {motion: 0, presence: 0, mech: feats.mech, valid: false};
    const shift = profileShift(feats.csi_profile, this.baseline.profile);
    const z = {
      rssi_std: this.z("rssi_std", feats.rssi_std), csi_std: this.z("csi_std", feats.csi_std),
      rssi_mean: this.z("rssi_mean", feats.rssi_mean, true), csi_shift: this.z("csi_shift", shift),
    };
    if (empty && this.calibrated) this.adapt(feats, shift, z);
    return {
      motion: Math.max(z.rssi_std, z.csi_std) / DET.motionThreshold,
      presence: Math.max(z.rssi_mean, z.csi_shift) / DET.presenceThreshold,
      mech: feats.mech, valid: true,
    };
  }
  adapt(feats, shift, z) {
    const values = {rssi_std: feats.rssi_std, csi_std: feats.csi_std, rssi_mean: feats.rssi_mean, csi_shift: shift};
    for (const [key, value] of Object.entries(values)) {
      const stat = this.baseline.stats[key];
      if (stat && value !== null && value !== undefined && z[key] < 2) stat.center += this.alpha * (value - stat.center);
    }
    const profile = feats.csi_profile, base = this.baseline.profile;
    if (base && profile && profile.length === base.length && z.csi_shift < 2) {
      for (let j = 0; j < base.length; j++) base[j] += this.alpha * (profile[j] - base[j]);
    }
  }
  reanchor(seconds = 30) {
    if (!this.ready || !this.history.length) return;
    const recent = this.history.slice(-Math.max(1, Math.floor(seconds / DET.tick)));
    const values = recent.filter(f => "rssi_mean" in f).map(f => f.rssi_mean);
    if (values.length && this.baseline.stats.rssi_mean) this.baseline.stats.rssi_mean.center = median(values);
    const base = this.baseline.profile;
    if (base) {
      const profiles = recent.filter(f => f.csi_profile && f.csi_profile.length === base.length).map(f => f.csi_profile);
      if (profiles.length) for (let j = 0; j < base.length; j++) base[j] = mean(profiles.map(p => p[j]));
    }
  }
}

class HumanScorer {
  constructor() { this.history = []; this.streak = 0; this.confirmTicks = Math.max(1, Math.round(DET.confirm / DET.tick)); }
  update(now, motion, mech) {
    this.history.push([now, motion, mech]);
    while (this.history.length && this.history[0][0] < now - 6) this.history.shift();
    const recent = this.history.filter(([t]) => t >= now - 2).map(([, m]) => m);
    const level = recent.length ? median(recent) : 0;
    const magnitude = clamp((level - 0.6) / 1.4, 0, 1);
    const last4 = this.history.filter(([t]) => t >= now - 4).map(([, m]) => m);
    const persistence = last4.filter(m => m >= 0.8).length / Math.max(last4.length, Math.round(4 / DET.tick));
    const machine = this.history.filter(([t, m]) => t >= now - 3 && m >= 0.5).map(([, , k]) => k);
    const machineShare = machine.length ? mean(machine) : 0;
    const likelihood = clamp(0.55 * magnitude + 0.45 * persistence - 0.6 * machineShare, 0, 1);
    this.streak = likelihood >= DET.humanThreshold ? this.streak + 1 : 0;
    return {likelihood: round(likelihood), confirmed: this.streak >= this.confirmTicks, machine: round(machineShare)};
  }
  reset() { this.history = []; this.streak = 0; }
}

class PresenceTracker {
  constructor() { this.state = "learning"; this.aboveSince = null; this.lastActive = null; this.lastMotion = null; this.episode = null; }
  update(now, motion, presence, human, ready, calibrating) {
    let started = null, ended = null;
    if (calibrating || !ready) {
      if (this.episode) { ended = this.episode; this.episode = null; ended.end = now; ended.reason = calibrating ? "calibrating" : "no_signal"; }
      this.state = calibrating ? "calibrating" : "learning";
      this.aboveSince = null;
      return [started, ended];
    }
    if (this.state === "learning" || this.state === "calibrating") this.state = "empty";
    const above = motion >= 1;
    this.aboveSince = above ? (this.aboveSince ?? now) : null;
    if (this.state === "empty") {
      if (above && now - this.aboveSince + DET.tick >= DET.trigger - 1e-9) {
        this.state = "motion";
        this.lastActive = this.lastMotion = now;
        started = this.episode = {start: this.aboveSince, peak_motion: 0, peak_presence: 0, human: 0, confirmed: false,
          links: {}, alarm: false, armed: false, position: null, position_motion: 0};
      }
    } else if (this.state === "motion") {
      if (motion >= 0.6) this.lastActive = this.lastMotion = now;
      else this.state = "presence";
    } else if (this.state === "presence") {
      const still = presence >= 1 && now - this.lastMotion < DET.staticMax;
      if (above) { this.state = "motion"; this.lastActive = this.lastMotion = now; }
      else if (still) this.lastActive = now;
      else if (now - this.lastActive >= DET.hold || presence >= 1) {
        this.state = "empty";
        ended = this.episode; this.episode = null;
        if (ended) { ended.end = now; ended.reason = presence >= 1 ? "static" : "calm"; }
      }
    }
    if (this.episode) {
      const e = this.episode;
      e.peak_motion = Math.max(e.peak_motion, motion);
      e.peak_presence = Math.max(e.peak_presence, presence);
      e.human = Math.max(e.human, human.likelihood);
      e.confirmed = e.confirmed || human.confirmed;
    }
    return [started, ended];
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Localisation (localize.py): which links are disturbed and which are calm → floor heat map.
// ---------------------------------------------------------------------------------------------------------------
class Localizer {
  constructor(cell = 0.25) {
    this.cols = Math.max(2, Math.round(ROOM.width / cell));
    this.rows = Math.max(2, Math.round(ROOM.depth / cell));
    this.sensitivity = {};
    for (const link of LINKS) {
      const a = nodeById[link.tx].position, b = nodeById[link.rx].position;
      const length = Math.max(dist(a, b), 0.1), width = 0.5 * Math.sqrt(LIGHT / link.ghz * length) + 0.35;
      const grid = new Float64Array(this.rows * this.cols);
      const dx = b[0] - a[0], dz = b[2] - a[2], span = dx * dx + dz * dz;
      for (let r = 0; r < this.rows; r++) {
        for (let c = 0; c < this.cols; c++) {
          const x = (c + 0.5) * ROOM.width / this.cols, z = (r + 0.5) * ROOM.depth / this.rows;
          const t = span < 1e-9 ? 0 : clamp(((x - a[0]) * dx + (z - a[2]) * dz) / span, 0, 1);
          const d = Math.hypot(x - (a[0] + t * dx), z - (a[2] + t * dz));
          grid[r * this.cols + c] = Math.exp(-((d / width) ** 2));
        }
      }
      this.sensitivity[link.id] = grid;
    }
  }
  update(scores) {
    const observed = {};
    for (const link of LINKS) {
      const s = scores[link.id] || {};
      observed[link.id] = clamp(Math.max(s.motion || 0, s.presence || 0) / 1.5, 0, 1);
    }
    if (Math.max(...Object.values(observed)) < 0.35) return [null, null];
    const size = this.rows * this.cols, logLike = new Float64Array(size);
    for (const [id, p] of Object.entries(observed)) {
      const e = this.sensitivity[id];
      for (let i = 0; i < size; i++) logLike[i] += Math.log(0.05 + e[i] * p + (1 - e[i]) * (1 - p));
    }
    let best = -Infinity;
    for (const v of logLike) best = Math.max(best, v);
    const heat = Array.from(logLike, v => Math.exp(v - best));
    let total = 0, sx = 0, sz = 0;
    heat.forEach((h, i) => {
      if (h < 0.5) return;
      const r = Math.floor(i / this.cols), c = i % this.cols;
      total += h; sx += h * (c + 0.5) * ROOM.width / this.cols; sz += h * (r + 0.5) * ROOM.depth / this.rows;
    });
    return [heat, total > 0 ? [round(sx / total, 2), 0, round(sz / total, 2)] : null];
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Runtime (runtime.py): calibration, episodes, arming, alarm, snapshots.
// ---------------------------------------------------------------------------------------------------------------
const buffers = Object.fromEntries(LINKS.map(l => [l.id, new LinkBuffer(l.kind)]));
const world = new SimWorld(buffers);
const detectors = Object.fromEntries(LINKS.map(l => [l.id, new LinkDetector()]));
const human = new HumanScorer(), tracker = new PresenceTracker(), localizer = new Localizer();
const linkNames = Object.fromEntries(LINKS.map(l => [l.id, l.name]));
const sim = {
  arming: {armed: false, armedAt: null}, calibration: null, calibrationRequest: null, calibrationResult: null,
  event: null, events: [], history: [], latest: null, lastAlarmSent: -Infinity, started: wall(),
};
const iso = epoch => new Date(epoch * 1000).toISOString().replace(/\.\d{3}Z$/, "+00:00");
const id = () => Array.from({length: 16}, () => Math.floor(rand() * 16).toString(16)).join("");

function arming(now) {
  const {armed, armedAt} = sim.arming, activeAt = armed ? armedAt + ARM_DELAY : null;
  return {armed, active: armed && now >= activeAt, armed_at: armedAt, active_at: activeAt, delay_seconds: ARM_DELAY};
}

function calibrationTick(now, feats) {
  const request = sim.calibrationRequest;
  sim.calibrationRequest = null;
  if (request === "cancel") { sim.calibration = null; sim.calibrationResult = {ok: false, message: "Megszakítva"}; }
  else if (request !== null) sim.calibration = {start: now, end: now + request, features: Object.fromEntries(LINKS.map(l => [l.id, []]))};
  if (!sim.calibration) return false;
  for (const [linkId, f] of Object.entries(feats)) if (f) sim.calibration.features[linkId].push(f);
  if (now < sim.calibration.end) return true;
  const missing = [];
  for (const [linkId, collected] of Object.entries(sim.calibration.features)) {
    if (collected.length >= 10) detectors[linkId].setBaseline(baselineFrom(collected, "calibration", false));
    else missing.push(linkNames[linkId]);
  }
  sim.calibration = null;
  sim.calibrationResult = {ok: !missing.length, message: missing.length ? `Kevés mérés: ${missing.join(", ")}` : "Kész", at: iso(wall())};
  human.reset();
  return false;
}

function fields(episode) {
  return {
    label: episode.confirmed ? "ember" : "mozgás", human: round(episode.human), peak_motion: round(episode.peak_motion, 2),
    peak_presence: round(episode.peak_presence, 2), links: {...episode.links}, position: episode.position, armed: episode.armed,
  };
}

function step(now) {
  const feats = {}, infos = {};
  for (const link of LINKS) {
    infos[link.id] = buffers[link.id].info(now);
    const {times, rssi, amps} = buffers[link.id].window(DET.window, now);
    feats[link.id] = infos[link.id].state === "ok" ? extract(times, rssi, amps) : null;
  }
  const calibrating = calibrationTick(now, feats);
  const empty = tracker.state === "empty" && !calibrating;
  const scores = {};
  for (const link of LINKS) {
    scores[link.id] = calibrating ? {motion: 0, presence: 0, mech: 0, valid: false} : detectors[link.id].update(feats[link.id], empty);
  }
  const valid = Object.fromEntries(Object.entries(scores).filter(([, s]) => s.valid));
  const online = Object.values(infos).some(info => info.state === "ok");
  const motion = Math.max(0, ...Object.values(valid).map(s => s.motion));
  const presence = Math.max(0, ...Object.values(valid).map(s => s.presence));
  const loudest = Object.keys(valid).sort((a, b) => valid[b].motion - valid[a].motion)[0];
  const humanNow = human.update(now, motion, loudest ? valid[loudest].mech : 0);
  const [started, ended] = tracker.update(now, motion, presence, humanNow, Object.keys(valid).length > 0, calibrating);
  const [heat, position] = Object.keys(valid).length ? localizer.update(valid) : [null, null];
  const arm = arming(now);
  if (started) {
    started.id = id();
    sim.event = {
      id: started.id, timestamp: iso(started.start), state: "active", label: "mozgás", human: 0, peak_motion: 0,
      peak_presence: 0, links: {}, position: null, armed: arm.active, alarm: false, alarm_at: null,
      notification_status: null, simulated: false,
    };
    sim.events.unshift(sim.event);
  }
  const episode = tracker.episode;
  if (episode) {
    if (!arm.armed) episode.alarm = false;  // disarming acknowledges it; re-arming needs fresh activity to alarm again
    for (const [linkId, s] of Object.entries(valid)) episode.links[linkId] = round(Math.max(episode.links[linkId] || 0, s.motion), 2);
    if (position && motion >= episode.position_motion) { episode.position = position; episode.position_motion = motion; }
    episode.armed = episode.armed || arm.active;
    if (!episode.alarm && arm.active && humanNow.confirmed) {
      episode.alarm = true;
      Object.assign(sim.event, {alarm: true, alarm_at: iso(now)});
      if (now - sim.lastAlarmSent < COOLDOWN) sim.event.notification_status = "cooldown";
      else {
        sim.lastAlarmSent = now;
        sim.event.notification_status = "pending";
        const target = sim.event;
        setTimeout(() => { target.notification_status = "sent"; }, 700);  // the ntfy push is simulated
      }
    }
    Object.assign(sim.event, fields(episode));
  }
  if (ended && sim.event) {
    Object.assign(sim.event, fields(ended), {
      state: "closed", ended_at: iso(now), duration_seconds: round(Math.max(0, ended.end - ended.start), 1), end_reason: ended.reason,
    });
    sim.event = null;
    if (ended.reason === "static") for (const [linkId, s] of Object.entries(valid)) if (s.presence >= 1) detectors[linkId].reanchor();
  }
  publish(now, scores, infos, motion, presence, humanNow, heat, position, arm, online);
}

function publish(now, scores, infos, motion, presence, humanNow, heat, position, arm, online) {
  const state = online ? tracker.state : "offline";
  const links = {};
  for (const link of LINKS) {
    const s = scores[link.id], {rssi} = buffers[link.id].window(1, now), detector = detectors[link.id];
    links[link.id] = {
      motion: round(s.motion, 2), presence: round(s.presence, 2), mech: round(s.mech || 0, 2), valid: s.valid,
      rssi: rssi.length ? round(mean(rssi), 1) : null,
      baseline: detector.calibrated ? "calibrated" : detector.ready ? "auto" : "learning",
      ...infos[link.id],
    };
  }
  const episode = tracker.episode;
  const snapshot = {
    t: round(now, 2), state, motion: round(motion, 2), presence: round(presence, 2), human: humanNow, arming: arm,
    alarm: !!(episode && episode.alarm && arm.armed),
    episode: episode ? {id: episode.id, started: round(episode.start, 2), label: episode.confirmed ? "ember" : "mozgás",
      human: round(episode.human), alarm: episode.alarm} : null,
    links,
    heat: heat ? {rows: localizer.rows, cols: localizer.cols, values: heat.map(v => round(v, 2))} : null,
    position,
    calibration: sim.calibration ? {
      progress: round(Math.min(1, (now - sim.calibration.start) / (sim.calibration.end - sim.calibration.start)), 3),
      remaining: Math.round(Math.max(0, sim.calibration.end - now)),
    } : null,
    calibration_result: sim.calibrationResult,
    sim: world.state(),
  };
  sim.latest = snapshot;
  sim.history.push({t: snapshot.t, s: snapshot.alarm ? "alarm" : state,
    m: Object.fromEntries(Object.entries(links).map(([k, v]) => [k, v.motion])),
    r: Object.fromEntries(Object.entries(links).map(([k, v]) => [k, v.rssi]))});
  if (sim.history.length > 600 / DET.tick) sim.history.shift();
}

// ---------------------------------------------------------------------------------------------------------------
// Clock: the world runs at 50 Hz, detection at 2 Hz. The page starts with a calibrated, empty room.
// ---------------------------------------------------------------------------------------------------------------
let worldTime = wall() - 50, nextTick = worldTime;
function advanceTo(target, maxCatchUp = 10) {
  // A background tab gets its timers throttled: skip ahead instead of simulating minutes at once.
  if (target - worldTime > maxCatchUp) { worldTime = target - maxCatchUp; nextTick = Math.max(nextTick, worldTime); }
  while (worldTime < target) {
    world.advance(worldTime);
    worldTime += STEP;
    if (worldTime >= nextTick) { step(nextTick); nextTick += DET.tick; }
  }
}
function prepare() {
  const now = wall();
  world.sceneUntil = now + 60;  // empty room while fast-forwarding
  advanceTo(now - 44, Infinity);
  sim.calibrationRequest = 30;
  advanceTo(now, Infinity);
  world.sceneUntil = now + 8;  // somebody walks in a few seconds after the page opens
  // A few earlier episodes, so the event list shows what a day looks like.
  const past = [
    [18, 160, "ember", 0.93, {"door-router": 3.1, "pi-router": 1.8}, false, null],
    [72, 25, "mozgás", 0.31, {"window-router": 1.2}, false, null],
    [125, 245, "ember", 0.96, {"door-router": 4.2, "window-router": 2.6, "pi-router": 2.1}, true, "sent"],
    [210, 380, "ember", 0.88, {"pi-router": 2.4, "window-router": 1.6}, false, null],
  ];
  for (const [minutes, seconds, label, likelihood, linkScores, alarm, notified] of past) {
    const start = now - minutes * 60;
    sim.events.push({
      id: id(), timestamp: iso(start), state: "closed", label, human: likelihood, peak_motion: Math.max(...Object.values(linkScores)),
      peak_presence: 0.4, links: linkScores, position: null, armed: alarm, alarm, alarm_at: alarm ? iso(start + 3) : null,
      notification_status: notified, simulated: false, ended_at: iso(start + seconds), duration_seconds: seconds, end_reason: "calm",
    });
  }
}
prepare();

const streams = new Set();
setInterval(() => {
  advanceTo(wall());
  for (const stream of streams) stream.deliver(sim.latest);
}, DET.tick * 1000);

// ---------------------------------------------------------------------------------------------------------------
// The fake API: fetch("/api/...") and EventSource("/api/stream").
// ---------------------------------------------------------------------------------------------------------------
function status(now) {
  const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
  return {
    layout: {room: ROOM, nodes: NODES, links: LINKS.map(link => ({
      id: link.id, name: link.name, source: "sim", kind: link.kind, tx: link.tx, rx: link.rx, ghz: link.ghz,
      length: round(dist(nodeById[link.tx].position, nodeById[link.rx].position), 2),
    }))},
    mode: "sim", simulated: true, uptime_seconds: Math.round(now - sim.started),
    notifications: {ntfy: true, webhook: false}, alarm_trigger: "human",
    thresholds: {motion_threshold: DET.motionThreshold, presence_threshold: DET.presenceThreshold, human_threshold: DET.humanThreshold,
      trigger_seconds: DET.trigger, hold_seconds: DET.hold, window_seconds: DET.window},
    calibration_seconds: CALIBRATION,
    today_events: sim.events.filter(e => Date.parse(e.timestamp) >= dayStart.getTime()).length,
    latest: sim.latest,
  };
}
const respond = (code, body) => new Response(JSON.stringify(body), {status: code, headers: {"Content-Type": "application/json"}});
const realFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, "http://demo.invalid");
  if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
  await new Promise(resolve => setTimeout(resolve, 40 + Math.random() * 80));
  const method = (init.method || "GET").toUpperCase(), now = wall();
  let body = null;
  try { body = init.body ? JSON.parse(init.body) : null; } catch { return respond(422, {detail: "Invalid JSON"}); }
  const path = url.pathname;
  if (method === "GET" && path === "/api/status") return respond(200, status(now));
  if (method === "GET" && path === "/api/live") {
    const seconds = Number(url.searchParams.get("seconds") || 120), last = sim.history.at(-1)?.t ?? now;
    return respond(200, {points: sim.history.filter(p => p.t >= last - seconds)});
  }
  if (method === "GET" && path === "/api/events") {
    const limit = Number(url.searchParams.get("limit") || 20), offset = Number(url.searchParams.get("offset") || 0);
    return respond(200, {events: sim.events.slice(offset, offset + limit).map(e => ({...e})), limit, offset});
  }
  if (method === "POST" && path === "/api/arm") {
    if (typeof body?.armed !== "boolean") return respond(422, {detail: "armed must be true or false"});
    if (body.armed !== sim.arming.armed) sim.arming = {armed: body.armed, armedAt: body.armed ? now : null};
    return respond(200, arming(now));
  }
  if (method === "POST" && path === "/api/calibrate") {
    if (body?.cancel) { sim.calibrationRequest = "cancel"; return respond(200, {cancelled: true}); }
    const seconds = body?.seconds ?? CALIBRATION;
    if (!(seconds >= 10 && seconds <= 600)) return respond(422, {detail: "seconds must be between 10 and 600"});
    sim.calibrationRequest = seconds;
    sim.calibrationResult = null;
    return respond(200, {seconds});
  }
  if (method === "POST" && path === "/api/notify-test") return respond(200, {result: "sent"});
  if (method === "POST" && path === "/api/sim") {
    if (!["auto", "enter", "walk", "still", "leave", "fan", "cat"].includes(body?.action)) return respond(422, {detail: `unknown action ${body?.action}`});
    return respond(200, world.action(body.action));
  }
  return respond(404, {detail: "Not Found"});
};

class DemoEventSource {
  constructor(url) {
    this.url = url; this.readyState = 0; this.onopen = this.onmessage = this.onerror = null;
    this.listeners = {open: [], message: [], error: []};
    setTimeout(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      this.emit("open", {});
      streams.add(this);
      if (sim.latest) this.deliver(sim.latest);
    }, 80);
  }
  emit(type, event) {
    this[`on${type}`]?.(event);
    for (const listener of this.listeners[type] || []) listener(event);
  }
  deliver(snapshot) { this.emit("message", {data: JSON.stringify(snapshot)}); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  removeEventListener(type, listener) { this.listeners[type] = (this.listeners[type] || []).filter(l => l !== listener); }
  close() { this.readyState = 2; streams.delete(this); }
}
const RealEventSource = window.EventSource;
window.EventSource = function EventSource(url, options) {
  return new URL(url, "http://demo.invalid").pathname === "/api/stream" ? new DemoEventSource(url) : new RealEventSource(url, options);
};

// ---------------------------------------------------------------------------------------------------------------
// Demo chrome: floating control panel for scenarios and faults (the room controls are the UI's own sim card).
// ---------------------------------------------------------------------------------------------------------------
const panel = document.createElement("aside");
panel.className = "demo-panel";
panel.setAttribute("aria-label", "Demó vezérlő");
panel.innerHTML = `
  <div class="demo-body" id="demo-body" hidden>
    <div class="demo-head"><h2>Demó mód</h2><p>A valódi felület kódja, szimulált szobával és érzékelőkkel. Nincs szerver és Pi: a jelek és a felismerés is a böngészőben futnak.</p></div>
    <section aria-labelledby="demo-scenario-heading"><h3 id="demo-scenario-heading">Élesített forgatókönyv</h3>
      <div class="demo-grid"><button type="button" data-scenario="burglar">Betörő belép</button><button type="button" data-scenario="cat">Csak a macska</button><button type="button" data-scenario="fan">Csak a ventilátor</button><button type="button" data-scenario="still">Mozdulatlan ember</button></div>
      <p class="demo-hint" id="demo-status" role="status">A rendszer azonnal élesedik (késleltetés nélkül). Az embert riasztja, a macskát és a ventilátort nem.</p></section>
    <section aria-labelledby="demo-fault-heading"><h3 id="demo-fault-heading">Hibahelyzet</h3>
      <label class="demo-switch"><span>Az ajtónál lévő ESP32 kiesik</span><input type="checkbox" data-fault="door"></label>
      <label class="demo-switch"><span>Energiatakarékos WiFi a Pi-n</span><input type="checkbox" data-fault="powerSave"></label>
      <label class="demo-switch"><span>Minden link kiesik</span><input type="checkbox" data-fault="all"></label></section>
    <div class="demo-foot"><button type="button" id="demo-reset">Alaphelyzet</button><span>Kilépési késleltetés a demóban: ${ARM_DELAY} mp.</span></div>
  </div>
  <button type="button" class="demo-toggle" aria-expanded="false" aria-controls="demo-body"><span class="demo-dot" aria-hidden="true"></span>Demó<span class="demo-toggle-hint">vezérlő</span></button>`;
document.body.append(panel);
const $demo = selector => panel.querySelector(selector);
function setOpen(open) {
  $demo("#demo-body").hidden = !open;
  $demo(".demo-toggle").setAttribute("aria-expanded", String(open));
}
let stored = null;
try { stored = localStorage.getItem("wifi-demo-panel"); } catch { /* storage unavailable */ }
setOpen(stored === "open");
$demo(".demo-toggle").addEventListener("click", () => {
  const open = $demo("#demo-body").hidden;
  setOpen(open);
  try { localStorage.setItem("wifi-demo-panel", open ? "open" : "closed"); } catch { /* storage unavailable */ }
});
panel.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$demo("#demo-body").hidden) { setOpen(false); $demo(".demo-toggle").focus(); }
});
const SCENARIOS = {
  burglar: "Élesítve. Egy ember belép: pár másodperc múlva riasztás és (szimulált) ntfy-értesítés.",
  cat: "Élesítve, a szobában csak a macska jár: mozgás lehet, emberi riasztás nem.",
  fan: "Élesítve, megy a ventilátor: a gépies, periodikus jelet kiszűri, nem riaszt.",
  still: "Élesítve, valaki mozdulatlanul ül: a belépésre riaszt; utána csak a CSI-linkek szintváltozása látja.",
};
for (const button of panel.querySelectorAll("[data-scenario]")) {
  button.addEventListener("click", () => {
    const now = wall(), scenario = button.dataset.scenario;
    sim.arming = {armed: true, armedAt: now - ARM_DELAY};
    world.auto = false;
    world.fanOn = scenario === "fan";
    world.cat.present = world.cat.moving = scenario === "cat";
    if (scenario === "burglar" || scenario === "still") {
      world.setScene("absent");
      setTimeout(() => world.setScene("enter"), 1500);
      if (scenario === "still") setTimeout(() => world.setScene("still"), 9000);
    } else world.setScene("absent");
    $demo("#demo-status").textContent = SCENARIOS[scenario];
  });
}
for (const input of panel.querySelectorAll("[data-fault]")) {
  input.addEventListener("change", () => { world.faults[input.dataset.fault] = input.checked; });
}
$demo("#demo-reset").addEventListener("click", () => {
  sim.arming = {armed: false, armedAt: null};
  world.faults = {door: false, all: false, powerSave: false};
  for (const input of panel.querySelectorAll("[data-fault]")) input.checked = false;
  world.fanOn = false;
  world.cat.present = world.cat.moving = false;
  world.setScene("absent");
  world.auto = true;
  world.scriptIndex = AUTO_SCRIPT.length - 1;
  world.sceneUntil = wall() + 8;
  $demo("#demo-status").textContent = "Alaphelyzet: üres szoba, hatástalanítva; pár másodperc múlva magától jön valaki.";
});
})();
