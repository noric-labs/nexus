// The race screen: track with every lane as a car, timing tower with
// standings tabs, heat clock, ticker, pit podium + attract replays, and the
// lane side panel. Everything is drawn on one W x H logical canvas using only
// fillRect, drawImage and putImageData, so it scales crisply by integers.

import { W, H, C, TYPE_COLOR, TYPE_SHORT, TYPES, faultColor, gameColor, gameShort, fmtPts, fmtClock, fmtTime } from "./theme.js";
import { text, textR, textC, textWidth, fit, termChar, newCanvas, CW } from "./font.js";
import { Track } from "./track.js";
import { drawCar, carSprite } from "./sprites.js";
import { Fx } from "./fx.js";

const TOP = 20, TICK_Y = 526, TOWER_X = 706, TOWER_W = 252;
const TABS = [["heat", "HEAT"], ["constructors", "CONSTR"], ["games", "GAMES"], ["season", "SEASON"]];
const PANEL = { x: 232, y: 24, w: 724, h: 498 };
const SCR = { cw: 6, ch: 8, maxCols: 100, maxRows: 34 };
const REPLAY_SECS = 4.5;

export class Race {
  constructor(model, sfx, hooks = {}) {
    this.m = model;
    this.sfx = sfx;
    this.hooks = hooks; // onSelect(id|null), onFocus(id)
    this.track = new Track();
    this.fx = new Fx(11);
    this.cars = new Map();     // lane -> {d, lat}
    this.hits = [];            // clickable regions this frame
    this.carHits = [];
    this.flash = new Map();    // lane -> {type, t}
    this.tab = "heat";
    this.auto = false;
    this.autoT = 0;
    this.sel = null;           // selected lane id
    this.card = null;          // /api/lanes/{id}
    this.frame = null;         // latest screen frame of the selected lane
    this.frameCanvas = null;
    this.logScroll = 0;
    this.logSel = -1;
    this.marquee = [];
    this.toastMsg = null;
    this.connected = false;
    this.t = 0;
    this.goT = -9;             // time of the last green flag
    this.lastLight = 0;
    this.replay = null;
    this.frames = 0;
    this.bg = newCanvas(704, H);
    const g = this.bg.getContext("2d");
    this.track.paint(g, 704, H);
    this.paintScenery(g);
  }

  // ---------- events ----------

  onState(s) {
    const before = this.m.heat && this.m.heat.id;
    this.m.applyState(s);
    if (before && this.m.heat && before !== this.m.heat.id) this.greenFlag();
  }

  onFailure(f) {
    const l = this.m.applyFailure(f);
    if (!l) return;
    this.fx.trigger(f.lane_id, f.type, f.scored ? f.points : 0);
    this.flash.set(f.lane_id, { type: f.type, t: this.t });
    this.sfx.play(f.type);
    if (f.lane_id === this.sel && this.hooks.onRefresh) this.hooks.onRefresh(f.lane_id);
  }

  onTicker(t) {
    this.m.applyTicker(t);
    const col = { first_blood: C.red, new_cluster: TYPE_COLOR.sanitizer, lead_change: C.gold }[t.kind] || C.text;
    this.say(t.text, col);
    if (t.kind === "lead_change") this.sfx.play("lead");
    if (t.kind === "new_cluster") this.sfx.play("newcluster");
  }

  onHeat(h) {
    this.m.applyHeat(h);
    this.say(`GREEN FLAG! ${h.id} IS UNDER WAY`, C.green);
    this.greenFlag();
  }

  onPit(p) {
    this.m.applyPit(p);
    const w = (p.podium || [])[0];
    this.say(`CHEQUERED FLAG: ${p.heat_id} IS OVER` + (w ? `. WINNER ${w.lane_id} (${w.game}/${w.fault}) WITH ${fmtPts(w.score)} PTS` : ""), C.white);
    this.sfx.play("flag");
    this.replay = null;
    if (this.hooks.onPit) this.hooks.onPit(p);
  }

  onFocus(f) {
    this.m.focus = { ...f, at: this.t };
  }

  greenFlag() {
    const id = this.m.heat && this.m.heat.id;
    if (id && id === this.goHeat) return; // once per heat (both `heat` and `state` announce it)
    this.goHeat = id;
    this.goT = this.t;
    this.replay = null;
    this.sfx.play("go");
  }

  say(msg, col = C.text) {
    const last = this.marquee[this.marquee.length - 1];
    const x = last ? Math.max(W, last.x + textWidth(last.text) + 48) : W;
    this.marquee.push({ text: String(msg).toUpperCase(), col, x });
    if (this.marquee.length > 40) this.marquee.splice(0, this.marquee.length - 40);
  }

  toast(msg, col = C.gold) { this.toastMsg = { text: msg, col, t: this.t }; }

  select(id) {
    if (id === this.sel) return;
    this.sel = id;
    this.card = null; this.frame = null; this.frameCanvas = null;
    this.logScroll = 0; this.logSel = -1;
    if (this.hooks.onSelect) this.hooks.onSelect(id);
  }

  setCard(card) { if (card && card.lane && card.lane.lane_id === this.sel) this.card = card; }

  setFrame(f) {
    if (!f || f.lane_id && f.lane_id !== this.sel) return;
    this.frame = f;
    this.frameCanvas = this.renderScreen(f);
  }

  // ---------- input ----------

  click(x, y) {
    for (let i = this.hits.length - 1; i >= 0; i--) {
      const h = this.hits[i];
      if (x >= h.x && y >= h.y && x < h.x + h.w && y < h.y + h.h) { h.fn(); return h.name; }
    }
    let best = null, bd = 1e9;
    for (const c of this.carHits) {
      const d = Math.hypot(c.x - x, c.y - y);
      if (d <= c.r && d < bd) { best = c; bd = d; }
    }
    if (best) { this.select(best.id); return "car"; }
    if (this.sel && !this.inPanel(x, y)) { this.select(null); return "close"; }
    return null;
  }

  inPanel(x, y) { return this.sel && x >= PANEL.x && y >= PANEL.y && x < PANEL.x + PANEL.w && y < PANEL.y + PANEL.h; }

  wheel(dy) { if (this.sel) this.logScroll = Math.max(0, this.logScroll + Math.sign(dy) * 3); }

  key(k) {
    const tabs = { 1: "heat", 2: "constructors", 3: "games", 4: "season" };
    if (tabs[k]) { this.tab = tabs[k]; this.auto = false; return true; }
    switch (k.toLowerCase()) {
      case "m": this.sfx.toggle(); return true;
      case "a": this.auto = !this.auto; this.autoT = 0; this.toast(this.auto ? "AUTO TABS ON" : "AUTO TABS OFF"); return true;
      case "escape": this.select(null); return true;
      case "f": if (this.sel && this.hooks.onFocus) this.hooks.onFocus(this.sel); return true;
      case "arrowdown": this.logScroll++; return true;
      case "arrowup": this.logScroll = Math.max(0, this.logScroll - 1); return true;
      case "n": case "p": {
        const r = this.m.ranked();
        if (!r.length) return true;
        let i = r.findIndex(l => l.lane_id === this.sel);
        i = (i + (k.toLowerCase() === "n" ? 1 : r.length - 1)) % r.length;
        this.select(r[i].lane_id);
        return true;
      }
    }
    return false;
  }

  hit(name, x, y, w, h, fn) { this.hits.push({ name, x, y, w, h, fn }); }

  // ---------- frame ----------

  render(ctx, dt, now = Date.now()) {
    this.t += dt;
    this.frames++;
    this.fx.update(dt);
    this.hits = [];
    this.carHits = [];
    if (this.auto) {
      this.autoT += dt;
      if (this.autoT > 15) { this.autoT = 0; const i = TABS.findIndex(t => t[0] === this.tab); this.tab = TABS[(i + 1) % TABS.length][0]; }
    }
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, W, H);

    const [sx, sy] = this.fx.shakeOffset();
    ctx.drawImage(this.bg, 0, TOP, 704, TICK_Y - TOP, sx, TOP + sy, 704, TICK_Y - TOP);
    const pit = this.m.phase() === "pit";
    if (!pit) this.drawInfield(ctx, now);
    this.drawCars(ctx, dt, sx, sy);
    if (pit) this.drawPit(ctx, dt, now);
    this.drawLights(ctx, now);
    this.drawTower(ctx);
    this.drawTopBar(ctx, now);
    this.drawTicker(ctx, dt);
    if (this.sel) this.drawPanel(ctx);
    if (this.toastMsg && this.t - this.toastMsg.t < 2.5) {
      const s = this.toastMsg.text, w = textWidth(s, 2) + 16;
      ctx.fillStyle = C.black; ctx.fillRect(352 - w / 2, 250, w, 28);
      ctx.fillStyle = this.toastMsg.col; ctx.fillRect(352 - w / 2, 250, w, 2); ctx.fillRect(352 - w / 2, 276, w, 2);
      textC(ctx, s, 352, 257, this.toastMsg.col, 2);
    }
  }

  // ---------- scenery ----------

  paintScenery(g) {
    let r = 99;
    const rnd = () => ((r = (Math.imul(r, 1103515245) + 12345) >>> 0) / 4294967296);
    // grandstand along the top straight
    g.fillStyle = "#2a2a3e"; g.fillRect(150, 26, 404, 44);
    g.fillStyle = "#44445e"; g.fillRect(146, 24, 412, 4);
    const crowd = ["#ff004d", "#ffa300", "#ffec27", "#29adff", "#ff77a8", "#fff1e8", "#00e436", "#83769c"];
    for (let y = 31; y < 66; y += 4) for (let x = 154; x < 550; x += 3) {
      if (rnd() < 0.18) continue;
      g.fillStyle = crowd[Math.floor(rnd() * crowd.length)];
      g.fillRect(x, y, 2, 2);
    }
    // advertising boards
    const ads = [["SIGSEGV", "#ff004d"], ["ASAN", "#ff3df5"], ["ENOSPC", "#29adff"], ["SIGWINCH", "#ffa300"], ["UBSAN", "#00e436"]];
    let x = 160;
    for (const [s, col] of ads) {
      const w = textWidth(s) + 8;
      g.fillStyle = "#101018"; g.fillRect(x, 70, w, 9);
      text(g, s, x + 4, 71, col);
      x += w + 12;
    }
    // pit building along the bottom
    g.fillStyle = "#3a3a52"; g.fillRect(150, 474, 404, 40);
    g.fillStyle = "#55557a"; g.fillRect(146, 472, 412, 3);
    for (let i = 0; i < 12; i++) {
      g.fillStyle = i % 2 ? "#1d1d2e" : "#24243a";
      g.fillRect(160 + i * 32, 486, 26, 24);
      g.fillStyle = faultColor(["none", "malloc-1pct", "malloc-0.1pct", "io-save-5pct", "strace-enospc-save", "strace-eintr-read",
        "winch-storm", "rlimit-tight", "time-warp", "keystorm", "x", "y"][i]);
      g.fillRect(160 + i * 32, 480, 26, 3);
    }
    g.fillStyle = "#101018"; g.fillRect(320, 462, 64, 9);
    text(g, "PIT LANE", 328, 463, C.gold);
  }

  // ---------- track and cars ----------

  carTarget(l, gridIdx) {
    const L = this.track.length;
    if (l.score > 0) return { d: (l.score / this.m.lapPoints) * L, lat: this.track.laneLat(l.lane_id) };
    const row = Math.floor(gridIdx / 5), col = gridIdx % 5;
    return { d: -12 - row * 10 - (col % 2) * 4, lat: (col - 2) * 8 };
  }

  drawCars(ctx, dt, sx, sy) {
    const lanes = this.m.ranked();
    const grid = lanes.filter(l => !(l.score > 0)).sort((a, b) => String(a.lane_id).localeCompare(String(b.lane_id)));
    const gi = new Map(grid.map((l, i) => [l.lane_id, i]));
    const L = this.track.length;
    const placed = [];
    for (const l of lanes) {
      const id = l.lane_id;
      const tg = this.carTarget(l, gi.get(id) ?? 0);
      let c = this.cars.get(id);
      if (!c) { c = { d: tg.d, lat: tg.lat }; this.cars.set(id, c); }
      if (!this.fx.stalled(id)) {
        const diff = tg.d - c.d;
        if (diff < -0.5 || diff > L * 4) c.d = tg.d;          // new heat (back to the grid) or a reload
        else if (diff > 0) c.d += Math.min(diff, Math.max(40, diff * 2.2) * dt);
      }
      c.lat += (tg.lat - c.lat) * Math.min(1, dt * 3);
      const p = this.track.at(c.d, c.lat);
      placed.push({ l, p, big: l.pos > 0 && l.pos <= 10 && l.score > 0 });
    }
    this.fx.drawUnder(ctx);
    // small cars first, then the top 10 on top (leader last)
    placed.sort((a, b) => (a.big - b.big) || (b.l.pos || 999) - (a.l.pos || 999));
    for (const { l, p, big } of placed) {
      const id = l.lane_id;
      const m = this.fx.car(id);
      let ang = p.ang + (m ? m.spin : 0);
      let x = p.x + sx, y = p.y + sy;
      if (m && m.jitter) { x += Math.cos(p.ang) * m.jitter; y += Math.sin(p.ang) * m.jitter; }
      this.fx.setPos(id, x, y, p.ang);
      this.carHits.push({ id, x, y, r: big ? 10 : 6 });
      if (m && m.hidden) continue;
      let body = faultColor(l.fault), acc = gameColor(l.game);
      if (l.status === "ended") { body = "#4a4a5a"; acc = "#5f5f6f"; }
      else if (l.status === "stale" && Math.floor(this.t * 2) % 2) { body = "#6a6a7a"; }
      if (m && m.flash) { body = "#ffffff"; acc = "#ffffff"; }
      if (m && m.glitch) {
        const gx = Math.floor(this.fx.rnd() * 5) - 2;
        drawCar(ctx, x + gx, y, ang, big, TYPE_COLOR.visual, "#ff00ff");
        ctx.fillStyle = Math.floor(this.t * 30) % 2 ? "#ff00ff" : TYPE_COLOR.visual;
        ctx.fillRect(Math.round(x - 6 + gx * 2), Math.round(y - 1 + gx), 12, 1);
      } else {
        drawCar(ctx, x, y, ang, big, body, acc);
      }
      if (id === this.sel && Math.floor(this.t * 3) % 2 === 0) {
        const r = big ? 10 : 7;
        ctx.fillStyle = C.white;
        ctx.fillRect(Math.round(x - r), Math.round(y - r), 3, 1); ctx.fillRect(Math.round(x - r), Math.round(y - r), 1, 3);
        ctx.fillRect(Math.round(x + r - 2), Math.round(y - r), 3, 1); ctx.fillRect(Math.round(x + r), Math.round(y - r), 1, 3);
        ctx.fillRect(Math.round(x - r), Math.round(y + r), 3, 1); ctx.fillRect(Math.round(x - r), Math.round(y + r - 2), 1, 3);
        ctx.fillRect(Math.round(x + r - 2), Math.round(y + r), 3, 1); ctx.fillRect(Math.round(x + r), Math.round(y + r - 2), 1, 3);
      }
      if (big) this.plate(ctx, x, y - 15, l);
    }
    this.fx.drawOver(ctx);
  }

  plate(ctx, x, y, l) {
    const s = String(l.pos), w = textWidth(s) + 4;
    x = Math.round(x - w / 2); y = Math.round(y);
    const bg = l.pos === 1 ? C.gold : l.pos === 2 ? C.silver : l.pos === 3 ? C.bronze : C.white;
    ctx.fillStyle = faultColor(l.fault); ctx.fillRect(x - 1, y - 1, w + 2, 11);
    ctx.fillStyle = bg; ctx.fillRect(x, y, w, 9);
    text(ctx, s, x + 2, y + 1, C.black);
  }

  drawInfield(ctx, now) {
    const cx = this.track.cx, h = this.m.heat;
    textC(ctx, h ? `HEAT ${h.id}` : "WAITING FOR SCOREBOARD", cx, 134, C.muted);
    const rem = this.m.remaining(now);
    const col = rem < 60000 && Math.floor(this.t * 2) % 2 ? C.red : C.white;
    textC(ctx, fmtClock(rem), cx, 146, col, 4);
    const lead = this.m.leader();
    if (lead) {
      const l = this.m.lanes.get(lead.lane_id) || {};
      textC(ctx, "LEADER", cx, 186, C.muted);
      textC(ctx, fit(lead.lane_id, 12), cx, 196, C.gold, 2);
      textC(ctx, `${fmtPts(lead.score)} PTS  LAP ${Math.floor(lead.score / this.m.lapPoints) + 1}`, cx, 216, C.text);
      textC(ctx, `${gameShort(l.game)} / ${fit(l.fault || "?", 18)}`, cx, 226, faultColor(l.fault));
    } else {
      textC(ctx, "NO SCORE YET", cx, 196, C.muted, 2);
      textC(ctx, "FIRST FAILURE TAKES THE LEAD", cx, 216, C.muted);
    }
    // failures this heat by type
    const tc = this.m.typeCounts();
    const x0 = cx - 120;
    TYPES.forEach((t, i) => {
      const x = x0 + (i % 3) * 82, y = 246 + Math.floor(i / 3) * 14;
      ctx.fillStyle = TYPE_COLOR[t]; ctx.fillRect(x, y, 7, 7);
      text(ctx, `${TYPE_SHORT[t]} ${tc[t] || 0}`, x + 10, y, C.text);
    });
    const c = this.m.counts();
    textC(ctx, `${c.running || 0} RUNNING  ${c.stale || 0} STALE  ${c.ended || 0} ENDED`, cx, 280, C.muted);
    // the most recent failure
    const f = this.m.failures[this.m.failures.length - 1];
    if (f) {
      textC(ctx, "LATEST", cx, 300, C.muted);
      textC(ctx, `${fit(f.lane_id, 8)} ${TYPE_SHORT[f.type] || f.type} ${f.scored ? "+" + fmtPts(f.points) : ""}`, cx, 310, TYPE_COLOR[f.type] || C.text);
      textC(ctx, fit(f.detail || f.stack_hash || "", 44), cx, 320, C.muted);
    }
    textC(ctx, `1 LAP = ${fmtPts(this.m.lapPoints)} PTS`, cx, 352, C.dim);
    textC(ctx, "BODY = CONSTRUCTOR  STRIPE = GAME", cx, 362, C.dim);
    textC(ctx, "CLICK A CAR  [1-4] TABS  [M] SOUND", cx, 384, C.dim);
  }

  drawLights(ctx, now) {
    const pit = this.m.phase() === "pit";
    const rem = this.m.remaining(now);
    const go = this.t - this.goT < 2.2;
    if (!(pit && rem <= 5000) && !go) return;
    const cx = this.track.cx, y = 86;
    ctx.fillStyle = C.black; ctx.fillRect(cx - 62, y - 4, 124, 22);
    const lit = go ? 5 : Math.min(5, 5 - Math.floor(rem / 1000));
    if (pit && lit !== this.lastLight) { this.lastLight = lit; if (lit > 0) this.sfx.play("light"); }
    for (let i = 0; i < 5; i++) {
      ctx.fillStyle = go ? (Math.floor(this.t * 6) % 2 ? C.green : "#00a028") : i < lit ? C.red : "#3a1020";
      ctx.fillRect(cx - 56 + i * 24, y, 14, 14);
    }
    if (go) textC(ctx, "GO! GO! GO!", cx, y + 20, C.green, 2);
  }

  // ---------- pit: podium + attract replays ----------

  podium() {
    if (this.m.podium && this.m.podium.length) return this.m.podium;
    const sp = this.m.config.season_points || [];
    return (this.m.standings.lanes || []).filter(e => e.score > 0).slice(0, 10).map(e => {
      const l = this.m.lanes.get(e.lane_id) || {};
      return { pos: e.pos, lane_id: e.lane_id, game: l.game, fault: l.fault, model: l.model, score: e.score, points: sp[e.pos - 1] || 0 };
    });
  }

  drawPit(ctx, dt, now) {
    ctx.fillStyle = "rgba(6,6,16,0.84)";
    ctx.fillRect(0, TOP, 704, TICK_Y - TOP);
    this.hit("pit", 0, TOP, 704, TICK_Y - TOP, () => {});
    // chequered bars
    for (let x = 0; x < 704; x += 4) for (let r = 0; r < 2; r++) {
      ctx.fillStyle = ((x >> 2) + r) % 2 ? C.white : C.black;
      ctx.fillRect(x, TOP + 2 + r * 4, 4, 4); ctx.fillRect(x, TICK_Y - 10 + r * 4, 4, 4);
    }
    const h = this.m.heat || {};
    textC(ctx, `CHEQUERED FLAG  ${h.id || ""}`, 352, 36, C.white, 2);
    textC(ctx, `PIT STOP - NEXT HEAT ${h.next_id || ""} IN ${fmtClock(this.m.remaining(now))}`, 352, 58, C.amber);

    // podium
    const pod = this.podium();
    const base = 300, blocks = [[2, 40, 56, C.silver], [1, 135, 74, C.gold], [3, 230, 44, C.bronze]];
    textC(ctx, "PODIUM", 175, 122, C.muted);
    for (const [pos, x, hgt, col] of blocks) {
      const r = pod.find(p => p.pos === pos);
      ctx.fillStyle = col; ctx.fillRect(x, base - hgt, 90, hgt);
      ctx.fillStyle = "rgba(0,0,0,0.25)"; ctx.fillRect(x, base - hgt, 90, 4);
      textC(ctx, String(pos), x + 45, base - hgt + 7, C.black, 3);
      if (!r) continue;
      this.hit("podium:" + r.lane_id, x, base - hgt - 46, 90, hgt + 46, () => this.select(r.lane_id));
      const bob = Math.round(Math.sin(this.t * 3 + pos) * 1.5);
      drawCar(ctx, x + 45, base - hgt - 14 + bob, 0, true, faultColor(r.fault), gameColor(r.game), 2);
      textC(ctx, fit(r.lane_id, 14), x + 45, base - hgt - 44, C.white);
      textC(ctx, `${fmtPts(r.score)} PTS`, x + 45, base - hgt - 34, col);
      textC(ctx, `+${r.points}`, x + 45, base - 10, C.black);
    }
    ctx.fillStyle = "#2a2a3e"; ctx.fillRect(30, base, 300, 4);
    if (!pod.length) textC(ctx, "NO FINISHERS", 175, 200, C.muted, 2);
    let y = base + 14;
    for (const r of pod.filter(p => p.pos > 3).slice(0, 7)) {
      text(ctx, `P${r.pos}`.padEnd(4), 34, y, C.muted);
      ctx.fillStyle = faultColor(r.fault); ctx.fillRect(58, y, 4, 7);
      ctx.fillStyle = gameColor(r.game); ctx.fillRect(62, y, 2, 7);
      text(ctx, fit(r.lane_id, 10), 68, y, C.text);
      text(ctx, gameShort(r.game), 134, y, gameColor(r.game));
      text(ctx, fit(r.fault, 14), 158, y, C.muted);
      textR(ctx, fmtPts(r.score), 290, y, C.text);
      textR(ctx, `+${r.points}`, 326, y, C.gold);
      this.hit("podium:" + r.lane_id, 30, y - 1, 300, 10, () => this.select(r.lane_id));
      y += 11;
    }
    this.drawReplay(ctx, dt);
  }

  drawReplay(ctx, dt) {
    const X = 352, Y = 112, Wd = 336, Hd = 380;
    ctx.fillStyle = "#0e0e20"; ctx.fillRect(X, Y, Wd, Hd);
    ctx.fillStyle = C.line; ctx.fillRect(X, Y, Wd, 1); ctx.fillRect(X, Y + Hd - 1, Wd, 1); ctx.fillRect(X, Y, 1, Hd); ctx.fillRect(X + Wd - 1, Y, 1, Hd);
    if (!this.replay) this.replay = { i: -1, t: REPLAY_SECS, list: [], fx: new Fx(5) };
    const rp = this.replay;
    rp.t += dt;
    if (rp.t >= REPLAY_SECS) {
      rp.list = this.m.moments(8);
      rp.i = rp.list.length ? (rp.i + 1) % rp.list.length : -1;
      rp.t = 0; rp.d = 0; rp.fired = false; rp.fx = new Fx(5 + rp.i);
    }
    const live = Math.floor(this.t * 2) % 2;
    ctx.fillStyle = live ? C.red : "#601020"; ctx.fillRect(X + 8, Y + 8, 6, 6);
    text(ctx, "INSTANT REPLAY", X + 18, Y + 7, C.white);
    if (rp.i < 0) {
      textC(ctx, "NO SCORING MOMENTS", X + Wd / 2, Y + 150, C.muted, 2);
      textC(ctx, "THIS HEAT", X + Wd / 2, Y + 172, C.muted, 2);
      return;
    }
    textR(ctx, `${rp.i + 1}/${rp.list.length}`, X + Wd - 8, Y + 7, C.muted);
    const mo = rp.list[rp.i], f = mo.f;
    // mini track strip
    const ry = Y + 30, rh = 70;
    ctx.fillStyle = "#1f6b2a"; ctx.fillRect(X + 4, ry, Wd - 8, rh);
    ctx.fillStyle = "#3a3a4a"; ctx.fillRect(X + 4, ry + 14, Wd - 8, rh - 28);
    ctx.fillStyle = C.wl || "#d8d8e0"; ctx.fillRect(X + 4, ry + 14, Wd - 8, 1); ctx.fillRect(X + 4, ry + rh - 15, Wd - 8, 1);
    for (let x = X + 4 + ((-Math.floor(rp.t * 60)) % 16 + 16) % 16; x < X + Wd - 8; x += 16) { ctx.fillStyle = "#8a8a9a"; ctx.fillRect(x, ry + rh / 2, 8, 1); }
    const fx = rp.fx;
    if (!fx.stalled("r")) rp.d += dt / REPLAY_SECS * 1.25;
    const cx = X + 20 + Math.min(1, rp.d) * (Wd - 40), cy = ry + rh / 2;
    fx.setPos("r", cx, cy, 0);
    if (!rp.fired && rp.d >= 0.4) { rp.fired = true; fx.trigger("r", f.type, f.points); this.sfx.play(f.type); }
    fx.update(dt);
    fx.drawUnder(ctx);
    const m = fx.car("r");
    if (!(m && m.hidden)) {
      const body = m && m.flash ? "#ffffff" : m && m.glitch ? TYPE_COLOR.visual : faultColor(f.fault);
      drawCar(ctx, cx + (m && m.jitter || 0) * 2, cy, m ? m.spin : 0, true, body, m && m.glitch ? "#ff00ff" : gameColor(f.game), 2);
    }
    fx.drawOver(ctx);
    // caption
    let y = ry + rh + 12;
    const col = TYPE_COLOR[f.type] || C.text;
    text(ctx, fit(f.lane_id, 24), X + 10, y, C.white, 2); y += 22;
    text(ctx, `${f.game} / ${f.fault}`, X + 10, y, faultColor(f.fault)); y += 12;
    text(ctx, (f.type || "").toUpperCase().replace("_", " "), X + 10, y, col, 3); y += 30;
    for (const line of wrap(f.detail || "(no detail)", 52).slice(0, 2)) { text(ctx, line, X + 10, y, C.text); y += 10; }
    text(ctx, `CLUSTER ${fit(f.cluster || f.stack_hash || "", 44)}`, X + 10, y, C.muted); y += 10;
    text(ctx, `HIT #${f.n || 1}  +${fmtPts(f.points)} PTS  AT ${fmtTime(f.ts)}`, X + 10, y, C.gold); y += 16;
    let tx = X + 10;
    for (const tag of mo.tags) {
      const w = textWidth(tag) + 8;
      ctx.fillStyle = tag === "FIRST BLOOD" ? C.red : tag === "LEAD CHANGE" ? C.gold : TYPE_COLOR.sanitizer;
      ctx.fillRect(tx, y, w, 11);
      text(ctx, tag, tx + 4, y + 2, C.black);
      tx += w + 6;
    }
    // progress pips
    for (let i = 0; i < rp.list.length; i++) {
      ctx.fillStyle = i === rp.i ? C.white : C.line;
      ctx.fillRect(X + Wd / 2 - rp.list.length * 5 + i * 10, Y + Hd - 12, 6, 4);
    }
  }

  // ---------- timing tower ----------

  drawTower(ctx) {
    const x0 = TOWER_X, y0 = TOP + 2;
    ctx.fillStyle = C.panel; ctx.fillRect(x0 - 2, TOP, W - x0 + 2, TICK_Y - TOP);
    ctx.fillStyle = C.line; ctx.fillRect(x0 - 2, TOP, 1, TICK_Y - TOP);
    let tx = x0;
    for (const [id, label] of TABS) {
      const w = textWidth(label) + 10, on = id === this.tab;
      ctx.fillStyle = on ? C.gold : C.panel2; ctx.fillRect(tx, y0, w, 12);
      text(ctx, label, tx + 5, y0 + 2, on ? C.black : C.muted);
      this.hit("tab:" + id, tx, y0, w, 12, () => { this.tab = id; this.auto = false; });
      tx += w + 3;
    }
    if (this.auto) text(ctx, "AUTO", W - 28, y0 + 2, C.gold);
    const y1 = y0 + 16;
    if (this.tab === "heat") this.towerHeat(ctx, x0, y1);
    else if (this.tab === "season") this.towerSeason(ctx, x0, y1);
    else this.towerGroups(ctx, x0, y1, this.tab);
  }

  towerHeat(ctx, x0, y0) {
    const lanes = this.m.ranked();
    const avail = TICK_Y - 4 - y0 - 10;
    let rh = 9, perCol = Math.floor(avail / rh);
    let cols = lanes.length > perCol ? 2 : 1;
    if (lanes.length > perCol * 2) { rh = Math.max(7, Math.floor(avail / Math.ceil(lanes.length / 2))); perCol = Math.floor(avail / rh); }
    const cw = cols === 1 ? TOWER_W : Math.floor(TOWER_W / 2);
    const lead = this.m.leader();
    for (let c = 0; c < cols; c++) {
      const x = x0 + c * cw;
      text(ctx, "POS", x, y0, C.dim);
      text(ctx, "LANE", x + 30, y0, C.dim);
      if (cols === 1) { text(ctx, "GM FAULT", x + 104, y0, C.dim); }
      textR(ctx, "GAP", x + cw - 4, y0, C.dim);
    }
    const shown = Math.min(lanes.length, perCol * cols);
    for (let i = 0; i < shown; i++) {
      const l = lanes[i], c = Math.floor(i / perCol), r = i % perCol;
      const x = x0 + c * cw, y = y0 + 10 + r * rh;
      const fl = this.flash.get(l.lane_id);
      if (fl && this.t - fl.t < 3 && Math.floor((this.t - fl.t) * 6) % 2 === 0) {
        ctx.fillStyle = TYPE_COLOR[fl.type] || C.red; ctx.fillRect(x - 1, y - 1, cw - 2, rh);
      } else if (l.lane_id === this.sel) {
        ctx.fillStyle = C.line; ctx.fillRect(x - 1, y - 1, cw - 2, rh);
      } else if (r % 2) {
        ctx.fillStyle = "#18182f"; ctx.fillRect(x - 1, y - 1, cw - 2, rh);
      }
      const flashing = fl && this.t - fl.t < 3 && Math.floor((this.t - fl.t) * 6) % 2 === 0;
      const scored = l.score > 0;
      const posCol = flashing ? C.black : !scored ? C.dim : l.pos === 1 ? C.gold : l.pos <= 3 ? C.silver : l.pos <= 10 ? C.white : C.muted;
      textR(ctx, scored ? String(l.pos) : "-", x + 17, y, posCol);
      ctx.fillStyle = l.status === "ended" ? "#4a4a5a" : faultColor(l.fault); ctx.fillRect(x + 21, y, 4, 7);
      ctx.fillStyle = gameColor(l.game); ctx.fillRect(x + 25, y, 2, 7);
      const nameCol = flashing ? C.black : l.status === "ended" ? C.dim : l.status === "stale" ? C.amber : C.text;
      text(ctx, fit(l.lane_id, cols === 1 ? 11 : 8), x + 30, y, nameCol);
      if (cols === 1) {
        text(ctx, gameShort(l.game), x + 104, y, flashing ? C.black : gameColor(l.game));
        text(ctx, fit(l.fault, 10), x + 128, y, flashing ? C.black : C.muted);
      }
      let gap = "--", gc = C.dim;
      if (scored && lead) {
        if (l.lane_id === lead.lane_id) { gap = cols === 1 ? "LEADER" : "LEAD"; gc = C.gold; }
        else { gap = "+" + fmtPts(lead.score - l.score); gc = C.text; }
      }
      textR(ctx, gap, x + cw - 4, y, flashing ? C.black : gc);
      this.hit("row:" + l.lane_id, x, y - 1, cw - 2, rh, () => this.select(l.lane_id));
    }
    if (shown < lanes.length) textR(ctx, `+${lanes.length - shown} MORE`, x0 + TOWER_W - 4, TICK_Y - 10, C.amber);
    if (!lanes.length) text(ctx, "NO LANES YET", x0, y0 + 20, C.muted);
  }

  towerGroups(ctx, x0, y0, which) {
    const rows = this.m.standings[which] || [];
    const isFault = which === "constructors";
    text(ctx, "POS", x0, y0, C.dim);
    text(ctx, isFault ? "CONSTRUCTOR" : "GAME", x0 + 40, y0, C.dim);
    textR(ctx, "HEAT", x0 + 178, y0, C.dim);
    textR(ctx, "LN", x0 + 202, y0, C.dim);
    textR(ctx, "SEAS", x0 + TOWER_W - 4, y0, C.dim);
    let y = y0 + 14;
    for (const r of rows) {
      if (y > TICK_Y - 30) break;
      const col = isFault ? faultColor(r.key) : C.white, acc = isFault ? "#fff1e8" : gameColor(r.key);
      textR(ctx, String(r.pos), x0 + 14, y + 2, r.pos === 1 ? C.gold : C.text);
      drawCar(ctx, x0 + 28, y + 5, 0, false, isFault ? col : "#c2c3c7", acc);
      text(ctx, fit(r.key, 18), x0 + 40, y + 2, isFault ? col : gameColor(r.key));
      textR(ctx, fmtPts(r.score), x0 + 178, y + 2, C.text);
      textR(ctx, String(r.lanes), x0 + 202, y + 2, C.muted);
      textR(ctx, String(r.season_points), x0 + TOWER_W - 4, y + 2, C.gold);
      // bar of this heat's score, scaled to the leader
      const max = Math.max(1e-9, ...rows.map(q => q.score));
      ctx.fillStyle = isFault ? col : gameColor(r.key);
      ctx.fillRect(x0 + 40, y + 11, Math.round((r.score / max) * 200), 2);
      y += 18;
    }
    if (!rows.length) text(ctx, "NOTHING YET", x0, y0 + 20, C.muted);
    text(ctx, isFault ? "CONSTRUCTOR = FAULT CONFIG" : "GAMES: SUM OF LANE SCORES", x0, TICK_Y - 22, C.dim);
    text(ctx, "HEAT = THIS HEAT  SEAS = SEASON PTS", x0, TICK_Y - 12, C.dim);
  }

  towerSeason(ctx, x0, y0) {
    const rows = this.m.standings.season || [];
    text(ctx, "POS LANE", x0, y0, C.dim);
    textR(ctx, "PTS", x0 + 140, y0, C.dim);
    textR(ctx, "W", x0 + 162, y0, C.dim);
    textR(ctx, "POD", x0 + 192, y0, C.dim);
    textR(ctx, "HTS", x0 + TOWER_W - 4, y0, C.dim);
    let y = y0 + 12;
    for (const r of rows) {
      if (y > TICK_Y - 28) { textR(ctx, `+${rows.length - rows.indexOf(r)} MORE`, x0 + TOWER_W - 4, TICK_Y - 12, C.amber); break; }
      const l = this.m.lanes.get(r.lane_id) || {};
      const pc = r.pos === 1 ? C.gold : r.pos === 2 ? C.silver : r.pos === 3 ? C.bronze : C.text;
      textR(ctx, String(r.pos), x0 + 17, y, pc);
      ctx.fillStyle = faultColor(l.fault); ctx.fillRect(x0 + 21, y, 4, 7);
      ctx.fillStyle = gameColor(l.game); ctx.fillRect(x0 + 25, y, 2, 7);
      text(ctx, fit(r.lane_id, 14), x0 + 30, y, C.text);
      textR(ctx, String(r.points), x0 + 140, y, C.gold);
      textR(ctx, String(r.wins), x0 + 162, y, C.text);
      textR(ctx, String(r.podiums), x0 + 192, y, C.text);
      textR(ctx, String(r.heats), x0 + TOWER_W - 4, y, C.muted);
      this.hit("season:" + r.lane_id, x0, y - 1, TOWER_W, 9, () => this.select(r.lane_id));
      y += 9;
    }
    if (!rows.length) {
      text(ctx, "NO FINISHED HEATS YET", x0, y0 + 20, C.muted);
      text(ctx, "POINTS " + (this.m.config.season_points || []).join("-"), x0, y0 + 32, C.dim);
    }
  }

  // ---------- top bar and ticker ----------

  drawTopBar(ctx, now) {
    ctx.fillStyle = C.black; ctx.fillRect(0, 0, W, TOP);
    ctx.fillStyle = C.red; ctx.fillRect(0, TOP - 1, W, 1);
    text(ctx, "TORMENT", 4, 2, C.red, 2);
    text(ctx, "NEXUS", 92, 2, C.gold, 2);
    const h = this.m.heat;
    const pit = this.m.phase() === "pit";
    const bx = 160;
    ctx.fillStyle = pit ? C.amber : C.green; ctx.fillRect(bx, 3, 34, 14);
    textC(ctx, pit ? "PIT" : "RACE", bx + 17, 6, C.black);
    textC(ctx, fmtClock(this.m.remaining(now)), bx + 72, 2, pit ? C.amber : C.white, 2);
    text(ctx, h ? h.id : "----", bx + 112, 2, C.text);
    text(ctx, pit ? `NEXT ${h ? h.next_id : ""}` : `${fmtPts(this.m.lapPoints)} PTS/LAP`, bx + 112, 11, C.muted);
    const lead = this.m.leader();
    if (lead) {
      text(ctx, "P1", 342, 2, C.gold);
      text(ctx, fit(lead.lane_id, 12), 360, 2, C.white);
      text(ctx, `${fmtPts(lead.score)} PTS`, 360, 11, C.gold);
    }
    const c = this.m.counts();
    text(ctx, `${c.total} LANES`, 448, 2, C.text);
    text(ctx, `${c.running || 0} RUN ${c.stale || 0} STL ${c.ended || 0} END`, 448, 11, C.muted);
    const f = this.m.focus;
    if (f && this.t - f.at < 30) text(ctx, `WALL> ${fit(f.lane_id, 10)}`, 590, 6, C.sky);
    // connection
    ctx.fillStyle = this.connected ? C.green : (Math.floor(this.t * 2) % 2 ? C.red : "#501020");
    ctx.fillRect(712, 7, 6, 6);
    text(ctx, this.connected ? "LIVE" : "RECONNECTING", 722, 6, this.connected ? C.green : C.red);
    // mute button
    const mx = 860, label = this.sfx.muted ? "SFX OFF" : "SFX ON";
    ctx.fillStyle = this.sfx.muted ? C.panel2 : C.gold; ctx.fillRect(mx, 3, 96, 14);
    speaker(ctx, mx + 4, 6, this.sfx.muted ? C.muted : C.black, this.sfx.muted);
    text(ctx, `[M] ${label}`, mx + 18, 6, this.sfx.muted ? C.muted : C.black);
    this.hit("mute", mx, 3, 96, 14, () => this.sfx.toggle());
  }

  drawTicker(ctx, dt) {
    ctx.fillStyle = C.black; ctx.fillRect(0, TICK_Y, W, H - TICK_Y);
    ctx.fillStyle = C.line; ctx.fillRect(0, TICK_Y, W, 1);
    const backlog = this.marquee.filter(m => m.x > W).length;
    const speed = 60 + backlog * 20;
    for (const m of this.marquee) {
      m.x -= speed * dt;
      if (m.x < W) text(ctx, m.text, m.x, TICK_Y + 4, m.col);
    }
    this.marquee = this.marquee.filter(m => m.x + textWidth(m.text) > 0);
    if (!this.marquee.length) this.say("TORMENT NEXUS - AI AGENTS PLAY GAMES UNDER INJECTED FAULTS - EVERY CRASH SCORES", C.muted);
    ctx.fillStyle = C.red; ctx.fillRect(0, TICK_Y + 1, 34, H - TICK_Y - 1);
    text(ctx, "LIVE", 5, TICK_Y + 4, C.white);
  }

  // ---------- lane panel ----------

  drawPanel(ctx) {
    const { x, y, w, h } = PANEL;
    const lane = (this.card && this.card.lane) || this.m.lanes.get(this.sel) || { lane_id: this.sel };
    const live = this.m.lanes.get(this.sel) || {};
    const fc = faultColor(lane.fault);
    ctx.fillStyle = "rgba(0,0,0,0.5)"; ctx.fillRect(x - 4, y - 2, w + 8, h + 4);
    ctx.fillStyle = C.panel; ctx.fillRect(x, y, w, h);
    ctx.fillStyle = fc; ctx.fillRect(x, y, w, 2); ctx.fillRect(x, y + h - 2, w, 2); ctx.fillRect(x, y, 2, h); ctx.fillRect(x + w - 2, y, 2, h);
    this.hit("panel", x, y, w, h, () => {}); // swallow clicks inside the panel
    // header
    drawCar(ctx, x + 18, y + 13, 0, true, fc, gameColor(lane.game));
    text(ctx, fit(lane.lane_id, 14), x + 34, y + 6, C.white, 2);
    const pos = live.pos || lane.pos;
    const sc = live.score ?? lane.score ?? 0;
    text(ctx, `${pos && sc > 0 ? "P" + pos : "--"}  ${fmtPts(sc)} PTS  LAP ${Math.floor(sc / this.m.lapPoints) + 1}  ${String(live.status || lane.status || "").toUpperCase()}`,
      x + 214, y + 6, C.gold);
    text(ctx, `${lane.hits ?? live.hits ?? 0} HITS  ${lane.clusters ?? live.clusters ?? 0} CLUSTERS  ${live.season_points ?? lane.season_points ?? 0} SEASON PTS`, x + 214, y + 15, C.muted);
    // buttons
    const bx = x + w - 172;
    ctx.fillStyle = C.sky; ctx.fillRect(bx, y + 5, 112, 15);
    text(ctx, "[F] FOCUS ON WALL", bx + 4, y + 9, C.black);
    this.hit("focus", bx, y + 5, 112, 15, () => this.hooks.onFocus && this.hooks.onFocus(this.sel));
    ctx.fillStyle = C.red; ctx.fillRect(x + w - 54, y + 5, 48, 15);
    text(ctx, "[ESC]X", x + w - 50, y + 9, C.white);
    this.hit("close", x + w - 54, y + 5, 48, 15, () => this.select(null));

    // live screen
    const sx = x + 8, sy = y + 28, sw = SCR.maxCols * SCR.cw, sh = SCR.maxRows * SCR.ch;
    ctx.fillStyle = "#000"; ctx.fillRect(sx - 3, sy - 3, sw + 6, sh + 6);
    ctx.fillStyle = C.screenBg; ctx.fillRect(sx, sy, sw, sh);
    const f = this.frame;
    if (this.frameCanvas && f) {
      const fw = this.frameCanvas.width, fh = this.frameCanvas.height;
      ctx.drawImage(this.frameCanvas, 0, 0, fw, fh, sx, sy, fw, fh);
      const [cx, cy] = f.cursor || [-1, -1];
      if (cx >= 0 && cy >= 0 && cx < SCR.maxCols && cy < SCR.maxRows && Math.floor(this.t * 2) % 2 === 0) {
        ctx.fillStyle = C.phosphor; ctx.fillRect(sx + cx * SCR.cw, sy + cy * SCR.ch + SCR.ch - 2, SCR.cw, 2);
      }
      text(ctx, `${f.cols}x${f.rows}  ${fmtTime(f.ts)}`, sx, sy + sh + 5, C.phosphorDim);
    } else {
      // no signal: static
      for (let i = 0; i < 260; i++) {
        ctx.fillStyle = this.fx.rnd() < 0.5 ? "#1a3a22" : "#0d1d11";
        ctx.fillRect(sx + Math.floor(this.fx.rnd() * (sw / 4)) * 4, sy + Math.floor(this.fx.rnd() * (sh / 4)) * 4, 4, 4);
      }
      textC(ctx, "NO SIGNAL", sx + sw / 2, sy + sh / 2 - 8, C.phosphor, 2);
      textC(ctx, "WAITING FOR SCREEN FRAMES", sx + sw / 2, sy + sh / 2 + 14, C.phosphorDim);
    }

    // config card
    const kx = sx + sw + 10, kw = x + w - 8 - kx;
    let ky = sy - 2;
    const kv = [
      ["GAME", `${lane.game || "?"}${lane.game_ver ? " " + lane.game_ver : ""}`, gameColor(lane.game)],
      ["FAULT", lane.fault || "?", fc],
      ["SEED", String(lane.seed ?? "?"), C.text],
      ["MODEL", lane.model || "?", C.text],
      ["HEAT", lane.heat_id || "?", C.text],
      ["TURN", String(lane.turn ?? 0), C.text],
      ["TOKENS", `${kfmt(lane.tokens_in)} / ${kfmt(lane.tokens_out)}`, C.text],
      ["LAST SEEN", fmtTime(lane.last_seen), C.text],
    ];
    const per = Math.floor(kw / CW);
    for (const [k, v, col] of kv) {
      text(ctx, k, kx, ky, C.dim); ky += 9;
      for (const line of wrap(v, per).slice(0, 2)) { text(ctx, line, kx, ky, col); ky += 9; }
      ky += 3;
    }
    const res = (this.card && this.card.results) || [];
    if (res.length) {
      text(ctx, "HEATS", kx, ky, C.dim); ky += 9;
      for (const r of res.slice(-6).reverse()) { text(ctx, fit(`${r.heat_id} P${r.pos} +${r.points}`, per), kx, ky, C.muted); ky += 9; }
    }

    // failure log
    const fl = (this.card && this.card.failures) || [];
    let ly = sy + sh + 16;
    text(ctx, `FAILURE LOG (${fl.length})`, x + 8, ly, C.white);
    ly += 11;
    for (const [lbl, ox] of [["TIME", 14], ["TYPE", 68], ["DETAIL", 104], ["CLUSTER", 278], ["HIT", 446], ["PTS", 488], ["REPLAY", 518], ["HEAT", 566]])
      text(ctx, lbl, x + ox, ly, C.dim);
    ly += 11;
    const rows = Math.floor((y + h - 24 - ly) / 9);
    this.logScroll = Math.min(this.logScroll, Math.max(0, fl.length - rows));
    for (let i = 0; i < rows && i + this.logScroll < fl.length; i++) {
      const idx = i + this.logScroll, e = fl[idx];
      const yy = ly + i * 9;
      if (idx === this.logSel) { ctx.fillStyle = C.line; ctx.fillRect(x + 6, yy - 1, w - 12, 9); }
      const col = TYPE_COLOR[e.type] || C.text;
      ctx.fillStyle = col; ctx.fillRect(x + 8, yy, 3, 7);
      text(ctx, fmtTime(e.ts), x + 14, yy, C.muted);
      text(ctx, (TYPE_SHORT[e.type] || e.type).padEnd(5), x + 68, yy, col);
      text(ctx, fit(e.detail || (e.signal ? "signal " + e.signal : e.exit_code != null ? "exit " + e.exit_code : ""), 28), x + 104, yy, C.text);
      text(ctx, fit(e.cluster || "", 27), x + 278, yy, C.muted);
      text(ctx, e.scored ? `#${e.n}` : "-", x + 446, yy, C.muted);
      textR(ctx, e.scored ? "+" + fmtPts(e.points) : "0", x + 506, yy, e.scored && e.points > 0 ? C.gold : C.dim);
      text(ctx, e.replay ? "BUNDLE" : "-", x + 518, yy, e.replay ? C.sky : C.dim);
      text(ctx, e.heat_id || "", x + 566, yy, C.dim);
      this.hit("log:" + idx, x + 6, yy - 1, w - 12, 9, () => { this.logSel = idx; });
    }
    if (!fl.length) text(ctx, this.card ? "NO FAILURES YET" : "LOADING...", x + 14, ly, C.muted);
    // replay bundle of the selected (or newest) failure
    const pick = fl[this.logSel >= 0 ? this.logSel : 0];
    const by = y + h - 14;
    ctx.fillStyle = C.panel2; ctx.fillRect(x + 6, by - 3, w - 12, 12);
    if (pick) {
      text(ctx, fit(`REPLAY ${pick.replay || "(none)"}  STACK ${pick.stack_hash || "-"}  ${pick.event_id}`, Math.floor((w - 24) / CW)), x + 10, by, pick.replay ? C.sky : C.muted);
    } else {
      text(ctx, "[N]/[P] NEXT/PREV LANE  [WHEEL] SCROLL LOG  CLICK A ROW FOR ITS REPLAY BUNDLE", x + 10, by, C.dim);
    }
  }

  renderScreen(f) {
    const cols = Math.min(SCR.maxCols, f.cols || 80), rows = Math.min(SCR.maxRows, f.rows || 24);
    const cv = newCanvas(cols * SCR.cw, rows * SCR.ch);
    const g = cv.getContext("2d");
    g.fillStyle = C.screenBg; g.fillRect(0, 0, cv.width, cv.height);
    const lines = f.lines || [];
    for (let r = 0; r < rows && r < lines.length; r++) {
      const chars = [...String(lines[r] ?? "")];
      for (let c = 0; c < cols && c < chars.length; c++) termChar(g, chars[c], c * SCR.cw, r * SCR.ch, SCR.cw, SCR.ch, C.phosphor);
    }
    return cv;
  }
}

function speaker(ctx, x, y, col, muted) {
  ctx.fillStyle = col;
  ctx.fillRect(x, y + 2, 2, 4); ctx.fillRect(x + 2, y + 1, 1, 6); ctx.fillRect(x + 3, y, 1, 8);
  if (muted) { ctx.fillRect(x + 6, y + 1, 1, 1); ctx.fillRect(x + 7, y + 2, 1, 1); ctx.fillRect(x + 8, y + 3, 1, 1); ctx.fillRect(x + 9, y + 4, 1, 1); ctx.fillRect(x + 9, y + 1, 1, 1); ctx.fillRect(x + 6, y + 4, 1, 1); }
  else { ctx.fillRect(x + 5, y + 2, 1, 4); ctx.fillRect(x + 7, y + 1, 1, 6); }
}

function kfmt(n) {
  n = n || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n);
}

export function wrap(s, n) {
  s = String(s ?? "");
  if (n <= 0) return [s];
  const out = [];
  for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n));
  return out.length ? out : [""];
}
