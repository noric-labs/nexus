// Failure effects, one per type, coloured by TYPE_COLOR:
//   sanitizer  explodes (particle burst, white flash, screen shake)
//   crash      spins out (two turns, skid marks, sparks)
//   hang       stalls with smoke (car holds position, grey puffs, "!")
//   error_exit sputters (jitter, blue backfire puffs)
//   visual     glitches the sprite (palette swap, slice offsets, flicker)
// Times are in seconds. Positions come from setPos(), called by the renderer.

import { TYPE_COLOR, fmtPts } from "./theme.js";
import { text, textC } from "./font.js";

const DUR = { sanitizer: 1.2, crash: 1.4, hang: 3.0, error_exit: 1.6, visual: 1.6, agent_error: 0.6 };
export const STALL = 2.5; // seconds a hanging car holds before driving on

export class Fx {
  constructor(seed = 1) {
    this.cars = new Map();   // key -> {type, t0, dur}
    this.pos = new Map();    // key -> {x, y, ang}
    this.parts = [];
    this.pops = [];          // floating "+5" labels
    this.t = 0;
    this.shake = 0;
    this.r = seed >>> 0 || 1;
  }

  rnd() { this.r = (Math.imul(this.r, 1664525) + 1013904223) >>> 0; return this.r / 4294967296; }

  setPos(key, x, y, ang) { this.pos.set(key, { x, y, ang }); }

  trigger(key, type, points = 0) {
    const dur = DUR[type] ?? 1;
    this.cars.set(key, { type, t0: this.t, dur, emit: 0 });
    const p = this.pos.get(key);
    if (!p) return;
    const col = TYPE_COLOR[type] || "#fff";
    if (type === "sanitizer") {
      for (let i = 0; i < 46; i++) {
        const a = this.rnd() * Math.PI * 2, v = 20 + this.rnd() * 70;
        this.parts.push({ x: p.x, y: p.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 0.5 + this.rnd() * 0.7,
          age: 0, col: [col, "#ffffff", "#ffec27", "#ff77a8"][i % 4], size: i % 5 === 0 ? 2 : 1, drag: 2.5 });
      }
      this.shake = Math.max(this.shake, 0.45);
    } else if (type === "crash") {
      for (let i = 0; i < 16; i++) {
        const a = p.ang + Math.PI + (this.rnd() - 0.5) * 1.6, v = 30 + this.rnd() * 50;
        this.parts.push({ x: p.x, y: p.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 0.4 + this.rnd() * 0.4,
          age: 0, col: i % 2 ? col : "#ffec27", size: 1, drag: 3 });
      }
    } else if (type === "visual") {
      for (let i = 0; i < 10; i++) {
        this.parts.push({ x: p.x + (this.rnd() - 0.5) * 16, y: p.y + (this.rnd() - 0.5) * 12, vx: 0, vy: 0,
          life: 0.3 + this.rnd() * 0.8, age: 0, col: i % 2 ? col : "#ff00ff", size: 1, w: 2 + Math.floor(this.rnd() * 8), blink: true });
      }
    }
    if (points > 0) this.pops.push({ x: p.x, y: p.y - 10, age: 0, life: 1.6, text: "+" + fmtPts(points), col });
  }

  /** Per-car modifiers for the renderer. */
  car(key) {
    const e = this.cars.get(key);
    if (!e) return null;
    const k = (this.t - e.t0) / e.dur;
    if (k >= 1) { this.cars.delete(key); return null; }
    const out = { type: e.type, k, spin: 0, jitter: 0, glitch: false, flash: false, stalled: false, hidden: false };
    switch (e.type) {
      case "sanitizer": out.flash = k < 0.25 && Math.floor(k * 40) % 2 === 0; out.hidden = k > 0.08 && k < 0.35; break;
      case "crash": { const q = Math.min(1, k / 0.8); out.spin = (1 - (1 - q) * (1 - q)) * Math.PI * 4; break; }
      case "hang": out.stalled = this.t - e.t0 < STALL; break;
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
            life: 1.2, age: 0, col: this.rnd() < 0.5 ? "#c2c3c7" : "#8a8a9a", size: 2, grow: true });
        }
      } else if (e.type === "error_exit" && age < e.dur) {
        e.emit -= dt;
        if (e.emit <= 0) {
          e.emit = 0.18 + this.rnd() * 0.12;
          const bx = p.x - Math.cos(p.ang) * 6, by = p.y - Math.sin(p.ang) * 6;
          this.parts.push({ x: bx, y: by, vx: -Math.cos(p.ang) * 14, vy: -Math.sin(p.ang) * 14 - 4, life: 0.5, age: 0,
            col: this.rnd() < 0.3 ? "#ffec27" : TYPE_COLOR.error_exit, size: 2, grow: true });
        }
      } else if (e.type === "crash" && age < 0.9) {
        e.emit -= dt;
        if (e.emit <= 0) {
          e.emit = 0.05;
          this.parts.push({ x: p.x, y: p.y, vx: 0, vy: 0, life: 2.5, age: 0, col: "#1a1a22", size: 1, under: true });
        }
      }
    }
    for (const q of this.parts) {
      q.age += dt;
      if (q.drag) { const f = Math.exp(-q.drag * dt); q.vx *= f; q.vy *= f; }
      q.x += q.vx * dt; q.y += q.vy * dt;
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
      ctx.fillRect(Math.round(q.x - s / 2), Math.round(q.y - s / 2), q.w || s, s);
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
