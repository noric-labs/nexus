// The oval: a stadium (two straights, two semicircles) driven anticlockwise
// on screen, with the start/finish line on the bottom straight. Distance 0
// is the line; one lap is `length` pixels of centre line.

import { hash } from "./theme.js";

export class Track {
  constructor({ cx = 352, cy = 270, S = 150, R = 168, half = 22, startOff = 40 } = {}) {
    Object.assign(this, { cx, cy, S, R, half, startOff });
    this.length = 4 * S + 2 * Math.PI * R;
    this.u0 = S + startOff; // start line, measured from the left end of the bottom straight
  }

  /** Point at distance d along the centre line, offset lat outward. */
  at(d, lat = 0) {
    const { cx, cy, S, R, length } = this;
    let u = ((d + this.u0) % length + length) % length;
    let x, y, ang, nx, ny;
    if (u < 2 * S) {
      x = cx - S + u; y = cy + R; ang = 0; nx = 0; ny = 1;
    } else if ((u -= 2 * S) < Math.PI * R) {
      const t = Math.PI / 2 - u / R;
      nx = Math.cos(t); ny = Math.sin(t);
      x = cx + S + R * nx; y = cy + R * ny; ang = Math.atan2(-nx, ny);
    } else if ((u -= Math.PI * R) < 2 * S) {
      x = cx + S - u; y = cy - R; ang = Math.PI; nx = 0; ny = -1;
    } else {
      u -= 2 * S;
      const t = -Math.PI / 2 - u / R;
      nx = Math.cos(t); ny = Math.sin(t);
      x = cx - S + R * nx; y = cy + R * ny; ang = Math.atan2(-nx, ny);
    }
    return { x: x + nx * lat, y: y + ny * lat, ang };
  }

  /** Signed distance from the centre line (positive = outside) and whether on a curve. */
  off(x, y) {
    const { cx, cy, S, R } = this;
    if (Math.abs(x - cx) <= S) return { d: Math.abs(y - cy) - R, curve: false };
    const ccx = x > cx ? cx + S : cx - S;
    return { d: Math.hypot(x - ccx, y - cy) - R, curve: true, a: Math.atan2(y - cy, x - ccx) };
  }

  get startX() { return this.cx + this.startOff; }

  /** Lateral slot for a lane so cars at equal distance spread across the track. */
  laneLat(id) { return ((hash(id) % 7) - 3) * 5.5; }

  /** Paint grass, stands, asphalt, kerbs and the start line into an ImageData-capable ctx. */
  paint(ctx, w, h, seed = 7) {
    const img = ctx.createImageData(w, h);
    const px = img.data;
    let r = seed >>> 0;
    const rnd = () => ((r = (Math.imul(r, 1664525) + 1013904223) >>> 0) / 4294967296);
    const rgb = (hex) => [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    const P = {
      g1: rgb("#1f6b2a"), g2: rgb("#237a30"), gi1: rgb("#1a5a24"), gi2: rgb("#1d6328"),
      a1: rgb("#3a3a4a"), a2: rgb("#34343f"), a3: rgb("#42424f"), wl: rgb("#d8d8e0"),
      kr: rgb("#e0203a"), kw: rgb("#f0f0f0"), ck: rgb("#101010"), sand: rgb("#c8a868"),
    };
    const { cx, cy, S, R, half } = this;
    const sx = this.startX;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const o = this.off(x + 0.5, y + 0.5);
        const ad = Math.abs(o.d);
        let c;
        if (ad < half) {
          const n = rnd();
          c = n < 0.12 ? P.a2 : n > 0.9 ? P.a3 : P.a1;
          if (ad >= half - 1.5 && !o.curve) c = P.wl;
          // start/finish chequer across the bottom straight
          if (!o.curve && y > cy && x >= sx - 2 && x < sx + 4) c = (((x - sx + 2) >> 1) + (y >> 1)) % 2 ? P.ck : P.kw;
          // grid slots behind the line
          if (!o.curve && y > cy && x < sx - 6 && x > cx - S + 8 && (sx - x - 2) % 10 === 0 && ad < half - 4) c = P.wl;
        } else if (ad < half + 3 && o.curve) {
          const seg = Math.floor((o.a + Math.PI) * R / 5);
          c = seg % 2 ? P.kr : P.kw;
        } else if (ad < half + 6 && o.curve && o.d > 0) {
          c = rnd() < 0.5 ? P.sand : P.g1;
        } else {
          const inside = o.d < 0;
          const stripe = (Math.floor((x + y * 0.5) / 18) % 2) === 0;
          c = inside ? (stripe ? P.gi1 : P.gi2) : (stripe ? P.g1 : P.g2);
          if (rnd() < 0.04) c = P.gi1;
        }
        const i = (y * w + x) * 4;
        px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2]; px[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }
}
