// WiFi jelenlét UI: status + live stream (SSE) → state card, 3D view, chart, links, events, controls.
import { LiveChart } from "/static/chart.js";

const COLORS = ["#e8c31c", "#2aa6e3", "#b48cf0", "#9aa3ab", "#ff8576"];
const STATE_TEXT = {
  learning: ["Tanulás", "Az üres szoba jelét tanulja…"],
  calibrating: ["Kalibrálás", "Maradj kint a szobából."],
  empty: ["Üres", "Nincs mozgás."],
  motion: ["Mozgás", ""],
  presence: ["Jelenlét", "Nemrég mozgás volt; figyeli, mikor nyugszik meg."],
  offline: ["Nincs jel", "Egyik link sem ad friss mérést."],
};
const BASELINE = { calibrated: ["kalibrált", "done"], auto: ["automatikus", ""], learning: ["tanul", "go"] };
const LINK_STATE = { stale: "Nincs friss mérés", error: "Hiba", starting: "Indul" };
const ICONS = {
  human: '<svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="2.2"/><path d="M12 8v6M8.5 10.5 12 9l3.5 1.5M12 14l-3 6M12 14l3 6"/></svg>',
  motion: '<svg viewBox="0 0 24 24"><path d="M3 12h3l2-5 4 10 3-7 2 2h4"/></svg>',
  alarm: '<svg viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6Z"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
};

const $ = (id) => document.getElementById(id);
const number = (v, digits = 1) => (v ?? 0).toLocaleString("hu-HU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (epoch) => new Date(epoch * 1000).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const escape = (text) => String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let status = null;
let view = null;
let chart = null;
let latest = null;
let lastEpisode = undefined;
let toastTimer = null;
let connected = false;

function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds} mp`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} p ${seconds % 60} mp`;
  return `${Math.floor(minutes / 60)} ó ${minutes % 60} p`;
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 3200);
}

async function post(url, body = {}) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function linkName(id) {
  return status.layout.links.find((link) => link.id === id)?.name ?? id;
}

// Static parts -------------------------------------------------------------------------------------
function buildLinks() {
  const list = $("links");
  list.innerHTML = "";
  const legend = $("chart-legend");
  legend.innerHTML = "";
  status.layout.links.forEach((link, index) => {
    const color = COLORS[index % COLORS.length];
    const item = document.createElement("li");
    item.className = "link";
    item.dataset.id = link.id;
    item.innerHTML = `
      <div class="link-head">
        <span class="link-dot" style="background:${color}"></span>
        <span class="link-name">${escape(link.name)}</span>
        <span class="pill small" data-role="baseline"></span>
      </div>
      <div class="link-meta">
        <span>${link.kind === "csi" ? "CSI" : "RSSI"} · ${link.source === "sim" ? "szimulált" : escape(link.source)}</span>
        <span>jel <b data-role="rssi">–</b></span>
        <span><b data-role="rate">–</b> Hz</span>
        <span>${number(link.length, 1)} m</span>
      </div>
      <div class="meter-track"><span class="meter-fill" data-role="fill"></span><span class="meter-mark"></span></div>
      <p class="link-error" data-role="error" hidden></p>`;
    list.appendChild(item);
    legend.insertAdjacentHTML("beforeend", `<span><i style="background:${color}"></i>${escape(link.name)}</span>`);
  });
  $("links-count").textContent = `${status.layout.links.length} link`;
}

function buildFacts() {
  const t = status.thresholds;
  const notify = [status.notifications.ntfy && "ntfy", status.notifications.webhook && "webhook"].filter(Boolean).join(" + ") || "nincs beállítva";
  const rows = [
    ["Riaszt", status.alarm_trigger === "human" ? "emberi mozgásra" : "bármilyen mozgásra"],
    ["Értesítés", notify],
    ["Kilépési késleltetés", duration(latest?.arming?.delay_seconds ?? 0)],
    ["Megtartás mozgás után", duration(t.hold_seconds)],
    ["Ember-küszöb", `${Math.round(t.human_threshold * 100)}%`],
  ];
  $("facts").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escape(v)}</dd>`).join("");
  $("notify-test").disabled = !status.notifications.ntfy && !status.notifications.webhook;
}

// Live updates -------------------------------------------------------------------------------------
function renderState(s) {
  const card = $("state-card");
  const alarm = s.alarm;
  const [title, defaultSub] = alarm ? ["RIASZTÁS", ""] : STATE_TEXT[s.state] ?? [s.state, ""];
  card.dataset.state = alarm ? "alarm" : s.state;
  $("state").textContent = title;
  let sub = defaultSub;
  if (s.episode && (s.state === "motion" || alarm)) {
    const loud = Object.entries(s.links).filter(([, l]) => l.valid && l.motion >= 0.6).map(([id]) => linkName(id));
    sub = [s.episode.label === "ember" ? "Ember" : "Mozgás", loud.join(", "), duration(s.t - s.episode.started)].filter(Boolean).join(" · ");
  } else if (s.state === "calibrating" && s.calibration) {
    sub = `Maradj kint · még ${duration(s.calibration.remaining)}`;
  }
  $("state-sub").textContent = sub;

  const human = s.human?.likelihood ?? 0;
  $("human-arc").setAttribute("stroke-dasharray", `${Math.round(human * 100)} 100`);
  $("human-value").textContent = `${Math.round(human * 100)}%`;
  $("human-ring").dataset.hot = String(human >= status.thresholds.human_threshold);
  $("human-ring").dataset.zero = String(human < 0.01);
  for (const [key, value] of [["motion", s.motion], ["presence", s.presence]]) {
    $(`${key}-value`).textContent = `${number(value)}×`;
    const fill = $(`${key}-fill`);
    fill.style.width = `${Math.min(value / 3, 1) * 100}%`;
    fill.dataset.over = String(value >= 1);
  }

  const arming = s.arming;
  const button = $("arm");
  button.disabled = false;
  button.setAttribute("aria-pressed", String(arming.armed));
  button.dataset.pending = String(arming.armed && !arming.active);
  let armText;
  if (!arming.armed) {
    $("arm-label").textContent = "Hatástalanítva";
    armText = "Riasztás kikapcsolva. Az események így is naplózódnak.";
  } else if (!arming.active) {
    const left = Math.max(0, arming.active_at - s.t);
    $("arm-label").textContent = `Élesítés · ${Math.ceil(left)} mp`;
    armText = `Kilépési késleltetés: ${duration(left)} múlva élesedik.`;
  } else {
    $("arm-label").textContent = "Élesítve";
    armText = `Élesítve ${clock(arming.armed_at)} óta · ${status.alarm_trigger === "human" ? "emberi mozgásra" : "bármilyen mozgásra"} riaszt.`;
  }
  $("arming-text").textContent = armText;
}

function renderLinks(s) {
  for (const item of $("links").children) {
    const link = s.links[item.dataset.id];
    if (!link) continue;
    const [label, kind] = BASELINE[link.baseline] ?? ["", ""];
    const pill = item.querySelector('[data-role="baseline"]');
    pill.textContent = label;
    pill.dataset.kind = kind;
    item.querySelector('[data-role="rssi"]').textContent = link.rssi == null ? "–" : `${number(link.rssi, 0)} dBm`;
    item.querySelector('[data-role="rate"]').textContent = number(link.rate, 0);
    const fill = item.querySelector('[data-role="fill"]');
    const score = link.valid ? Math.max(link.motion, link.presence) : 0;
    fill.style.width = `${Math.min(score / 3, 1) * 100}%`;
    fill.dataset.over = String(score >= 1);
    const error = item.querySelector('[data-role="error"]');
    if (link.state !== "ok") {
      error.textContent = `${LINK_STATE[link.state] ?? link.state}${link.message ? `: ${link.message}` : ""}`;
    } else if (link.power_save) {
      error.textContent = `Energiatakarékos WiFi-mód: ritkább mérés (sudo iw dev ${link.interface} set power_save off)`;
    }
    error.hidden = link.state === "ok" && !link.power_save;
  }
}

function renderBanner(s) {
  const banner = $("banner");
  let kind = null, text = "";
  if (!connected) {
    kind = "warn"; text = "Kapcsolat megszakadt a szolgáltatással, újrapróbálkozás…";
  } else if (s.alarm) {
    kind = "alarm"; text = `Riasztás ${clock(s.episode.started)} óta · ember valószínűsége ${Math.round((s.episode.human ?? 0) * 100)}%`;
  } else if (s.state === "offline") {
    kind = "warn"; text = "Nincs friss WiFi-mérés. Ellenőrizd a linkeket (Dockerben network_mode: host kell).";
  }
  banner.hidden = !kind;
  if (kind) { banner.dataset.kind = kind; banner.textContent = text; }
}

function renderCalibration(s) {
  const active = !!s.calibration;
  $("calib-progress").hidden = !active;
  $("calibrate-cancel").hidden = !active;
  $("calibrate").disabled = active;
  if (active) $("calib-bar").style.width = `${s.calibration.progress * 100}%`;
  const kinds = Object.values(s.links).map((l) => l.baseline);
  const calibrated = kinds.length && kinds.every((k) => k === "calibrated");
  const pill = $("calib-pill");
  pill.textContent = active ? "folyamatban" : calibrated ? "kalibrált" : kinds.includes("learning") ? "tanul" : "automatikus";
  pill.dataset.kind = active ? "go" : calibrated ? "done" : "";
  let text = "Hagyd el a szobát, és indítsd el: az üres szoba jelét veszi fel alapvonalnak.";
  if (active) text = `Maradj kint a szobából, még ${duration(s.calibration.remaining)}.`;
  else if (s.calibration_result) text = s.calibration_result.ok ? `Kész: az alapvonal az üres szobát írja le.` : `Nem sikerült: ${s.calibration_result.message}`;
  else if (!calibrated) text += " Addig az alapvonal automatikus: a legcsendesebb időszakokból becsül.";
  $("calib-text").textContent = text;
}

function renderSim(s) {
  if (!s.sim) return;
  const scene = { absent: "üres", enter: "belép", walk: "járkál", still: "mozdulatlan", leave: "kimegy" }[s.sim.scene] ?? s.sim.scene;
  $("sim-scene").textContent = s.sim.auto ? `auto · ${scene}` : scene;
  document.querySelector('[data-sim="fan"]').setAttribute("aria-pressed", String(s.sim.fan));
  document.querySelector('[data-sim="cat"]').setAttribute("aria-pressed", String(!!s.sim.cat));
  document.querySelector('[data-sim="auto"]').setAttribute("aria-pressed", String(s.sim.auto));
}

function onSnapshot(s) {
  latest = s;
  renderState(s);
  renderLinks(s);
  renderBanner(s);
  renderCalibration(s);
  renderSim(s);
  chart.push(s);
  view?.update(s);
  const episode = s.episode ? `${s.episode.id}:${s.episode.label}:${s.episode.alarm}` : null;
  if (episode !== lastEpisode) {
    lastEpisode = episode;
    loadEvents();
  }
}

// Events -------------------------------------------------------------------------------------------
function eventRow(event) {
  const start = Date.parse(event.timestamp) / 1000;
  const human = event.label === "ember";
  const kind = event.alarm ? "alarm" : human ? "human" : "motion";
  const pills = [];
  if (event.state === "active") pills.push(['folyamatban', "go"]);
  if (event.alarm) pills.push(["riasztás", "warn"]);
  if (event.notification_status === "sent") pills.push(["értesítve", "done"]);
  else if (event.notification_status === "failed") pills.push(["értesítés hiba", "warn"]);
  else if (event.notification_status === "cooldown") pills.push(["értesítés szünetel", ""]);
  if (!event.alarm && event.armed === false) pills.push(["nem élesített", ""]);
  if (event.simulated) pills.push(["teszt", ""]);
  const links = Object.entries(event.links || {}).sort((a, b) => b[1] - a[1]).filter(([, v]) => v >= 1).map(([id]) => linkName(id));
  const span = event.duration_seconds != null ? duration(event.duration_seconds) : "tart";
  return `<li>
    <span class="event-icon" data-kind="${kind}">${ICONS[kind]}</span>
    <div>
      <p class="event-title">${human ? "Ember" : "Mozgás"} · ${clock(start)}</p>
      <p class="event-sub">${span} · ember ${Math.round((event.human ?? 0) * 100)}%${links.length ? ` · ${escape(links.join(", "))}` : ""}</p>
    </div>
    <div class="event-pills">${pills.map(([t, k]) => `<span class="pill small" data-kind="${k}">${t}</span>`).join("")}</div>
  </li>`;
}

let eventsTimer = null;
function loadEvents() {
  clearTimeout(eventsTimer);
  eventsTimer = setTimeout(async () => {
    try {
      const response = await fetch("/api/events?limit=8");
      const { events } = await response.json();
      $("events").innerHTML = events.length ? events.map(eventRow).join("") : '<li class="empty muted">Még nincs esemény.</li>';
      const today = new Date(); today.setHours(0, 0, 0, 0);
      const count = events.filter((e) => Date.parse(e.timestamp) >= today.getTime()).length;
      $("events-today").textContent = count ? `ma ${count}${events.length === count ? "" : "+"}` : "";
    } catch { /* the stream banner already reports connectivity */ }
  }, 250);
}

// Controls -----------------------------------------------------------------------------------------
function bindControls() {
  $("arm").addEventListener("click", async () => {
    const armed = latest?.arming?.armed;
    try {
      const result = await post("/api/arm", { armed: !armed });
      toast(result.armed ? `Élesítés: ${duration(result.delay_seconds)} múlva aktív` : "Hatástalanítva");
    } catch (error) { toast(`Nem sikerült: ${error.message}`); }
  });
  $("calibrate").addEventListener("click", async () => {
    try {
      const result = await post("/api/calibrate", {});
      toast(`Kalibrálás ${duration(result.seconds)}: hagyd el a szobát`);
    } catch (error) { toast(`Nem sikerült: ${error.message}`); }
  });
  $("calibrate-cancel").addEventListener("click", () => post("/api/calibrate", { cancel: true }).catch(() => {}));
  $("notify-test").addEventListener("click", async () => {
    try {
      const { result } = await post("/api/notify-test");
      toast(result === "sent" ? "Teszt értesítés elküldve" : result === "disabled" ? "Nincs értesítés beállítva" : "Az értesítés nem ment ki");
    } catch (error) { toast(`Nem sikerült: ${error.message}`); }
  });
  for (const button of document.querySelectorAll("[data-sim]")) {
    button.addEventListener("click", () => post("/api/sim", { action: button.dataset.sim }).catch((error) => toast(error.message)));
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => {
      for (const other of document.querySelectorAll("[data-view]")) other.setAttribute("aria-pressed", String(other === button));
      view?.setView(button.dataset.view);
    });
  }
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onopen = () => { connected = true; };
  source.onmessage = (message) => {
    connected = true;
    onSnapshot(JSON.parse(message.data));
  };
  source.onerror = () => {
    connected = false;
    if (latest) renderBanner(latest);
  };
}

async function init() {
  bindControls();
  status = await (await fetch("/api/status")).json();
  latest = status.latest;
  const links = status.layout.links;
  const sensors = links.map((l) => (l.kind === "csi" ? "CSI" : "RSSI"));
  $("subtitle").textContent = `${links.length} link · ${[...new Set(sensors)].join(" + ")} · ${status.layout.room.width} × ${status.layout.room.depth} m`;
  $("mode-pill").hidden = !status.simulated;
  $("sim-card").hidden = !status.simulated;
  document.querySelector(".truth-key").hidden = !status.simulated;
  buildLinks();
  buildFacts();
  chart = new LiveChart($("chart"), links, COLORS);
  try {
    const { points } = await (await fetch("/api/live?seconds=120")).json();
    chart.seed(points);
  } catch { /* starts empty */ }
  // The 3D view is optional: detection and controls work without WebGL.
  try {
    const { SpaceView, webglAvailable } = await import("/static/scene3d.js");
    if (webglAvailable()) view = new SpaceView($("scene"), status.layout, COLORS);
    else $("scene-fallback").hidden = false;
  } catch (error) {
    console.error(error);
    $("scene-fallback").hidden = false;
  }
  if (latest) onSnapshot(latest);
  loadEvents();
  connect();
}

init().catch((error) => {
  console.error(error);
  $("subtitle").textContent = "A szolgáltatás nem érhető el.";
});
