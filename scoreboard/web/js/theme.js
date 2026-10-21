// Colours and naming for the race UI. Neon anti-grav palette: void black,
// dark deck blues, cyan/magenta/yellow signal colours and a blood ramp.

export const W = 960, H = 540; // logical resolution; scaled by an integer to the display

export const C = {
  bg: "#04050a", panel: "#0a0d1a", panel2: "#141a30", line: "#1f2a50", dim: "#4a5680",
  text: "#dfe8ff", muted: "#8a96c0", gold: "#f5e800", silver: "#c8d6f0", bronze: "#ff8a3d",
  red: "#ff1f3a", green: "#3dff8a", amber: "#ffb000", sky: "#00f0ff", white: "#eef6ff",
  black: "#000000", phosphor: "#7dff9a", phosphorDim: "#2a5a35", screenBg: "#061108",
  void: "#04050a", deck: "#0d1020", deck2: "#141a30", grid: "#1b2240",
  cyan: "#00f0ff", magenta: "#ff2bd6", yellow: "#f5e800",
  blood1: "#7a0010", blood2: "#b3001b", blood3: "#ff1f3a",
};

// Failure types: one colour each, used by effects, tower flashes, log rows.
export const TYPE_COLOR = {
  sanitizer: "#ff2bd6", crash: "#ff1f3a", hang: "#f5e800", error_exit: "#00f0ff",
  visual: "#3dff8a", agent_error: "#8a96c0",
};
export const TYPE_SHORT = {
  sanitizer: "SANI", crash: "CRSH", hang: "HANG", error_exit: "EXIT", visual: "VIS", agent_error: "AGNT",
};
export const TYPES = ["sanitizer", "crash", "hang", "error_exit", "visual", "agent_error"];

// Constructor (fault config) liveries: hull colours.
const LIVERY = ["#ff2bd6", "#00f0ff", "#f5e800", "#ff6a00", "#3dff8a", "#8b5cff", "#ff1f3a",
  "#eef6ff", "#00a3ff", "#ff8ad8", "#b6ff00", "#ffb000", "#00ffc8", "#c04bff"];
const KNOWN_FAULTS = ["none", "malloc-1pct", "malloc-0.1pct", "io-save-5pct", "strace-enospc-save",
  "strace-eintr-read", "winch-storm", "rlimit-tight", "time-warp", "keystorm"];

// Game accents (stripe on the car).
const GAME_ACCENT = { brogue: "#f5e800", "frotz-zork1": "#00f0ff", greed: "#3dff8a", dcss: "#ff8ad8" };
const ACCENTS = ["#eef6ff", "#f5e800", "#00f0ff", "#3dff8a", "#ff8ad8", "#ffb000", "#8b5cff"];

export function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

export function faultColor(f) {
  f = f || "?";
  const i = KNOWN_FAULTS.indexOf(f);
  return LIVERY[(i >= 0 ? i : hash(f)) % LIVERY.length];
}

export function gameColor(g) {
  g = g || "?";
  return GAME_ACCENT[g] || ACCENTS[hash(g) % ACCENTS.length];
}

/** A 7x7 constructor pictogram: rows of 7-bit masks, mirrored left/right, seeded by the name. */
export function pictogram(key) {
  let h = hash(String(key || "?")) || 1;
  const rows = [];
  for (let y = 0; y < 7; y++) {
    let r = 0;
    for (let x = 0; x < 4; x++) {
      h = (Math.imul(h, 1664525) + 1013904223) >>> 0;
      if (h >>> 31 || (y === 3 && x === 3)) r |= (1 << (6 - x)) | (1 << x);
    }
    rows.push(r);
  }
  return rows;
}

export function gameShort(g) {
  return ({ brogue: "BRG", "frotz-zork1": "ZRK", greed: "GRD", dcss: "DCS" })[g] || String(g || "?").slice(0, 3).toUpperCase();
}

export function fmtPts(x) {
  x = Math.round((x || 0) * 100) / 100;
  return Number.isInteger(x) ? String(x) : x.toFixed(x * 10 === Math.round(x * 10) ? 1 : 2);
}

export function fmtClock(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "--:--:--";
  return d.toISOString().slice(11, 19);
}
