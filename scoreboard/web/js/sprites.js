// Procedural car sprites. A car is a small palette pattern facing +x; it is
// pre-rotated into DIRS directions by nearest-neighbour inverse mapping, so
// every rotated sprite is still made of whole logical pixels.

import { newCanvas } from "./font.js";

export const DIRS = 32;

// B body, D body shade, A accent (game), W cockpit, K tyre, Y light, S wing
const SMALL = [
  ".KK..KK.",
  "DBBBWBBY",
  "SAAAWAAB",
  "DBBBWBBY",
  ".KK..KK.",
];
const BIG = [
  "..KKK....KKK..",
  "S.DBBBBBBBBB..",
  "SDBBAAWWBBBBBY",
  "SBAAAAWWAAAAAB",
  "SDBBAAWWBBBBBY",
  "S.DBBBBBBBBB..",
  "..KKK....KKK..",
];

function shade(hex, f) {
  const n = parseInt(hex.slice(1), 16);
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => Math.max(0, Math.min(255, Math.round(v * f))));
  return "#" + c.map(v => v.toString(16).padStart(2, "0")).join("");
}

const cache = new Map();

/** Returns {img, n} where img is an n x n canvas centred on the car. */
export function carSprite(big, body, accent, dir) {
  dir = ((dir % DIRS) + DIRS) % DIRS;
  const key = `${big ? 1 : 0}|${body}|${accent}|${dir}`;
  let s = cache.get(key);
  if (s) return s;
  const pat = big ? BIG : SMALL;
  const ph = pat.length, pw = pat[0].length;
  const pal = { B: body, D: shade(body, 0.7), A: accent, W: "#1d2b53", K: "#111118", Y: "#fff6a0", S: shade(body, 0.45) };
  const n = Math.ceil(Math.hypot(pw, ph)) + 1;
  const img = newCanvas(n, n);
  const g = img.getContext("2d");
  const a = (dir / DIRS) * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a);
  for (let oy = 0; oy < n; oy++) {
    for (let ox = 0; ox < n; ox++) {
      const x = ox + 0.5 - n / 2, y = oy + 0.5 - n / 2;
      const sx = Math.floor(x * ca + y * sa + pw / 2), sy = Math.floor(-x * sa + y * ca + ph / 2);
      if (sx < 0 || sy < 0 || sx >= pw || sy >= ph) continue;
      const k = pat[sy][sx];
      if (k === ".") continue;
      g.fillStyle = pal[k];
      g.fillRect(ox, oy, 1, 1);
    }
  }
  s = { img, n };
  cache.set(key, s);
  return s;
}

export function dirOf(ang) { return Math.round((ang / (Math.PI * 2)) * DIRS); }

/** Draw a car centred at (x, y). scale is an integer magnification. */
export function drawCar(ctx, x, y, ang, big, body, accent, scale = 1) {
  const s = carSprite(big, body, accent, dirOf(ang));
  const n = s.n * scale;
  ctx.drawImage(s.img, 0, 0, s.n, s.n, Math.round(x - n / 2), Math.round(y - n / 2), n, n);
  return s;
}
