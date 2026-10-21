#!/usr/bin/env node
// Headless smoke test of the race UI against a running scoreboard (no browser).
//
//   node tools/smoke_ui.mjs --url http://localhost:8088 [--png out/] [--wait-pit]
//
// It checks that / and every JS module are served (and match web/js on disk),
// then boots web/js/main.js in node with a tiny DOM: a software canvas that
// implements exactly the 2D calls the UI is allowed to use (fillRect,
// drawImage, putImageData, ...; anything else fails the test), an SSE
// EventSource over fetch, and a stub AudioContext. It drives the UI (click a
// car, focus on wall, tabs, sound), optionally waits for the pit and the next
// heat, writes PNG screenshots of the canvas, and fails on any JS error.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";

const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => {
  if (x.startsWith("--")) a.push([x.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : true]);
  return a;
}, []));
const BASE = String(args.url || "http://localhost:8080").replace(/\/?$/, "/");
const PNG_DIR = args.png && args.png !== true ? args.png : null;
const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, "..", "web");

const results = [];
let failed = 0;
function check(name, ok, info = "") {
  results.push({ name, ok: !!ok, info });
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${info ? "  (" + info + ")" : ""}`);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, ms, step = 100) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (fn()) return true; await sleep(step); }
  return !!fn();
}

// ---------- software canvas ----------

const unsupported = new Set();
const colorCache = new Map();
function parseColor(s) {
  let c = colorCache.get(s);
  if (c) return c;
  let m;
  if ((m = /^#([0-9a-f]{3})$/i.exec(s))) c = [...m[1]].map(h => parseInt(h + h, 16)).concat(255);
  else if ((m = /^#([0-9a-f]{6})$/i.exec(s))) c = [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16)).concat(255);
  else if ((m = /^rgba?\(([^)]+)\)$/i.exec(s))) {
    const p = m[1].split(",").map(Number);
    c = [p[0], p[1], p[2], Math.round((p[3] ?? 1) * 255)];
  } else { unsupported.add("color:" + s); c = [255, 0, 255, 255]; }
  colorCache.set(s, c);
  return c;
}

class Ctx2D {
  constructor(cv) { this.cv = cv; this.fillStyle = "#000000"; this.globalAlpha = 1; this.imageSmoothingEnabled = true; this.calls = 0; }
  blend(i, r, g, b, a) {
    const d = this.cv.data;
    a = (a / 255) * this.globalAlpha;
    if (a >= 1) { d[i] = r; d[i + 1] = g; d[i + 2] = b; d[i + 3] = 255; return; }
    if (a <= 0) return;
    d[i] = d[i] * (1 - a) + r * a; d[i + 1] = d[i + 1] * (1 - a) + g * a; d[i + 2] = d[i + 2] * (1 - a) + b * a;
    d[i + 3] = Math.min(255, d[i + 3] + a * 255);
  }
  fillRect(x, y, w, h) {
    this.calls++;
    const [r, g, b, a] = parseColor(this.fillStyle);
    const W = this.cv.width, H = this.cv.height;
    const x0 = Math.max(0, Math.round(x)), x1 = Math.min(W, Math.round(x + w));
    const y0 = Math.max(0, Math.round(y)), y1 = Math.min(H, Math.round(y + h));
    for (let yy = y0; yy < y1; yy++) for (let xx = x0; xx < x1; xx++) this.blend((yy * W + xx) * 4, r, g, b, a);
  }
  clearRect(x, y, w, h) { const s = this.fillStyle; this.fillStyle = "rgba(0,0,0,0)"; this.cv.data.fill(0); this.fillStyle = s; }
  drawImage(img, ...a) {
    this.calls++;
    let sx = 0, sy = 0, sw = img.width, sh = img.height, dx, dy, dw, dh;
    if (a.length === 2) { [dx, dy] = a; dw = sw; dh = sh; }
    else if (a.length === 4) { [dx, dy, dw, dh] = a; }
    else if (a.length === 8) { [sx, sy, sw, sh, dx, dy, dw, dh] = a; }
    else throw new Error("drawImage arity " + (a.length + 1));
    if (!(img instanceof FakeCanvas)) throw new Error("drawImage source is not a canvas");
    if (this.imageSmoothingEnabled !== false && (dw !== sw || dh !== sh)) unsupported.add("smoothed scaled drawImage");
    const W = this.cv.width, H = this.cv.height, s = img.data;
    const x0 = Math.round(dx), y0 = Math.round(dy), x1 = Math.round(dx + dw), y1 = Math.round(dy + dh);
    for (let yy = Math.max(0, y0); yy < Math.min(H, y1); yy++) {
      const syy = Math.floor(sy + ((yy - y0 + 0.5) * sh) / (y1 - y0));
      if (syy < 0 || syy >= img.height) continue;
      for (let xx = Math.max(0, x0); xx < Math.min(W, x1); xx++) {
        const sxx = Math.floor(sx + ((xx - x0 + 0.5) * sw) / (x1 - x0));
        if (sxx < 0 || sxx >= img.width) continue;
        const si = (syy * img.width + sxx) * 4;
        if (s[si + 3]) this.blend((yy * W + xx) * 4, s[si], s[si + 1], s[si + 2], s[si + 3]);
      }
    }
  }
  createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; }
  putImageData(img, x, y) {
    this.calls++;
    const W = this.cv.width;
    for (let yy = 0; yy < img.height; yy++) {
      if (y + yy < 0 || y + yy >= this.cv.height) continue;
      const row = img.data.subarray(yy * img.width * 4, (yy + 1) * img.width * 4);
      this.cv.data.set(row.subarray(0, Math.max(0, Math.min(img.width, W - x)) * 4), ((y + yy) * W + x) * 4);
    }
  }
  save() { this._s = [this.fillStyle, this.globalAlpha]; }
  restore() { if (this._s) [this.fillStyle, this.globalAlpha] = this._s; }
}

class FakeCanvas {
  constructor(w = 300, h = 150) { this._w = w; this._h = h; this.data = new Uint8ClampedArray(w * h * 4); this.style = {}; this.listeners = {}; this._ctx = null; }
  get width() { return this._w; } set width(v) { this._w = v; this.data = new Uint8ClampedArray(v * this._h * 4); }
  get height() { return this._h; } set height(v) { this._h = v; this.data = new Uint8ClampedArray(this._w * v * 4); }
  getContext(kind) {
    if (kind !== "2d") throw new Error("only 2d");
    if (!this._ctx) {
      const real = new Ctx2D(this);
      this._ctx = new Proxy(real, {
        get(t, k) {
          if (k in t) { const v = t[k]; return typeof v === "function" ? v.bind(t) : v; }
          if (typeof k === "string") unsupported.add("ctx." + k);
          return () => {};
        },
        set(t, k, v) {
          if (!["fillStyle", "globalAlpha", "imageSmoothingEnabled"].includes(k)) unsupported.add("ctx." + String(k) + "=");
          t[k] = v; return true;
        },
      });
    }
    return this._ctx;
  }
  addEventListener(n, fn) { (this.listeners[n] ||= []).push(fn); }
  dispatch(n, e) { for (const fn of this.listeners[n] || []) fn(e); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1920, height: 1080 }; }
}

function png(cv) {
  const { width: w, height: h, data } = cv;
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) { raw[y * (w * 4 + 1)] = 0; Buffer.from(data.buffer, y * w * 4, w * 4).copy(raw, y * (w * 4 + 1) + 1); }
  const chunk = (type, body) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(body.length);
    const tb = Buffer.concat([Buffer.from(type), body]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(tb) >>> 0);
    return Buffer.concat([len, tb, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
// count pixels in a box that exactly match any of the given #rrggbb colours
function countPixels(cv, x0, y0, x1, y1, colors) {
  const want = colors.map(parseColor);
  let n = 0;
  for (let y = Math.max(0, y0); y < Math.min(cv.height, y1); y++)
    for (let x = Math.max(0, x0); x < Math.min(cv.width, x1); x++) {
      const i = (y * cv.width + x) * 4, d = cv.data;
      if (want.some(c => d[i] === c[0] && d[i + 1] === c[1] && d[i + 2] === c[2])) n++;
    }
  return n;
}

function shot(cv, name) {
  if (!PNG_DIR) return;
  mkdirSync(PNG_DIR, { recursive: true });
  // 2x nearest-neighbour, as on a 1920x1080 display
  const big = new FakeCanvas(cv.width * 2, cv.height * 2);
  const g = big.getContext("2d"); g.imageSmoothingEnabled = false;
  g.drawImage(cv, 0, 0, cv.width, cv.height, 0, 0, big.width, big.height);
  writeFileSync(join(PNG_DIR, name + ".png"), png(big));
}

// ---------- EventSource over fetch ----------

const openStreams = new Set();
class NodeEventSource {
  constructor(url) {
    this.url = new URL(url, BASE).href; this.listeners = {}; this.readyState = 0; this.lastId = ""; this.closed = false;
    openStreams.add(this);
    this.connect();
  }
  addEventListener(n, fn) { (this.listeners[n] ||= []).push(fn); }
  emit(n, e) { if (n === "open" && this.onopen) this.onopen(e); if (n === "error" && this.onerror) this.onerror(e); for (const fn of this.listeners[n] || []) fn(e); }
  async connect() {
    this.ac = new AbortController();
    try {
      const r = await fetch(this.url, { signal: this.ac.signal, headers: this.lastId ? { "Last-Event-ID": this.lastId } : {} });
      if (!r.ok || !String(r.headers.get("content-type")).includes("text/event-stream")) throw new Error("bad SSE response " + r.status);
      this.readyState = 1; this.emit("open", {});
      const dec = new TextDecoder();
      let buf = "";
      for await (const chunk of r.body) {
        buf += dec.decode(chunk, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          let ev = "message", data = [], id = null;
          for (const line of block.split("\n")) {
            if (line.startsWith(":")) continue;
            const c = line.indexOf(":"); const f = c < 0 ? line : line.slice(0, c); let v = c < 0 ? "" : line.slice(c + 1);
            if (v.startsWith(" ")) v = v.slice(1);
            if (f === "event") ev = v; else if (f === "data") data.push(v); else if (f === "id") id = v;
          }
          if (id !== null) this.lastId = id;
          if (data.length) this.emit(ev, { data: data.join("\n"), lastEventId: this.lastId });
        }
      }
    } catch (e) { if (this.closed) return; }
    if (this.closed) return;
    this.readyState = 0; this.emit("error", {});
    setTimeout(() => !this.closed && this.connect(), 1000);
  }
  close() { this.closed = true; this.readyState = 2; this.ac && this.ac.abort(); openStreams.delete(this); }
}

// ---------- AudioContext stub (counts nodes) ----------

const audio = { contexts: 0, oscillators: 0, sources: 0, filters: 0, loops: 0 };
const param = (value = 0) => ({ value, setValueAtTime() {}, exponentialRampToValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {}, cancelScheduledValues() {} });
class FakeAudioContext {
  constructor() { audio.contexts++; this.currentTime = 0; this.sampleRate = 8000; this.state = "running"; this.destination = {}; this._t = setInterval(() => { this.currentTime += 0.05; }, 50); this._t.unref(); }
  node(extra) { return { connect() {}, disconnect() {}, start() {}, stop() {}, ...extra }; }
  createGain() { return this.node({ gain: param(1) }); }
  createOscillator() { audio.oscillators++; return this.node({ frequency: param(440), detune: param(), type: "sine" }); }
  createBiquadFilter() { audio.filters++; return this.node({ frequency: param(350), Q: param(1), gain: param(), type: "lowpass" }); }
  createBufferSource() {
    audio.sources++;
    const n = this.node({ buffer: null, playbackRate: param(1) });
    let loop = false;
    Object.defineProperty(n, "loop", { get: () => loop, set: v => { if (v && !loop) audio.loops++; loop = !!v; }, enumerable: true });
    return n;
  }
  createBuffer(ch, len) { const d = new Float32Array(len); return { getChannelData: () => d }; }
  resume() { return Promise.resolve(); }
}

// ---------- run ----------

const uncaught = [];
process.on("uncaughtException", e => { uncaught.push(String(e && e.stack || e)); console.error("uncaught", e); });
process.on("unhandledRejection", e => { uncaught.push(String(e && e.stack || e)); console.error("unhandled", e); });

async function main() {
  // 1. page and modules are served, and match the files on disk
  const page = await fetch(BASE);
  const html = await page.text();
  check("GET / serves the race UI", page.ok && html.includes('<canvas id="screen"') && html.includes('src="js/main.js"'), `status ${page.status}`);
  const mods = ["main.js", "model.js", "ui.js", "audio.js", "theme.js", "font.js", "track.js", "sprites.js", "fx.js", "music.js"];
  for (const m of mods) {
    const r = await fetch(new URL("js/" + m, BASE));
    const body = await r.text();
    const disk = readFileSync(join(WEB, "js", m), "utf8");
    check(`GET js/${m}`, r.ok && /javascript/.test(r.headers.get("content-type")) && body === disk,
      `${r.status} ${r.headers.get("content-type")}${body === disk ? "" : " (differs from disk: rebuild the binary)"}`);
  }
  const html2 = html + mods.map(m => readFileSync(join(WEB, "js", m), "utf8")).join("\n");
  check("no external URLs in the UI", !/https?:\/\//.test(html2));

  // 2. boot with the fake DOM
  const canvas = new FakeCanvas(960, 540);
  const winListeners = {};
  const win = {
    innerWidth: 1920, innerHeight: 1080, devicePixelRatio: 1,
    addEventListener(n, fn) { (winListeners[n] ||= []).push(fn); },
    requestAnimationFrame: cb => setTimeout(() => cb(performance.now()), 16),
  };
  globalThis.document = { createElement: t => { if (t !== "canvas") throw new Error("createElement " + t); return new FakeCanvas(); }, getElementById: () => canvas };
  globalThis.AudioContext = FakeAudioContext;
  globalThis.NEXUS_NO_AUTOBOOT = true;
  const { boot } = await import(join(WEB, "js", "main.js"));
  const app = boot({ canvas, win, base: BASE, EventSource: NodeEventSource, fetch: (u, o) => fetch(new URL(u, BASE), o) });
  const press = key => { for (const fn of winListeners.keydown || []) fn({ key, preventDefault() {} }); };
  const clickAt = (x, y) => canvas.dispatch("mousedown", { clientX: x * 2, clientY: y * 2 });
  const race = app.race, model = app.model;

  check("canvas scaled by an integer", app.scale === 2 && canvas.style.width === "1920px", `scale ${app.scale}, ${canvas.style.width}`);
  check("/stream connects and sends state", await until(() => app.stats.state > 0 && race.connected, 5000), `states ${app.stats.state}`);
  await until(() => model.lanes.size >= 30, 15000);
  check("lanes on the track", model.lanes.size >= 30, `${model.lanes.size} lanes`);
  check("failures arrive over SSE", await until(() => app.stats.failure > 0, 20000), `${app.stats.failure} failures`);
  await sleep(4000);
  check("frames rendered", race.frames > 30, `${race.frames} frames`);
  check("cars are clickable", race.carHits.length === model.lanes.size, `${race.carHits.length} hit areas`);
  check("timing tower lists every lane", race.hits.filter(h => h.name.startsWith("row:")).length === model.lanes.size);
  shot(canvas, "1-race");

  // every failure effect at once, on five visible cars
  const types = ["sanitizer", "crash", "hang", "error_exit", "visual"];
  const victims = model.ranked().slice(0, 5);
  victims.forEach((l, i) => race.onFailure({ event_id: "smoke-" + i, ts: new Date().toISOString(), heat_id: model.heat.id,
    lane_id: l.lane_id, game: l.game, fault: l.fault, type: types[i], detail: "smoke " + types[i], scored: false, points: 0, n: 0, lane_score: l.score }));
  check("effects triggered for every type", types.every((t, i) => race.fx.cars.get(victims[i].lane_id)?.type === t));
  await sleep(350);
  shot(canvas, "1b-effects");

  // crash + sanitizer are pilot deaths: banner, kill-cam, blood decals
  const banner = race.banner;
  check("death banner shown", !!banner && /PILOT TERMINATED|DOUBLE KILL|MULTI KILL/.test(banner.title), banner ? banner.title : "no banner");
  const bannerPx = countPixels(canvas, 0, 220, 704, 266, ["#ff1f3a", "#eef6ff"]);
  check("death banner drawn", bannerPx > 200, `${bannerPx} red/white pixels in the banner band`);
  if (model.phase() !== "pit") {
    await sleep(150);
    check("kill-cam active", !!race.cam && race.cam.frames.length > 0, race.cam ? `${race.cam.frames.length} frames` : "no kill-cam");
  }
  await sleep(350);
  shot(canvas, "1c-death");
  await until(() => race.fx.decals.length > 0, 1500);
  check("blood decals on the deck", race.fx.decals.length > 0, `${race.fx.decals.length} decals`);

  // 3. sound on: failures play synthesized SFX
  press("m");
  check("unmute creates an AudioContext", !app.sfx.muted && audio.contexts === 1);
  const osc0 = audio.oscillators;
  await until(() => audio.oscillators > osc0, 15000);
  check("SFX play on events", audio.oscillators > osc0, `${audio.oscillators} oscillators, ${audio.sources} noise sources`);

  // music: on with sound, B toggles it alone
  check("music plays when unmuted", app.music.playing === true);
  const notes0 = app.music.notes;
  check("music schedules notes", await until(() => app.music.notes > notes0, 1000, 50), `${app.music.notes - notes0} notes, ${audio.filters} filters, ${audio.loops} looped sources`);
  press("b");
  check("B stops the music only", app.music.playing === false && !app.sfx.muted);
  press("b");
  check("B restarts the music", app.music.playing === true && !app.sfx.muted);
  press("m");
  check("mute again", app.sfx.muted);
  check("mute stops the music", app.music.playing === false);

  // 4. click the leading car: panel, card, live screen
  const lead = model.ranked()[0];
  const hitc = race.carHits.find(c => c.id === lead.lane_id);
  clickAt(hitc.x, hitc.y);
  check("click on a car selects it", race.sel === lead.lane_id, `${app.lastClick} -> ${race.sel}`);
  check("lane card loads", await until(() => race.card && race.card.lane.lane_id === lead.lane_id, 5000), `${(race.card && race.card.failures.length) || 0} failures in log`);
  check("live screen streams", await until(() => app.stats.screen > 0 && race.frameCanvas, 8000), race.frame ? `${race.frame.cols}x${race.frame.rows}` : "no frame");
  await sleep(500);
  shot(canvas, "2-panel");
  // a Brogue lane shows 100x34
  const brogue = [...model.lanes.values()].find(l => l.game === "brogue");
  if (brogue) {
    press("Escape");
    race.select(brogue.lane_id);
    check("brogue screen is 100x34", await until(() => race.frame && race.frame.cols === 100 && race.frame.rows === 34, 8000));
    await sleep(500);
    shot(canvas, "3-panel-brogue");
  }
  // focus on wall: POST and the focus SSE event comes back
  const f0 = app.stats.focus;
  press("f");
  check("focus on wall posts and is rebroadcast", await until(() => app.stats.focusPosts === 1 && app.stats.focus > f0, 5000));
  const logRow = race.hits.find(h => h.name.startsWith("log:"));
  if (logRow) { clickAt(logRow.x + 5, logRow.y + 4); check("click a failure log row", race.logSel === 0 || race.logSel >= 0); }
  press("Escape");
  check("escape closes the panel", race.sel === null);

  // 5. standings tabs
  for (const [k, name] of [["2", "constructors"], ["3", "games"], ["4", "season"]]) {
    press(k); await sleep(300);
    check(`tab ${name}`, race.tab === name);
    shot(canvas, `4-tab-${name}`);
  }
  const tab = race.hits.find(h => h.name === "tab:heat");
  clickAt(tab.x + 2, tab.y + 2);
  check("click the HEAT tab", race.tab === "heat");

  // 6. pit: podium + attract replays, then the next heat
  if (args["wait-pit"]) {
    const heat0 = model.heat.id;
    check("pit starts", await until(() => model.phase() === "pit", 90000, 250), `${heat0}`);
    await sleep(2500);
    check("pit event received with a podium", app.stats.pit > 0 && Array.isArray(model.podium), `${(model.podium || []).length} on podium`);
    check("attract mode has moments", race.replay && race.replay.list.length > 0, `${race.replay ? race.replay.list.length : 0} moments`);
    shot(canvas, "5-pit");
    await sleep(3000);
    shot(canvas, "6-pit-replay");
    check("next heat starts", await until(() => model.heat.id !== heat0 && model.phase() === "race", 20000, 250), model.heat.id);
    check("heat event received", app.stats.heat > 0);
    await sleep(800);
    shot(canvas, "7-green-flag");
  }

  check("no JS errors", app.errors.length === 0 && uncaught.length === 0, [...app.errors, ...uncaught].slice(0, 3).join(" | "));
  check("only pixel-safe canvas calls", unsupported.size === 0, [...unsupported].join(", "));
  app.stop();
  for (const s of [...openStreams]) s.close();
  console.log(JSON.stringify({ passed: results.length - failed, failed, stats: app.stats, audio }, null, 0));
  process.exit(failed ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(2); });
