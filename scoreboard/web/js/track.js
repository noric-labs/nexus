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

  /** Paint the void, grid floor, deck, neon rails, speed pads and start gate into an ImageData-capable ctx.
   *  Speed-pad chevron pixels are kept in this.padPix ([x, y, k] with k the chevron phase 0..3) so the
   *  renderer can pulse them. */
  paint(ctx, w, h, seed = 7) {
    const img = ctx.createImageData(w, h);
    const px = img.data;
    let r = seed >>> 0;
    const rnd = () => ((r = (Math.imul(r, 1664525) + 1013904223) >>> 0) / 4294967296);
    const rgb = (hex) => [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    const P = {
      v: rgb("#04050a"), vg: rgb("#0b0f20"), iv: rgb("#05070f"), ig: rgb("#0d1226"), st: rgb("#3a4470"), st2: rgb("#9fb0e0"),
      d1: rgb("#0d1020"), d2: rgb("#10142a"), seam: rgb("#1a2140"), slot: rgb("#2a3560"),
      cy: rgb("#00f0ff"), cyh: rgb("#00505a"), mg: rgb("#ff2bd6"), mgh: rgb("#5a1050"),
      ck: rgb("#00f0ff"), kw: rgb("#eef6ff"), pad: rgb("#f5e800"), padD: rgb("#4a4600"),
    };
    const { cx, cy, S, R, half } = this;
    const sx = this.startX;
    this.padPix = [];
    // speed pads: bottom straight (travel +x) after the line, top straight (travel -x) mid-way
    const pad = (x, y, o) => {
      if (o.curve || Math.abs(o.d) >= half - 5) return -1;
      let u;
      if (y > cy && x >= cx + 80 && x < cx + 112) u = x - (cx + 80);
      else if (y < cy && x >= cx - 16 && x < cx + 16) u = cx + 15 - x;
      else return -1;
      const v = u + Math.abs(o.d) * 0.5;
      if (v >= 32 || ((v % 8) + 8) % 8 >= 3) return -2;
      return Math.floor(v / 8) & 3;
    };
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const o = this.off(x + 0.5, y + 0.5);
        const ad = Math.abs(o.d), out = o.d > 0;
        let c;
        if (ad < half - 2) {
          c = rnd() < 0.1 ? P.d2 : P.d1;
          const seam = o.curve ? (((o.a + Math.PI) * R / 24) % 1) < 0.06 : x % 24 === 0;
          if (seam) c = P.seam;
          if (ad >= half - 3) c = out ? P.cyh : P.mgh;
          const k = pad(x, y, o);
          if (k >= 0) { c = P.pad; this.padPix.push([x, y, k]); }
          else if (k === -2) c = P.padD;
          // start gate chequer across the bottom straight
          if (!o.curve && y > cy && x >= sx - 2 && x < sx + 4) c = (((x - sx + 2) >> 1) + (y >> 1)) % 2 ? P.ck : P.kw;
          // grid slots behind the line
          if (!o.curve && y > cy && x < sx - 6 && x > cx - S + 8 && (sx - x - 2) % 10 === 0 && ad < half - 4) c = P.slot;
        } else if (ad < half) {
          c = out ? P.cy : P.mg;
        } else if (ad < half + 1.5) {
          c = out ? P.cyh : P.mgh;
        } else {
          const inside = o.d < 0;
          const grid = x % 32 === 0 || y % 32 === 0;
          c = inside ? (grid ? P.ig : P.iv) : (grid ? P.vg : P.v);
          const n = rnd();
          if (n < 0.0006) c = P.st2; else if (n < 0.003) c = P.st;
        }
        const i = (y * w + x) * 4;
        px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2]; px[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }
}
