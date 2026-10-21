// Boot: wires the public scoreboard API to the race screen.
//   GET /stream                    state, failure, ticker, heat, pit, focus
//   GET /api/lanes/{id}            config card + failure log (panel, attract backfill)
//   GET /lanes/{id}/screen/stream  live screen of the selected lane
//   POST /wall/focus               "focus on wall"
// URLs are relative to the page, so the UI also works behind a path prefix.

import { Model } from "./model.js";
import { Race } from "./ui.js";
import { Sfx } from "./audio.js";
import { W, H } from "./theme.js";

export function boot(opts = {}) {
  const win = opts.win || globalThis;
  const doc = opts.doc || globalThis.document;
  const canvas = opts.canvas || doc.getElementById("screen");
  const base = opts.base ?? "";
  const ES = opts.EventSource || globalThis.EventSource;
  const fetchFn = opts.fetch || ((...a) => globalThis.fetch(...a));
  const raf = opts.raf || (cb => win.requestAnimationFrame(cb));
  const now = opts.now || (() => performance.now());

  const model = new Model();
  const sfx = new Sfx();
  const app = { model, sfx, errors: [], stats: { state: 0, failure: 0, ticker: 0, heat: 0, pit: 0, focus: 0, screen: 0, card: 0, focusPosts: 0 } };
  const enc = encodeURIComponent;
  let screenES = null;

  const fail = (where, err) => {
    app.errors.push(`${where}: ${err && err.stack || err}`);
    if (app.errors.length > 50) app.errors.shift();
    console.error(where, err);
  };

  async function getJSON(path) {
    const r = await fetchFn(base + path, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }

  async function loadCard(id) {
    try {
      const card = await getJSON(`api/lanes/${enc(id)}`);
      app.stats.card++;
      race.setCard(card);
    } catch (e) { if (race.sel === id) race.toast("LANE NOT FOUND", "#ff004d"); }
  }

  let refreshTimer = null;
  const race = new Race(model, sfx, {
    onSelect(id) {
      if (screenES) { screenES.close(); screenES = null; }
      if (!id) return;
      loadCard(id);
      screenES = new ES(`${base}lanes/${enc(id)}/screen/stream`);
      screenES.addEventListener("screen", e => {
        try { app.stats.screen++; race.setFrame(JSON.parse(e.data)); } catch (err) { fail("screen", err); }
      });
    },
    onRefresh(id) {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => { refreshTimer = null; if (race.sel === id) loadCard(id); }, 400);
    },
    async onFocus(id) {
      try {
        const r = await fetchFn(base + "wall/focus", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lane_id: id }) });
        app.stats.focusPosts++;
        race.toast(r.ok ? `WALL FOCUS: ${id}` : `FOCUS FAILED (${r.status})`, r.ok ? "#29adff" : "#ff004d");
      } catch (e) { race.toast("FOCUS FAILED", "#ff004d"); }
    },
    onPit(p) { backfill((p.podium || []).map(r => r.lane_id)); },
  });
  app.race = race;

  // Attract mode needs the heat's failures; after a reload we only have what
  // arrived since, so pull the failure logs of the front runners.
  async function backfill(ids) {
    for (const id of ids.slice(0, 10)) {
      try { const c = await getJSON(`api/lanes/${enc(id)}`); model.mergeFailures(c.failures); } catch (e) { /* lane gone */ }
    }
  }

  let first = true;
  let es = null;
  const handlers = {};
  const on = (name, fn) => { handlers[name] = fn; };
  // EventSource retries by itself (sending Last-Event-ID) unless the server
  // answered with an error; then it is CLOSED and we start a fresh one.
  function connect() {
    es = app.es = new ES(base + "stream");
    es.onopen = () => { race.connected = true; };
    es.onerror = () => {
      race.connected = false;
      if (es.readyState === 2 && !app.stopped) setTimeout(connect, 3000);
    };
    for (const [name, fn] of Object.entries(handlers)) es.addEventListener(name, e => {
      race.connected = true;
      app.stats[name]++;
      try { fn(JSON.parse(e.data)); } catch (err) { fail(name, err); }
    });
  }
  on("state", s => {
    race.onState(s);
    if (first && s.full) {
      first = false;
      backfill((model.standings.lanes || []).filter(e => e.score > 0).map(e => e.lane_id));
    }
  });
  on("failure", f => race.onFailure(f));
  on("ticker", t => race.onTicker(t));
  on("heat", h => race.onHeat(h));
  on("pit", p => race.onPit(p));
  on("focus", f => race.onFocus(f));
  connect();

  // keep the open panel's card fresh (status, turn, tokens)
  app.cardTimer = setInterval(() => { if (race.sel) loadCard(race.sel); }, 5000);

  // integer pixel scaling (fractional only if the window is smaller than W x H)
  function fit() {
    const dpr = win.devicePixelRatio || 1;
    const raw = Math.min((win.innerWidth * dpr) / W, (win.innerHeight * dpr) / H);
    const s = raw >= 1 ? Math.floor(raw) : raw;
    canvas.style.width = (W * s) / dpr + "px";
    canvas.style.height = (H * s) / dpr + "px";
    app.scale = s;
  }
  canvas.width = W; canvas.height = H;
  fit();
  win.addEventListener("resize", fit);

  const toLogical = e => {
    const r = canvas.getBoundingClientRect();
    return [((e.clientX - r.left) * W) / r.width, ((e.clientY - r.top) * H) / r.height];
  };
  canvas.addEventListener("mousedown", e => {
    try { const [x, y] = toLogical(e); app.lastClick = race.click(x, y); } catch (err) { fail("click", err); }
  });
  canvas.addEventListener("mousemove", e => {
    const [x, y] = toLogical(e);
    const over = race.hits.some(h => x >= h.x && y >= h.y && x < h.x + h.w && y < h.y + h.h && h.name !== "panel" && h.name !== "pit")
      || race.carHits.some(c => Math.hypot(c.x - x, c.y - y) <= c.r);
    canvas.style.cursor = over ? "pointer" : "default";
  });
  canvas.addEventListener("wheel", e => race.wheel(e.deltaY), { passive: true });
  win.addEventListener("keydown", e => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    try { if (race.key(e.key) && e.preventDefault) e.preventDefault(); } catch (err) { fail("key", err); }
  });

  const ctx = canvas.getContext("2d");
  let last = now();
  function frame() {
    if (app.stopped) return;
    raf(frame);
    const t = now();
    const dt = (t - last) / 1000;
    if (dt < 1 / 32) return; // ~30 fps is plenty for pixel art
    last = t;
    try { race.render(ctx, Math.min(dt, 0.1)); } catch (err) { fail("render", err); }
  }
  raf(frame);
  app.stop = () => { app.stopped = true; es.close(); if (screenES) screenES.close(); clearInterval(app.cardTimer); };
  return app;
}

if (typeof document !== "undefined" && !globalThis.NEXUS_NO_AUTOBOOT) globalThis.nexus = boot();
