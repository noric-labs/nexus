// Chiptune SFX synthesised with WebAudio (square/triangle oscillators and a
// noise buffer). Muted by default; the AudioContext is only created when the
// viewer unmutes (a user gesture), so autoplay policies are respected.

export class Sfx {
  constructor() {
    this.muted = true;
    this.ctx = null;
    this.last = new Map(); // name -> last play time (s), for rate limiting
  }

  toggle() {
    this.muted = !this.muted;
    if (!this.muted) {
      if (!this.ctx) {
        const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
        if (!AC) { this.muted = true; return this.muted; }
        this.ctx = new AC();
        this.master = this.ctx.createGain();
        this.master.gain.value = 0.18;
        this.master.connect(this.ctx.destination);
        const len = Math.floor(this.ctx.sampleRate * 1.0);
        this.noiseBuf = this.ctx.createBuffer(1, len, this.ctx.sampleRate);
        const d = this.noiseBuf.getChannelData(0);
        let r = 1;
        for (let i = 0; i < len; i++) { r = (r * 16807) % 2147483647; d[i] = (r / 2147483647) * 2 - 1; }
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      this.play("blip");
    }
    return this.muted;
  }

  tone(freq, dur, { type = "square", at = 0, vol = 0.5, to = null } = {}) {
    const c = this.ctx, t = c.currentTime + at;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    if (to) o.frequency.exponentialRampToValueAtTime(Math.max(20, to), t + dur);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    o.connect(g); g.connect(this.master);
    o.start(t); o.stop(t + dur + 0.02);
  }

  noise(dur, { at = 0, vol = 0.5, freq = 2000, q = 1 } = {}) {
    const c = this.ctx, t = c.currentTime + at;
    const s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
    s.buffer = this.noiseBuf;
    f.type = "lowpass"; f.frequency.setValueAtTime(freq, t); f.Q.value = q;
    f.frequency.exponentialRampToValueAtTime(80, t + dur);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    s.connect(f); f.connect(g); g.connect(this.master);
    s.start(t); s.stop(t + dur + 0.02);
  }

  /** Play a named effect; repeated names are rate limited. */
  play(name) {
    if (this.muted || !this.ctx) return false;
    const now = this.ctx.currentTime;
    const gap = { splat: 0.12, crunch: 0.12, sanitizer: 0.25, crash: 0.2, hang: 0.4, error_exit: 0.2, visual: 0.2, agent_error: 0.5 }[name] ?? 0.1;
    if (now - (this.last.get(name) ?? -9) < gap) return false;
    this.last.set(name, now);
    const fn = SOUNDS[name];
    if (!fn) return false;
    fn(this);
    return true;
  }
}

const SOUNDS = {
  blip: s => s.tone(880, 0.08, { vol: 0.3 }),
  // big boom plus a falling square wave
  sanitizer: s => { s.noise(0.7, { vol: 0.9, freq: 3000 }); s.tone(440, 0.5, { to: 55, vol: 0.4 }); s.tone(1760, 0.12, { at: 0.02, vol: 0.2 }); },
  // short smash then a squealing slide (spin out)
  crash: s => { s.noise(0.25, { vol: 0.7, freq: 5000 }); s.tone(1200, 0.5, { type: "sawtooth", to: 300, vol: 0.15, at: 0.05 }); },
  // engine dies: a low buzz sliding down
  hang: s => { s.tone(110, 0.9, { type: "sawtooth", to: 30, vol: 0.3 }); s.noise(0.9, { vol: 0.15, freq: 400 }); },
  // sputter: three coughs
  error_exit: s => { for (let i = 0; i < 3; i++) { s.noise(0.06, { at: i * 0.12, vol: 0.5, freq: 900 }); s.tone(90, 0.06, { at: i * 0.12, vol: 0.3 }); } },
  // glitch: fast random arpeggio
  visual: s => { const f = [523, 1568, 784, 2093, 659, 1319]; f.forEach((x, i) => s.tone(x, 0.04, { at: i * 0.035, vol: 0.2 })); },
  agent_error: s => s.tone(220, 0.08, { type: "triangle", vol: 0.2 }),
  // pilot burst: a wet, low-passed slap with a short tail
  splat: s => { s.noise(0.18, { vol: 0.9, freq: 900, q: 4 }); s.noise(0.35, { at: 0.04, vol: 0.4, freq: 350 }); s.tone(70, 0.12, { type: "sine", to: 40, vol: 0.5 }); },
  // hull breaking up
  crunch: s => { for (let i = 0; i < 4; i++) s.noise(0.05, { at: i * 0.03, vol: 0.6, freq: 2500 - i * 400, q: 3 }); },
  lead: s => { [523, 659, 784, 1047].forEach((f, i) => s.tone(f, 0.12, { at: i * 0.09, vol: 0.3 })); },
  newcluster: s => { [784, 1047, 1568].forEach((f, i) => s.tone(f, 0.08, { at: i * 0.06, vol: 0.25, type: "triangle" })); },
  light: s => s.tone(440, 0.18, { vol: 0.35 }),
  go: s => s.tone(880, 0.6, { vol: 0.4 }),
  flag: s => { [784, 659, 784, 659, 784, 1047].forEach((f, i) => s.tone(f, 0.14, { at: i * 0.15, vol: 0.3 })); },
};
