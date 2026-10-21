// Failure effects, one per type, coloured by TYPE_COLOR:
//   sanitizer  vaporised: white flash, red mist ring, hull shards, the pilot
//              is flung out and bursts almost at once (death)
//   crash      destroyed: hull shards, the pilot is thrown forward, tumbles
//              and bursts into blood and gibs (death)
//   hang       powers down: holds position, sparks and dark smoke, "!"
//   error_exit ejects: jitter, cyan backfire, the pilot bails out (no gore)
//   visual     hologram glitch (palette swap, slice offsets, flicker)
// Deaths leave blood decals on the deck until the next green flag.
// Times are in seconds. Positions come from setPos(), called by the renderer.

import { TYPE_COLOR, C, fmtPts } from "./theme.js";
import { text, textC } from "./font.js";

const DUR = { sanitizer: 1.9, crash: 1.6, hang: 3.0, error_exit: 1.6, visual: 1.6, agent_error: 0.6 };
const DEATH = new Set(["sanitizer", "crash"]);
const BLOOD = [C.blood1, C.blood2, C.blood2, C.blood3];
const MAX_DECALS = 600;
export const STALL = 2.5; // seconds a hanging car holds before driving on
export const RESPAWN = 0.5; // seconds of blinking before a destroyed ship is back

export class Fx {
  constructor(seed = 1) {
    this.cars = new Map();   // key -> {type, t0, dur}
    this.pos = new Map();    // key -> {x, y, ang}
    this.parts = [];
    this.pilots = [];        // ejected pilots in flight
    this.decals = [];        // blood and scorch left on the deck: {x, y, s, col}
    this.pops = [];          // floating "+5" labels
    this.t = 0;
    this.shake = 0;
    this.flashT = -9;        // last full-screen flash (sanitizer)
    this.lastDeath = null;   // {key, x, y, t, type}
    this.r = seed >>> 0 || 1;
  }

  rnd() { this.r = (Math.imul(this.r, 1664525) + 1013904223) >>> 0; return this.r / 4294967296; }

  setPos(key, x, y, ang) { this.pos.set(key, { x, y, ang }); }

  /** livery: {body, acc} of the ship, for shards and the pilot's suit. */
  trigger(key, type, points = 0, livery = null) {
    const dur = DUR[type] ?? 1;
    this.cars.set(key, { type, t0: this.t, dur, emit: 0, death: DEATH.has(type) });
    const p = this.pos.get(key);
    if (!p) return;
    const col = TYPE_COLOR[type] || "#fff";
    if (DEATH.has(type)) this.death(key, type, p, livery);
    else if (type === "error_exit") {
      this.pilots.push({ x: p.x, y: p.y, vx: Math.cos(p.ang + Math.PI / 2) * 40, vy: Math.sin(p.ang + Math.PI / 2) * 40,
        age: 0, burst: 1.1, gore: false, suit: livery ? livery.body : col });
    } else if (type === "visual") {
      for (let i = 0; i < 10; i++) {
        this.parts.push({ x: p.x + (this.rnd() - 0.5) * 16, y: p.y + (this.rnd() - 0.5) * 12, vx: 0, vy: 0,
          life: 0.3 + this.rnd() * 0.8, age: 0, col: i % 2 ? col : C.magenta, size: 1, w: 2 + Math.floor(this.rnd() * 8), blink: true });
      }
    }
    if (points > 0) this.pops.push({ x: p.x, y: p.y - 10, age: 0, life: 1.6, text: "+" + fmtPts(points), col });
  }

  death(key, type, p, livery) {
    const big = type === "sanitizer";
    const body = livery ? livery.body : TYPE_COLOR[type], acc = livery ? livery.acc : C.white;
    const ca = Math.cos(p.ang), sa = Math.sin(p.ang);
    // hull shards keep some of the ship's momentum
    for (let i = 0, n = big ? 14 : 8; i < n; i++) {
      const a = this.rnd() * Math.PI * 2, v = 30 + this.rnd() * 60;
      this.parts.push({ x: p.x, y: p.y, vx: ca * 40 + Math.cos(a) * v, vy: sa * 40 + Math.sin(a) * v, life: 0.7 + this.rnd() * 0.6,
        age: 0, col: [body, acc, C.white, "#2a3050"][i % 4], size: 2, w: 1 + Math.floor(this.rnd() * 3), drag: 2.2 });
    }
    for (let i = 0; i < 10; i++) {
      const a = this.rnd() * Math.PI * 2, v = 40 + this.rnd() * 80;
      this.parts.push({ x: p.x, y: p.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 0.2 + this.rnd() * 0.3,
        age: 0, col: i % 2 ? C.yellow : C.white, size: 1, drag: 3 });
    }
    for (let i = 0; i < 5; i++) this.decal(p.x + (this.rnd() - 0.5) * 6, p.y + (this.rnd() - 0.5) * 6, 2, "#1c0a14");
    // the pilot is flung forward and bursts
    const side = (this.rnd() - 0.5) * 40, v = 70 + this.rnd() * 30;
    this.pilots.push({ x: p.x, y: p.y, vx: ca * v - sa * side, vy: sa * v + ca * side, age: 0,
      burst: big ? 0.15 : 0.4, gore: true, big, suit: body, drip: 0 });
    if (big) {
      for (let i = 0; i < 28; i++) {
        const a = (i / 28) * Math.PI * 2;
        this.parts.push({ x: p.x, y: p.y, vx: Math.cos(a) * 90, vy: Math.sin(a) * 90, life: 0.5, age: 0,
          col: i % 2 ? C.blood3 : C.magenta, size: 1, drag: 4, blink: true });
      }
      this.flashT = this.t;
    }
    this.shake = Math.max(this.shake, big ? 0.5 : 0.25);
    this.lastDeath = { key, x: p.x, y: p.y, t: this.t, type };
  }

  splat(x, y, vx, vy, big) {
    const heading = Math.atan2(vy, vx);
    for (let i = 0, n = big ? 100 + Math.floor(this.rnd() * 30) : 40 + Math.floor(this.rnd() * 40); i < n; i++) {
      const a = this.rnd() < 0.3 ? this.rnd() * Math.PI * 2 : heading + (this.rnd() - 0.5) * 2.4;
      const v = 20 + this.rnd() * (big ? 140 : 100);
      this.parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 0.4 + this.rnd() * 0.5, age: 0,
        col: BLOOD[Math.floor(this.rnd() * BLOOD.length)], size: this.rnd() < 0.2 ? 2 : 1, drag: 3.5, stain: true });
    }
    for (let i = 0, n = 6 + Math.floor(this.rnd() * 5); i < n; i++) {
      const a = this.rnd() * Math.PI * 2, v = 30 + this.rnd() * 50;
      this.parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, z: 0, vz: 60 + this.rnd() * 60, bounce: 0,
        life: 2, age: 0, col: i % 3 ? C.blood2 : "#e8b8a8", size: 2, drag: 1.5, stain: true });
    }
    for (let i = 0; i < 8; i++) this.decal(x + (this.rnd() - 0.5) * 6, y + (this.rnd() - 0.5) * 6, 2, C.blood1);
  }

  decal(x, y, s, col) {
    this.decals.push({ x: Math.round(x), y: Math.round(y), s, col });
    if (this.decals.length > MAX_DECALS) this.decals.splice(0, this.decals.length - MAX_DECALS);
  }

  clearDecals() { this.decals = []; }

  /** Per-car modifiers for the renderer. */
  car(key) {
    const e = this.cars.get(key);
    if (!e) return null;
    const age = this.t - e.t0, k = age / e.dur;
    if (k >= 1) { this.cars.delete(key); return null; }
    const out = { type: e.type, k, spin: 0, jitter: 0, glitch: false, flash: false, stalled: false, hidden: false, dead: false };
    if (e.death) {
      out.flash = age < 0.06;
      out.hidden = age >= 0.06 && (age < e.dur - RESPAWN || Math.floor(age * 12) % 2 === 0);
      return out;
    }
    switch (e.type) {
      case "hang": out.stalled = age < STALL; out.dead = out.stalled; break;
      case "error_exit": out.jitter = Math.floor(k * 30) % 3 === 0 ? (Math.floor(k * 30) % 2 ? 1 : -1) : 0; break;
      case "visual": out.glitch = Math.floor(k * 24) % 4 !== 3; break;
    }
    return out;
  }

  /** True while a hanging car must not advance. */
  stalled(key) {
    const e = this.cars.get(key);
    return !!e && e.type === "hang" && this.t - e.t0 < STALL;
  }

  update(dt) {
    this.t += dt;
    this.shake = Math.max(0, this.shake - dt);
    // continuous emitters
    for (const [key, e] of this.cars) {
      const p = this.pos.get(key);
      if (!p) continue;
      const age = this.t - e.t0;
      if (e.type === "hang" && age < e.dur) {
        e.emit -= dt;
        if (e.emit <= 0) {
          e.emit = 0.12;
          this.parts.push({ x: p.x + (this.rnd() - 0.5) * 4, y: p.y - 2, vx: (this.rnd() - 0.5) * 6, vy: -10 - this.rnd() * 8,
            life: 1.2, age: 0, col: this.rnd() < 0.5 ? "#2a3050" : "#3a4470", size: 2, grow: true });
          if (this.rnd() < 0.4) {
            const a = this.rnd() * Math.PI * 2;
            this.parts.push({ x: p.x, y: p.y, vx: Math.cos(a) * 40, vy: Math.sin(a) * 40, life: 0.25, age: 0,
              col: this.rnd() < 0.5 ? C.yellow : C.cyan, size: 1, drag: 4 });
          }
        }
      } else if (e.type === "error_exit" && age < e.dur) {
        e.emit -= dt;
        if (e.emit <= 0) {
          e.emit = 0.18 + this.rnd() * 0.12;
          const bx = p.x - Math.cos(p.ang) * 6, by = p.y - Math.sin(p.ang) * 6;
          this.parts.push({ x: bx, y: by, vx: -Math.cos(p.ang) * 14, vy: -Math.sin(p.ang) * 14 - 4, life: 0.5, age: 0,
            col: this.rnd() < 0.3 ? C.white : TYPE_COLOR.error_exit, size: 2, grow: true });
        }
      }
    }
    for (const q of this.pilots) {
      q.age += dt;
      const f = Math.exp(-1.2 * dt);
      q.vx *= f; q.vy *= f;
      q.x += q.vx * dt; q.y += q.vy * dt;
      if (q.gore && (q.drip -= dt) <= 0) { q.drip = 0.05; this.decal(q.x, q.y, 1, C.blood1); }
      if (q.gore && q.age >= q.burst) this.splat(q.x, q.y, q.vx, q.vy, q.big);
    }
    this.pilots = this.pilots.filter(q => q.age < q.burst);
    for (const q of this.parts) {
      q.age += dt;
      if (q.drag) { const f = Math.exp(-q.drag * dt); q.vx *= f; q.vy *= f; }
      q.x += q.vx * dt; q.y += q.vy * dt;
      if (q.z !== undefined) {
        q.vz -= 260 * dt; q.z += q.vz * dt;
        if (q.z < 0) {
          q.z = 0;
          if (q.bounce++ < 1) { q.vz = -q.vz * 0.35; this.decal(q.x, q.y, 2, C.blood1); }
          else q.age = q.life;
        }
      }
      if (q.stain && q.age >= q.life) this.decal(q.x, q.y, q.size, q.col === C.blood3 ? C.blood2 : q.col);
    }
    this.parts = this.parts.filter(q => q.age < q.life);
    if (this.parts.length > 1500) this.parts.splice(0, this.parts.length - 1500);
    for (const q of this.pops) q.age += dt;
    this.pops = this.pops.filter(q => q.age < q.life);
  }

  shakeOffset() {
    if (this.shake <= 0) return [0, 0];
    const m = Math.ceil(this.shake * 6);
    return [Math.round((this.rnd() - 0.5) * m), Math.round((this.rnd() - 0.5) * m)];
  }

  drawUnder(ctx) {
    for (const d of this.decals) {
      ctx.fillStyle = d.col;
      ctx.fillRect(d.x, d.y, d.s, d.s);
    }
    for (const q of this.parts) if (q.under) {
      ctx.fillStyle = q.col;
      ctx.fillRect(Math.round(q.x), Math.round(q.y), 1, 1);
    }
  }

  drawOver(ctx) {
    for (const q of this.parts) {
      if (q.under) continue;
      if (q.blink && Math.floor(this.t * 20 + q.x) % 2) continue;
      const s = q.grow ? Math.max(1, Math.round(q.size + (q.age / q.life) * 3)) : q.size;
      ctx.fillStyle = q.col;
      ctx.fillRect(Math.round(q.x - s / 2), Math.round(q.y - s / 2 - (q.z || 0)), q.w || s, s);
    }
    // tumbling pilots: a 2x3 figure, upright or on its side
    for (const q of this.pilots) {
      if (!q.gore && q.age > q.burst * 0.6 && Math.floor(this.t * 16) % 2) continue;
      const x = Math.round(q.x), y = Math.round(q.y), side = Math.floor(q.age * 16) % 2;
      ctx.fillStyle = q.suit;
      if (side) ctx.fillRect(x - 1, y, 3, 2); else ctx.fillRect(x, y - 1, 2, 3);
      ctx.fillStyle = "#ffd0b0";
      ctx.fillRect(side ? x + 1 : x, side ? y : y - 1, 1, 1);
    }
    // stalled cars get a blinking "!" overhead
    for (const [key, e] of this.cars) {
      if (e.type !== "hang") continue;
      const p = this.pos.get(key);
      if (p && Math.floor(this.t * 4) % 2 === 0) text(ctx, "!", p.x - 2, p.y - 16, TYPE_COLOR.hang);
    }
    for (const q of this.pops) {
      const y = q.y - (q.age / q.life) * 14;
      if (q.age > q.life * 0.7 && Math.floor(this.t * 16) % 2) continue;
      textC(ctx, q.text, q.x, y, q.col);
    }
  }
}
