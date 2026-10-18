// Two-pane live chart on a plain canvas: motion score per link (threshold at 1.0) and RSSI in dBm.
const SECONDS = 120;
const STATE_TINT = { motion: "rgba(201,210,54,.13)", presence: "rgba(201,210,54,.06)", alarm: "rgba(255,133,118,.2)" };

export class LiveChart {
  constructor(canvas, links, colors) {
    this.canvas = canvas;
    this.links = links;
    this.colors = colors;
    this.points = [];
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  seed(points) {
    this.points = points.slice(-SECONDS * 4);
    this.draw();
  }

  push(snapshot) {
    const m = {}, r = {};
    for (const [id, link] of Object.entries(snapshot.links)) {
      m[id] = link.valid ? link.motion : null;
      r[id] = link.rssi;
    }
    this.points.push({ t: snapshot.t, s: snapshot.alarm ? "alarm" : snapshot.state, m, r });
    const cutoff = snapshot.t - SECONDS - 5;
    while (this.points.length && this.points[0].t < cutoff) this.points.shift();
    this.draw();
  }

  draw() {
    const canvas = this.canvas;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const left = 44, right = 8, gap = 34, bottom = 20;
    const paneHeight = (height - gap - bottom) * 0.58;
    const lowerTop = paneHeight + gap;
    const lowerHeight = height - bottom - lowerTop;
    const now = this.points.length ? this.points[this.points.length - 1].t : Date.now() / 1000;
    const x = (t) => left + ((t - (now - SECONDS)) / SECONDS) * (width - left - right);
    ctx.font = "600 11px Manrope, sans-serif";
    ctx.textBaseline = "middle";

    // State bands behind both panes.
    for (let i = 1; i < this.points.length; i++) {
      const tint = STATE_TINT[this.points[i].s];
      if (!tint) continue;
      ctx.fillStyle = tint;
      ctx.fillRect(x(this.points[i - 1].t), 0, Math.max(1, x(this.points[i].t) - x(this.points[i - 1].t)), height - bottom);
    }

    // Upper pane: motion score.
    // The interesting range is around the threshold: one spike must not flatten everything else.
    let peak = 3;
    for (const p of this.points) for (const v of Object.values(p.m)) if (v != null && v > peak) peak = v;
    peak = Math.min(Math.ceil(peak), 6);
    const y = (v) => paneHeight - (Math.min(v, peak) / peak) * (paneHeight - 6);
    this.axis(ctx, left, width - right, [0, 1, peak], y, (v) => `${v}×`);
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = "rgba(255,255,255,.55)";
    ctx.beginPath();
    ctx.moveTo(left, y(1));
    ctx.lineTo(width - right, y(1));
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = "rgba(255,255,255,.55)";
    ctx.textAlign = "right";
    ctx.fillText("küszöb", width - right - 2, y(1) - 9);
    this.links.forEach((link, index) => this.series(ctx, x, y, (p) => p.m[link.id], this.colors[index % this.colors.length], 2));

    // Lower pane: RSSI.
    let min = Infinity, max = -Infinity;
    for (const p of this.points) for (const v of Object.values(p.r)) if (v != null) { min = Math.min(min, v); max = Math.max(max, v); }
    if (!Number.isFinite(min)) { min = -80; max = -40; }
    min = Math.floor(min - 1); max = Math.ceil(max + 1);
    if (max - min < 6) { const mid = (max + min) / 2; min = Math.floor(mid - 3); max = Math.ceil(mid + 3); }
    const yr = (v) => lowerTop + (1 - (v - min) / (max - min)) * lowerHeight;
    this.axis(ctx, left, width - right, [min, max], yr, (v) => `${v}`);
    ctx.fillStyle = "rgba(255,255,255,.42)";
    ctx.textAlign = "left";
    ctx.fillText("RSSI, dBm", left + 4, lowerTop - 10);
    this.links.forEach((link, index) => this.series(ctx, x, yr, (p) => p.r[link.id], this.colors[index % this.colors.length], 1.5, 0.8));

    // Time axis.
    ctx.fillStyle = "rgba(255,255,255,.42)";
    ctx.textAlign = "center";
    for (const back of [120, 90, 60, 30, 0]) {
      ctx.fillText(back ? `−${back} mp` : "most", Math.min(Math.max(x(now - back), left + 18), width - right - 14), height - 8);
    }
  }

  axis(ctx, x0, x1, values, y, format) {
    ctx.strokeStyle = "rgba(255,255,255,.08)";
    ctx.fillStyle = "rgba(255,255,255,.42)";
    ctx.textAlign = "right";
    for (const v of values) {
      ctx.beginPath();
      ctx.moveTo(x0, y(v));
      ctx.lineTo(x1, y(v));
      ctx.stroke();
      ctx.fillText(format(v), x0 - 8, y(v));
    }
  }

  series(ctx, x, y, pick, color, width, alpha = 1) {
    ctx.strokeStyle = color;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let drawing = false;
    for (const p of this.points) {
      const v = pick(p);
      if (v == null) { drawing = false; continue; }
      if (drawing) ctx.lineTo(x(p.t), y(v));
      else { ctx.moveTo(x(p.t), y(v)); drawing = true; }
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
}
