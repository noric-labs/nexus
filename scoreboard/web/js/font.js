// Bitmap font for the race UI: 5x7 glyphs (plus a descender row) in a 6x9
// cell, drawn by hand for this project (no third-party font, no licence
// attached). Text is blitted from per-colour atlases with drawImage, so it
// stays crisp under nearest-neighbour scaling.

export const GW = 5, GH = 8, CW = 6, CH = 9;

// One glyph per line: the character, a space, then 7 or 8 rows.
const SRC = String.raw`
! ..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..
" .#.#. .#.#. ..... ..... ..... ..... .....
# .#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.
$ ..#.. .#### #.#.. .###. ..#.# ####. ..#..
% ##... ##..# ...#. ..#.. .#... #..## ...##
& .##.. #..#. #.#.. .#... #.#.# #..#. .##.#
' ..#.. ..#.. .#... ..... ..... ..... .....
( ...#. ..#.. .#... .#... .#... ..#.. ...#.
) .#... ..#.. ...#. ...#. ...#. ..#.. .#...
* ..... ..#.. #.#.# .###. #.#.# ..#.. .....
+ ..... ..#.. ..#.. ##### ..#.. ..#.. .....
, ..... ..... ..... ..... .##.. ..#.. .#...
- ..... ..... ..... ##### ..... ..... .....
. ..... ..... ..... ..... ..... .##.. .##..
/ ..... ....# ...#. ..#.. .#... #.... .....
0 .###. #...# #..## #.#.# ##..# #...# .###.
1 ..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
2 .###. #...# ....# ...#. ..#.. .#... #####
3 .###. #...# ....# ..##. ....# #...# .###.
4 ...#. ..##. .#.#. #..#. ##### ...#. ...#.
5 ##### #.... ####. ....# ....# #...# .###.
6 ..##. .#... #.... ####. #...# #...# .###.
7 ##### ....# ...#. ..#.. .#... .#... .#...
8 .###. #...# #...# .###. #...# #...# .###.
9 .###. #...# #...# .#### ....# ...#. .##..
: ..... .##.. .##.. ..... .##.. .##.. .....
; ..... .##.. .##.. ..... .##.. ..#.. .#...
< ...#. ..#.. .#... #.... .#... ..#.. ...#.
= ..... ..... ##### ..... ##### ..... .....
> .#... ..#.. ...#. ....# ...#. ..#.. .#...
? .###. #...# ....# ...#. ..#.. ..... ..#..
@ .###. #...# ....# .##.# #.#.# #.#.# .###.
A .###. #...# #...# ##### #...# #...# #...#
B ####. #...# #...# ####. #...# #...# ####.
C .###. #...# #.... #.... #.... #...# .###.
D ###.. #..#. #...# #...# #...# #..#. ###..
E ##### #.... #.... ####. #.... #.... #####
F ##### #.... #.... ####. #.... #.... #....
G .###. #...# #.... #.### #...# #...# .####
H #...# #...# #...# ##### #...# #...# #...#
I .###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.
J ..### ...#. ...#. ...#. ...#. #..#. .##..
K #...# #..#. #.#.. ##... #.#.. #..#. #...#
L #.... #.... #.... #.... #.... #.... #####
M #...# ##.## #.#.# #.#.# #...# #...# #...#
N #...# #...# ##..# #.#.# #..## #...# #...#
O .###. #...# #...# #...# #...# #...# .###.
P ####. #...# #...# ####. #.... #.... #....
Q .###. #...# #...# #...# #.#.# #..#. .##.#
R ####. #...# #...# ####. #.#.. #..#. #...#
S .#### #.... #.... .###. ....# ....# ####.
T ##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
U #...# #...# #...# #...# #...# #...# .###.
V #...# #...# #...# #...# #...# .#.#. ..#..
W #...# #...# #...# #.#.# #.#.# #.#.# .#.#.
X #...# #...# .#.#. ..#.. .#.#. #...# #...#
Y #...# #...# .#.#. ..#.. ..#.. ..#.. ..#..
Z ##### ....# ...#. ..#.. .#... #.... #####
[ .###. .#... .#... .#... .#... .#... .###.
\ ..... #.... .#... ..#.. ...#. ....# .....
] .###. ...#. ...#. ...#. ...#. ...#. .###.
^ ..#.. .#.#. #...# ..... ..... ..... .....
_ ..... ..... ..... ..... ..... ..... #####
a ..... ..... .###. ....# .#### #...# .####
b #.... #.... #.##. ##..# #...# #...# ####.
c ..... ..... .###. #.... #.... #...# .###.
d ....# ....# .##.# #..## #...# #...# .####
e ..... ..... .###. #...# ##### #.... .###.
f ..##. .#..# .#... ###.. .#... .#... .#...
g ..... ..... .#### #...# #...# .#### ....# .###.
h #.... #.... #.##. ##..# #...# #...# #...#
i ..#.. ..... .##.. ..#.. ..#.. ..#.. .###.
j ...#. ..... ..##. ...#. ...#. ...#. #..#. .##..
k #.... #.... #..#. #.#.. ##... #.#.. #..#.
l ##... .#... .#... .#... .#... .#... ..##.
m ..... ..... ##.#. #.#.# #.#.# #...# #...#
n ..... ..... #.##. ##..# #...# #...# #...#
o ..... ..... .###. #...# #...# #...# .###.
p ..... ..... ####. #...# #...# ####. #.... #....
q ..... ..... .##.# #..## #...# .#### ....# ....#
r ..... ..... #.##. ##..# #.... #.... #....
s ..... ..... .###. #.... .###. ....# ####.
t .#... .#... ###.. .#... .#... .#..# ..##.
u ..... ..... #...# #...# #...# #..## .##.#
v ..... ..... #...# #...# #...# .#.#. ..#..
w ..... ..... #...# #...# #.#.# #.#.# .#.#.
x ..... ..... #...# .#.#. ..#.. .#.#. #...#
y ..... ..... #...# #...# #...# .#### ....# .###.
z ..... ..... ##### ...#. ..#.. .#... #####
{ ...#. ..#.. ..#.. .#... ..#.. ..#.. ...#.
| ..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
} .#... ..#.. ..#.. ...#. ..#.. ..#.. .#...
~ ..... ..... .#... #.#.# ...#. ..... .....
`;

const glyphs = new Map(); // char code -> rows (array of 8 bitmasks, bit 4 = leftmost)

function parseRows(tokens) {
  const rows = new Array(GH).fill(0);
  tokens.forEach((t, i) => {
    let m = 0;
    for (let x = 0; x < GW; x++) if (t[x] === "#") m |= 1 << (GW - 1 - x);
    rows[i] = m;
  });
  return rows;
}

for (const line of SRC.split("\n")) {
  if (line.length < 3) continue;
  glyphs.set(line.charCodeAt(0), parseRows(line.slice(2).trim().split(/\s+/)));
}
glyphs.set(32, parseRows([]));
glyphs.set(96, parseRows(".#... ..#.. ...#. ..... ..... ..... .....".split(" ")));

// Non-ASCII characters that games commonly print, folded onto ASCII.
const FOLD = {
  "·": ".", "•": "*", "°": "o", "≈": "~", "…": ".", "–": "-", "—": "-", "‘": "'", "’": "'",
  "“": '"', "”": '"', "×": "x", "÷": "/", "←": "<", "→": ">", "↑": "^", "↓": "v", "♣": "&",
  "♠": "^", "♥": "<", "♦": "*", "Ω": "&", "∞": "8", "±": "+", "¬": "-", "¦": "|", "⌂": "^",
  "ⓘ": "i", "☺": "@", "☻": "@", "♪": "&", "≥": ">", "≤": "<", "■": "#", "□": "#",
};

/** Glyph rows for a character (folded to ASCII; unknown -> '?'). */
export function glyphRows(ch) {
  let c = typeof ch === "number" ? ch : ch.charCodeAt(0);
  let g = glyphs.get(c);
  if (!g) {
    const f = FOLD[String.fromCharCode(c)];
    g = f ? glyphs.get(f.charCodeAt(0)) : glyphs.get(63);
  }
  return g;
}

export function glyphCount() { return glyphs.size; }

let makeCanvas = (w, h) => {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  return c;
};
export function setCanvasFactory(f) { makeCanvas = f; }
export function newCanvas(w, h) { return makeCanvas(w, h); }

// Atlas: ASCII 32..126 in a row, one CW-wide slot per glyph.
const atlases = new Map();
function atlas(color) {
  let a = atlases.get(color);
  if (a) return a;
  a = makeCanvas(95 * CW, GH);
  const g = a.getContext("2d");
  g.fillStyle = color;
  for (let c = 32; c < 127; c++) {
    const rows = glyphs.get(c), ox = (c - 32) * CW;
    for (let y = 0; y < GH; y++) {
      const r = rows[y];
      if (!r) continue;
      // merge horizontal runs into one rect
      for (let x = 0; x < GW; x++) {
        if (!(r & (1 << (GW - 1 - x)))) continue;
        let e = x;
        while (e + 1 < GW && (r & (1 << (GW - 2 - e)))) e++;
        g.fillRect(ox + x, y, e - x + 1, 1);
        x = e;
      }
    }
  }
  atlases.set(color, a);
  return a;
}

function slot(ch) {
  const c = ch.charCodeAt(0);
  if (c >= 32 && c < 127) return c - 32;
  const f = FOLD[ch];
  return f ? f.charCodeAt(0) - 32 : 63 - 32;
}

/** Draw s at (x, y) (top-left), returns the x after the last glyph. */
export function text(ctx, s, x, y, color, scale = 1) {
  s = String(s);
  const a = atlas(color);
  x = Math.round(x); y = Math.round(y);
  for (const ch of s) {
    const i = slot(ch);
    if (i !== 0) ctx.drawImage(a, i * CW, 0, GW, GH, x, y, GW * scale, GH * scale);
    x += CW * scale;
  }
  return x;
}

export function textWidth(s, scale = 1) { return [...String(s)].length * CW * scale - scale; }

/** Right-aligned text ending at x. */
export function textR(ctx, s, x, y, color, scale = 1) {
  return text(ctx, s, x - textWidth(s, scale), y, color, scale);
}

/** Centred text around cx. */
export function textC(ctx, s, cx, y, color, scale = 1) {
  return text(ctx, s, cx - Math.floor(textWidth(s, scale) / 2), y, color, scale);
}

/** Truncate to n chars, marking the cut with '~'. */
export function fit(s, n) {
  s = String(s ?? "");
  return s.length <= n ? s : s.slice(0, Math.max(0, n - 1)) + "~";
}

// ---- terminal cells (box drawing and blocks drawn procedurally) ----

const BOX = {};
const boxSet = (chars, conn) => { for (const c of chars) BOX[c] = conn; };
// conn bits: 1 left, 2 right, 4 up, 8 down
boxSet("─━═╌╍┄┅┈┉", 3); boxSet("│┃║╎╏┆┇┊┋", 12);
boxSet("┌┏╔╒╓┍┎╭", 10); boxSet("┐┓╗╕╖┑┒╮", 9); boxSet("└┗╚╘╙┕┖╰", 6); boxSet("┘┛╝╛╜┙┚╯", 5);
boxSet("├┣╠╞╟┝┞┟┠┡┢", 14); boxSet("┤┫╣╡╢┥┦┧┨┩┪", 13); boxSet("┬┳╦╤╥┭┮┯┰┱┲", 11);
boxSet("┴┻╩╧╨┵┶┷┸┹┺", 7); boxSet("┼╋╬╪╫┽┾┿╀╁╂╃╄╅╆╇╈╉╊", 15);
boxSet("╴", 1); boxSet("╶", 2); boxSet("╵", 4); boxSet("╷", 8);

/**
 * Draw one terminal character in a cw x chh cell. Box drawing and block
 * elements are drawn as lines/fills so maps line up; everything else uses
 * the bitmap font (cell must be at least CW x GH).
 */
export function termChar(ctx, ch, x, y, cw, chh, color) {
  if (ch === " " || ch === "") return;
  const conn = BOX[ch];
  if (conn !== undefined) {
    const mx = x + (cw >> 1), my = y + (chh >> 1);
    ctx.fillStyle = color;
    if (conn & 1) ctx.fillRect(x, my, mx - x + 1, 1);
    if (conn & 2) ctx.fillRect(mx, my, x + cw - mx, 1);
    if (conn & 4) ctx.fillRect(mx, y, 1, my - y + 1);
    if (conn & 8) ctx.fillRect(mx, my, 1, y + chh - my);
    return;
  }
  ctx.fillStyle = color;
  switch (ch) {
    case "█": ctx.fillRect(x, y, cw, chh); return;
    case "▀": ctx.fillRect(x, y, cw, chh >> 1); return;
    case "▄": ctx.fillRect(x, y + (chh >> 1), cw, chh - (chh >> 1)); return;
    case "▌": ctx.fillRect(x, y, cw >> 1, chh); return;
    case "▐": ctx.fillRect(x + (cw >> 1), y, cw - (cw >> 1), chh); return;
    case "░": case "▒": case "▓": {
      const on = ch === "░" ? (a, b) => (a + 2 * b) % 4 === 0
        : ch === "▒" ? (a, b) => (a + b) % 2 === 0 : (a, b) => (a + b) % 4 !== 0;
      for (let yy = 0; yy < chh; yy++)
        for (let xx = 0; xx < cw; xx++) if (on(xx, yy)) ctx.fillRect(x + xx, y + yy, 1, 1);
      return;
    }
  }
  text(ctx, ch, x, y, color);
}
