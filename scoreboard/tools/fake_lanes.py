#!/usr/bin/env python3
"""Fake lane driver for demos and UI testing (stdlib only).

Posts schema-v1 events and screen frames for N fake lanes to a running
scoreboard: lane_start, heartbeats every 5 s, random failures of every type
(crash, sanitizer, hang, error_exit, visual, agent_error) with repeating stack
hashes so the decay and clusters show, and an 80x24 screen (100x34 for Brogue)
per lane about once per second. When a new heat starts it recycles every lane
with a new seed, like lane/recycle would.

    ./scoreboard -heat 60s -pit 10s -listen :8088 -db /tmp/sb.db &
    python3 tools/fake_lanes.py --url http://localhost:8088 --lanes 40

Options: --lanes N, --rate R (mean failures per lane per minute), --duration S
(0 = forever), --seed, --screens/--no-screens.
"""

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

GAMES = ["brogue", "frotz-zork1", "greed", "dcss"]
GAME_VER = {"brogue": "1.14", "frotz-zork1": "2.54", "greed": "4.3", "dcss": "0.32"}
FAULTS = ["none", "malloc-1pct", "malloc-0.1pct", "io-save-5pct", "strace-enospc-save",
          "strace-eintr-read", "winch-storm", "rlimit-tight", "time-warp", "keystorm"]
MODELS = ["@cf/qwen/qwen3-30b-a3b-fp8", "@cf/meta/llama-4-scout-17b-16e-instruct",
          "@cf/mistralai/mistral-small-3.1-24b-instruct", "@cf/google/gemma-3-12b-it"]
# How nasty each fault is (failure-rate multiplier).
FAULT_HEAT = {"none": 0.1, "malloc-1pct": 2.0, "malloc-0.1pct": 1.0, "io-save-5pct": 1.2,
              "strace-enospc-save": 0.8, "strace-eintr-read": 0.9, "winch-storm": 1.4,
              "rlimit-tight": 1.6, "time-warp": 0.7, "keystorm": 1.1}
TYPES = [("crash", 30), ("sanitizer", 12), ("hang", 10), ("error_exit", 22), ("visual", 14), ("agent_error", 12)]
SAN_DETAIL = ["heap-use-after-free", "heap-buffer-overflow", "stack-buffer-overflow", "null-deref",
              "signed-integer-overflow", "shift-exponent", "double-free", "misaligned-address"]
EXIT_DETAIL = ["Out of memory!", "could not write save file", "fatal: read error", "terminal too small",
               "bad save header", "ENOSPC on save"]
VIS_DETAIL = ["garbled-rows", "blank-screen", "stuck-cursor", "torn-frame"]
HANG_DETAIL = ["no-screen-change-60s", "spin-100pct-cpu", "futex-wait"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Client:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.errors = 0

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.url + path, data=body.encode(), method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except (urllib.error.URLError, OSError) as e:
            self.errors += 1
            if self.errors < 5 or self.errors % 100 == 0:
                print(f"post {path}: {e}", file=sys.stderr)
            return 0

    def get_json(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=5) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"get {path}: {e}", file=sys.stderr)
            return None

    def events(self, evs):
        if evs:
            self.post("/events", "\n".join(json.dumps(e) for e in evs) + "\n", "application/x-ndjson")


class Lane:
    def __init__(self, i, rnd):
        self.id = f"l-{i:03d}"
        self.rnd = rnd
        self.game = GAMES[i % len(GAMES)]
        self.fault = FAULTS[(i * 7 + i // len(GAMES)) % len(FAULTS)]
        self.model = MODELS[i % len(MODELS)]
        self.seed = rnd.getrandbits(48)
        self.turn = 0
        self.tok_in = 0
        self.tok_out = 0
        self.hashes = ["".join(rnd.choice("0123456789abcdef") for _ in range(12)) for _ in range(4)]
        self.down_until = 0.0
        self.stalled_until = 0.0
        self.player = [rnd.randrange(5, 70), rnd.randrange(3, 20)]
        self.log = []

    @property
    def cols(self):
        return 100 if self.game == "brogue" else 80

    @property
    def rows(self):
        return 34 if self.game == "brogue" else 24

    def ev(self, typ, heat_id="", **kw):
        e = {"v": 1, "event_id": str(uuid.uuid4()), "ts": now_iso(), "heat_id": heat_id, "lane_id": self.id,
             "game": self.game, "game_ver": GAME_VER[self.game], "fault": self.fault, "seed": self.seed,
             "model": self.model, "type": typ, "turn": self.turn, "tokens_in": self.tok_in, "tokens_out": self.tok_out}
        e.update({k: v for k, v in kw.items() if v is not None})
        return e

    def failure(self, heat_id):
        typ = self.rnd.choices([t for t, _ in TYPES], [w for _, w in TYPES])[0]
        # a few hashes repeat a lot (decay), the odd one is brand new (new cluster)
        h = self.rnd.choice(self.hashes) if self.rnd.random() < 0.85 else "%012x" % self.rnd.getrandbits(48)
        kw = {}
        if typ == "crash":
            kw = dict(signal=self.rnd.choice([11, 11, 11, 6, 7, 8]), stack_hash=h,
                      detail=self.rnd.choice(["SIGSEGV in dungeon gen", "SIGABRT in free()", "SIGBUS on save mmap", "SIGSEGV in ncurses"]))
            if "ncurses" in kw["detail"]:
                kw["stack_hash"] = "ncurses:" + h[:4]
        elif typ == "sanitizer":
            kw = dict(stack_hash=h, detail=self.rnd.choice(SAN_DETAIL))
        elif typ == "hang":
            kw = dict(detail=self.rnd.choice(HANG_DETAIL))
            self.stalled_until = time.time() + 4
        elif typ == "error_exit":
            kw = dict(exit_code=self.rnd.choice([1, 1, 2, 3, 127]), detail=self.rnd.choice(EXIT_DETAIL), stack_hash=h if self.rnd.random() < 0.3 else None)
        elif typ == "visual":
            kw = dict(detail=self.rnd.choice(VIS_DETAIL))
        else:
            kw = dict(exit_code=130, signal=2, detail="agent sent ^C")
        kw["replay"] = None if typ == "agent_error" else f"/out/replay/{uuid.uuid4()}.tar.zst"
        self.log.append(f"*** {typ.upper()} {kw.get('detail', '')}")
        return self.ev(typ, heat_id, **kw)

    def step(self):
        self.turn += self.rnd.randrange(1, 4)
        self.tok_in += self.rnd.randrange(2000, 9000)
        self.tok_out += self.rnd.randrange(50, 400)
        self.player[0] = max(1, min(self.cols - 2, self.player[0] + self.rnd.choice([-1, 0, 1])))
        self.player[1] = max(1, min(self.rows - 5, self.player[1] + self.rnd.choice([-1, 0, 1])))

    def screen(self):
        r = random.Random(self.seed)  # stable map per seed
        cols, rows = self.cols, self.rows
        if self.game == "frotz-zork1":
            lines = [" West of House" + " " * 50 + f"Score: {self.turn // 20}  Moves: {self.turn}", ""]
            text = ["You are standing in an open field west of a white house, with a boarded",
                    "front door.", "There is a small mailbox here.", "", ">open mailbox",
                    "Opening the small mailbox reveals a leaflet.", "", ">read leaflet",
                    "\"WELCOME TO ZORK!\"", "", f">{self.rnd.choice(['north', 'take lamp', 'xyzzy', 'inventory'])}"]
            lines += text + [ln for ln in self.log[-6:]]
            cur = [len(lines[-1]) if lines else 0, min(rows - 1, len(lines) - 1)]
        elif self.game == "greed":
            lines = []
            for y in range(rows - 1):
                lines.append("".join(str(r.randrange(1, 10)) for _ in range(cols - 1)))
            px, py = self.player
            row = list(lines[py]); row[px] = "@"; lines[py] = "".join(row)
            lines.append(f"Score: {self.turn * 3}  {self.turn / 10:.2f}%   Greed v4.3  {self.log[-1] if self.log else ''}")
            cur = [px, py]
        else:
            # brogue / dcss: rooms drawn with box characters, monsters, status lines
            grid = [[" "] * cols for _ in range(rows)]
            for _ in range(6):
                w, h = r.randrange(8, 22), r.randrange(4, 9)
                x, y = r.randrange(0, cols - w - 1), r.randrange(0, rows - h - 4)
                for yy in range(y, y + h):
                    for xx in range(x, x + w):
                        edge_y, edge_x = yy in (y, y + h - 1), xx in (x, x + w - 1)
                        if edge_y and edge_x:
                            ch = "┌" if (yy == y and xx == x) else "┐" if yy == y else "└" if xx == x else "┘"
                        elif edge_y:
                            ch = "─"
                        elif edge_x:
                            ch = "│"
                        else:
                            ch = "·" if self.game == "brogue" else "."
                        grid[yy][xx] = ch
            for _ in range(10):
                grid[r.randrange(rows - 4)][r.randrange(cols)] = r.choice("gkoZ$!?%")
            px, py = self.player
            grid[py][px] = "@"
            lines = ["".join(row) for row in grid[:rows - 3]]
            lines.append("Health ████████░░░░  Str 12  Lvl 3  Depth %d   Turn %d" % (1 + self.turn // 200, self.turn))
            lines.append(self.log[-1] if self.log else "Welcome, adventurer.")
            lines.append(f"[{self.fault}] seed {self.seed}")
            cur = [px, py]
        lines = [ln[:cols] for ln in lines[:rows]]
        return {"ts": now_iso(), "cols": cols, "rows": rows, "cursor": cur, "lines": lines}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--lanes", type=int, default=40)
    ap.add_argument("--rate", type=float, default=2.0, help="mean failures per lane per minute")
    ap.add_argument("--duration", type=float, default=0, help="seconds to run (0 = forever)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--screens", action=argparse.BooleanOptionalAction, default=True)
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    c = Client(a.url)
    st = c.get_json("/api/state")
    if st is None:
        sys.exit(f"scoreboard not reachable at {a.url}")
    heat = st["heat"]["id"]
    lanes = [Lane(i + 1, rnd) for i in range(a.lanes)]
    c.events([ln.ev("lane_start", heat) for ln in lanes])
    print(f"{len(lanes)} lanes started in {heat}", file=sys.stderr)

    t0 = last_hb = last_state = time.time()
    sent = {"failures": 0, "frames": 0}
    screen_i = 0
    tick = 0.5
    pit = st["heat"]["phase"] == "pit"
    while not a.duration or time.time() - t0 < a.duration:
        loop = time.time()
        if loop - last_state > 2:
            last_state = loop
            st = c.get_json("/api/state")
            if st and st["heat"]["id"] != heat and st["heat"]["phase"] == "race":
                old, heat = heat, st["heat"]["id"]
                evs = []
                for ln in lanes:  # recycle: end, new seed, start
                    evs.append(ln.ev("lane_end", old))
                    ln.seed, ln.turn, ln.tok_in, ln.tok_out, ln.log = rnd.getrandbits(48), 0, 0, 0, []
                    evs.append(ln.ev("lane_start", heat))
                c.events(evs)
                print(f"recycled {len(lanes)} lanes into {heat}", file=sys.stderr)
            if st:
                pit = st["heat"]["phase"] == "pit"
        evs = []
        for ln in lanes:
            if loop < ln.down_until:
                continue
            if ln.down_until and loop >= ln.down_until:
                ln.down_until = 0
                evs.append(ln.ev("lane_start", heat))
            ln.step()
            p = a.rate / 60 * tick * FAULT_HEAT.get(ln.fault, 1)
            if not pit and loop > ln.stalled_until and rnd.random() < p:
                evs.append(ln.failure(heat))
                sent["failures"] += 1
                if rnd.random() < 0.02:  # the odd lane dies for a while
                    evs.append(ln.ev("lane_end", heat))
                    ln.down_until = loop + rnd.uniform(5, 20)
        if loop - last_hb >= 5:
            last_hb = loop
            evs += [ln.ev("heartbeat", heat) for ln in lanes if not ln.down_until]
        c.events(evs)
        if a.screens:
            # each lane about once a second (well under the 2/s limit)
            n = max(1, round(len(lanes) * tick))
            for _ in range(n):
                ln = lanes[screen_i % len(lanes)]
                screen_i += 1
                c.post(f"/lanes/{ln.id}/screen", json.dumps(ln.screen()))
                sent["frames"] += 1
        time.sleep(max(0, tick - (time.time() - loop)))
    print(f"done: {sent['failures']} failures, {sent['frames']} frames, {c.errors} post errors", file=sys.stderr)


if __name__ == "__main__":
    main()
