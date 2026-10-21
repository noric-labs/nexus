// Generative drum & bass, synthesised with WebAudio; no audio files. Shares
// the Sfx AudioContext and master gain, so it only starts after the viewer
// unmutes (a user gesture). A lookahead scheduler queues 16th notes ~120 ms
// ahead at 172 BPM. Each heat gets its own song, seeded by the heat id: key,
// chord loop, drum variant and acid riff. Intensity follows the race:
//   pit    pad, hats and a filtered sub (outro / waiting)
//   race   full mix; the acid line and pad swap every 16 bars
//   final  last minute: acid filter opens, extra percussion
// Going from pit to race plays a two-bar riser into the drop. Stingers:
// hit("death") drops the drums for a bar under a sub boom and a reverse swell;
// hit("lead") plays an acid fill.

import { hash } from "./theme.js";

const BPM = 172, STEP = 60 / BPM / 4, AHEAD = 0.12;
const SCALE = [0, 2, 3, 5, 7, 8, 10]; // natural minor
const PROGS = [[0, 5, 2, 6], [0, 3, 5, 4], [0, 6, 5, 6], [0, 2, 5, 4], [5, 6, 0, 0]];
const DRUMS = [
  { k: [0, 10], s: [4, 12], g: [7, 15] },
  { k: [0, 6, 10], s: [4, 12], g: [9, 14] },
  { k: [0, 10, 11], s: [4, 12], g: [2, 7] },
];
const BASS = [[0, 6], [6, 4], [10, 6]]; // [step, length] following the kick
const mtof = m => 440 * Math.pow(2, (m - 69) / 12);

export class Music {
  constructor(sfx) {
    this.sfx = sfx;
    this.on = true;          // the viewer's music preference; sound as a whole is sfx.muted
    this.timer = null;
    this.bus = null;
    this.step = 0;
    this.next = 0;
    this.phase = "pit";
    this.songId = null;
    this.dropBar = -1;       // bar with the drums dropped (death)
    this.buildTo = -1;       // bar the riser lands on
    this.fillBar = -1;       // bar with an acid fill (lead change)
    this.notes = 0;          // voices scheduled, for tests
    this.setSong("");
  }

  get playing() { return !!this.timer; }

  start() {
    const c = this.sfx.ctx;
    if (this.timer || !c) return;
    this.bus = c.createGain();
    this.bus.gain.setValueAtTime(0.9, c.currentTime);
    this.bus.connect(this.sfx.master);
    this.next = c.currentTime + 0.05;
    this.step = 0;
    this.timer = setInterval(() => this.tick(), 25);
  }

  stop() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
    const c = this.sfx.ctx, bus = this.bus;
    bus.gain.setValueAtTime(bus.gain.value || 0.9, c.currentTime);
    bus.gain.linearRampToValueAtTime(0, c.currentTime + 0.1);
    setTimeout(() => bus.disconnect(), 300);
    this.bus = null;
  }

  /** Called every frame with the current heat and phase ("pit", "race" or "final"). */
  follow(heatId, phase) {
    if (heatId && heatId !== this.songId) this.setSong(heatId);
    if (phase === this.phase) return;
    if (this.phase === "pit" && phase !== "pit") this.buildTo = this.bar() + 2;
    this.phase = phase;
  }

  setSong(id) {
    this.songId = id;
    let r = hash(String(id)) || 1;
    const rnd = () => ((r = (Math.imul(r, 1664525) + 1013904223) >>> 0) / 4294967296);
    this.root = 33 + Math.floor(rnd() * 8);                  // A1..E2
    this.prog = PROGS[Math.floor(rnd() * PROGS.length)];
    this.drums = DRUMS[Math.floor(rnd() * DRUMS.length)];
    this.riff = Array.from({ length: 16 }, () => rnd() < 0.55
      ? { d: Math.floor(rnd() * 8), oct: rnd() < 0.25 ? 12 : 0, acc: rnd() < 0.3 } : null);
  }

  hit(kind) {
    if (!this.timer) return;
    const c = this.sfx.ctx, now = c.currentTime;
    if (kind === "death") {
      this.dropBar = this.bar() + 1;
      this.boom(now);
      const end = this.next + (16 - (this.step % 16)) * STEP + 16 * STEP; // end of the dropped bar
      this.swell(end - 0.7, 0.7);
    } else if (kind === "lead") {
      this.fillBar = this.bar() + 1;
    }
  }

  bar() { return Math.floor(this.step / 16); }

  tick() {
    const c = this.sfx.ctx;
    if (!c) return;
    if (this.next < c.currentTime) this.next = c.currentTime + 0.02; // tab was asleep: skip, don't burst
    while (this.next < c.currentTime + AHEAD) {
      this.schedule(this.step, this.next);
      this.next += STEP;
      this.step++;
    }
  }

  degree(d, base) {
    const o = Math.floor(d / 7), i = ((d % 7) + 7) % 7;
    return base + o * 12 + SCALE[i];
  }

  schedule(step, t) {
    const s = step % 16, bar = Math.floor(step / 16), section = Math.floor(bar / 16);
    const chord = this.prog[Math.floor(bar / 2) % 4];
    const pit = this.phase === "pit", final = this.phase === "final";
    const building = bar < this.buildTo;
    const drums = !pit && !building && bar !== this.dropBar;
    const D = this.drums;
    // riser into the drop
    if (building && bar === this.buildTo - 2 && s === 0) this.riser(t, 32 * STEP);
    if (building && bar === this.buildTo - 1 && s % (s < 8 ? 2 : 1) === 0) this.snare(t, 0.2 + s * 0.03);
    // drums
    if (drums) {
      if (D.k.includes(s)) this.kick(t, 1);
      if (D.s.includes(s)) this.snare(t, 0.7);
      if (D.g.includes(s)) this.snare(t, 0.15);
      if (final && s % 4 === 2) this.hat(t, 0.25, true);
    }
    if (!building && bar !== this.dropBar && (final ? true : s % 2 === 0)) this.hat(t, s % 4 === 2 ? 0.22 : 0.12, false);
    // sub
    for (const [at, len] of BASS) if (s === at && (drums || pit)) this.sub(t, mtof(this.degree(chord, this.root)), len * STEP, pit ? 140 : 260);
    // acid: odd sections, always in the final minute, and fills
    const fill = bar === this.fillBar;
    if (!pit && drums && (section % 2 === 1 || final || fill)) {
      const n = fill ? { d: s % 8, oct: s >= 8 ? 12 : 0, acc: s % 4 === 0 } : this.riff[s];
      if (n) this.acid(t, mtof(this.degree(chord + n.d, this.root + 24) + n.oct), STEP * 0.9, n.acc, final ? 1400 : 700);
    }
    // pad on chord changes: always in the pit, even sections in the race
    if (s === 0 && bar % 2 === 0 && (pit || section % 2 === 0 || building)) {
      const notes = [0, 2, 4].map(k => mtof(this.degree(chord + k, this.root + 36)));
      this.pad(t, notes, 32 * STEP);
    }
  }

  // ---------- voices ----------

  env(t, peak, attack, decay) {
    const g = this.sfx.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(peak, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    g.connect(this.bus);
    return g;
  }

  osc(type, f, t, dur, out) {
    const o = this.sfx.ctx.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(f, t);
    o.connect(out);
    o.start(t); o.stop(t + dur + 0.05);
    this.notes++;
    return o;
  }

  noise(t, dur, out) {
    const n = this.sfx.ctx.createBufferSource();
    n.buffer = this.sfx.noiseBuf;
    n.loop = true; // the shared buffer is 1 s; risers run longer
    n.connect(out);
    n.start(t); n.stop(t + dur + 0.05);
    this.notes++;
    return n;
  }

  filter(type, f, q, out) {
    const b = this.sfx.ctx.createBiquadFilter();
    b.type = type;
    b.frequency.value = f;
    b.Q.value = q;
    b.connect(out);
    return b;
  }

  kick(t, v) {
    const o = this.osc("sine", 150, t, 0.3, this.env(t, v, 0.002, 0.28));
    o.frequency.exponentialRampToValueAtTime(45, t + 0.12);
  }

  snare(t, v) {
    this.noise(t, 0.18, this.filter("highpass", 1800, 0.7, this.env(t, v * 0.6, 0.001, 0.17)));
    const o = this.osc("triangle", 180, t, 0.1, this.env(t, v * 0.5, 0.001, 0.09));
    o.frequency.exponentialRampToValueAtTime(140, t + 0.08);
  }

  hat(t, v, open) {
    const d = open ? 0.12 : 0.03;
    this.noise(t, d, this.filter("highpass", 7000, 0.7, this.env(t, v, 0.001, d)));
  }

  // Reese: two detuned saws and a sine, low-passed
  sub(t, f, dur, cut) {
    const g = this.sfx.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.32, t + 0.01);
    g.gain.setValueAtTime(0.32, t + dur - 0.04);
    g.gain.linearRampToValueAtTime(0.0001, t + dur);
    g.connect(this.bus);
    const lp = this.filter("lowpass", cut, 2, g);
    this.osc("sawtooth", f, t, dur, lp);
    this.osc("sawtooth", f * 1.007, t, dur, lp);
    this.osc("sine", f, t, dur, g);
  }

  acid(t, f, dur, acc, cut) {
    const lp = this.filter("lowpass", cut, 12, this.env(t, acc ? 0.16 : 0.1, 0.003, dur));
    lp.frequency.setValueAtTime(cut * (acc ? 4 : 2.5), t);
    lp.frequency.exponentialRampToValueAtTime(cut, t + 0.12);
    this.osc("sawtooth", f, t, dur, lp);
  }

  pad(t, notes, dur) {
    const g = this.sfx.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.05, t + 0.6);
    g.gain.setValueAtTime(0.05, t + dur - 0.8);
    g.gain.linearRampToValueAtTime(0.0001, t + dur);
    g.connect(this.bus);
    const lp = this.filter("lowpass", 900, 0.5, g);
    notes.forEach((f, i) => this.osc("sawtooth", f * (1 + (i - 1) * 0.004), t, dur, lp));
  }

  boom(t) {
    const o = this.osc("sine", 60, t, 1.2, this.env(t, 0.9, 0.005, 1.1));
    o.frequency.exponentialRampToValueAtTime(30, t + 1.1);
    this.noise(t, 0.6, this.filter("lowpass", 300, 1, this.env(t, 0.6, 0.002, 0.55)));
  }

  // reverse noise: rises in level and brightness, cut dead at the end
  swell(t, dur) {
    const g = this.sfx.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.35, t + dur);
    g.gain.setValueAtTime(0.0001, t + dur + 0.01);
    g.connect(this.bus);
    const hp = this.filter("highpass", 300, 1, g);
    hp.frequency.setValueAtTime(300, t);
    hp.frequency.exponentialRampToValueAtTime(5000, t + dur);
    this.noise(t, dur, hp);
  }

  riser(t, dur) {
    const g = this.sfx.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.3, t + dur);
    g.gain.setValueAtTime(0.0001, t + dur + 0.01);
    g.connect(this.bus);
    const bp = this.filter("bandpass", 400, 4, g);
    bp.frequency.setValueAtTime(400, t);
    bp.frequency.exponentialRampToValueAtTime(6000, t + dur);
    this.noise(t, dur, bp);
  }
}
