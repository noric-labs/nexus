// Colours and naming for the race UI. Pixel-arcade palette (PICO-8-like).

export const W = 960, H = 540; // logical resolution; scaled by an integer to the display

export const C = {
  bg: "#0b0b14", panel: "#14142a", panel2: "#1d1d3a", line: "#3b3b6a", dim: "#5f5f8a",
  text: "#e8e8f0", muted: "#9a9ab8", gold: "#ffd23f", silver: "#c8d0e0", bronze: "#d0803a",
  red: "#ff004d", green: "#00e436", amber: "#ffa300", sky: "#29adff", white: "#fff1e8",
  black: "#000000", phosphor: "#7dff9a", phosphorDim: "#2a5a35", screenBg: "#061108",
};

// Failure types: one colour each, used by effects, tower flashes, log rows.
export const TYPE_COLOR = {
  sanitizer: "#ff3df5", crash: "#ff4422", hang: "#ffb000", error_exit: "#29adff",
  visual: "#00ff9c", agent_error: "#9a9ab8",
};
export const TYPE_SHORT = {
  sanitizer: "SANI", crash: "CRSH", hang: "HANG", error_exit: "EXIT", visual: "VIS", agent_error: "AGNT",
};
export const TYPES = ["sanitizer", "crash", "hang", "error_exit", "visual", "agent_error"];

// Constructor (fault config) liveries: body colours.
const LIVERY = ["#ff004d", "#ffa300", "#ffec27", "#00e436", "#29adff", "#ff77a8", "#c2c3c7",
  "#ab5236", "#83769c", "#ffccaa", "#1de9b6", "#7e2553", "#008751", "#ff6c24"];
const KNOWN_FAULTS = ["none", "malloc-1pct", "malloc-0.1pct", "io-save-5pct", "strace-enospc-save",
  "strace-eintr-read", "winch-storm", "rlimit-tight", "time-warp", "keystorm"];

// Game accents (stripe on the car).
const GAME_ACCENT = { brogue: "#ffd23f", "frotz-zork1": "#29adff", greed: "#00e436", dcss: "#ff77a8" };
const ACCENTS = ["#fff1e8", "#ffd23f", "#29adff", "#00e436", "#ff77a8", "#ffa300", "#83769c"];

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
