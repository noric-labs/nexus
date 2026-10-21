"""wall-pager: cycle wall pages, keep the Hot tab on recent failures, follow focus requests.

Runs next to a zellij session started from wall-gen's wall.kdl. It talks to zellij only
through `zellij --session S action ...` and to the scoreboard through GET /api/state
and the SSE GET /stream (`failure`, `focus`, `state`).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request

from . import common as c


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def sse(url: str, q: queue.Queue, stop: threading.Event):
    """Read SSE frames forever (reconnecting with Last-Event-ID) into q as (event, data)."""
    last_id = None
    while not stop.is_set():
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        if last_id:
            req.add_header("Last-Event-ID", last_id)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                log("stream connected", url)
                ev, data = "message", []
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data:
                            try:
                                q.put((ev, json.loads("\n".join(data))))
                            except ValueError:
                                pass
                        ev, data = "message", []
                    elif line.startswith(":"):
                        continue
                    else:
                        k, _, v = line.partition(":")
                        v = v[1:] if v.startswith(" ") else v
                        if k == "event":
                            ev = v
                        elif k == "data":
                            data.append(v)
                        elif k == "id":
                            last_id = v
        except Exception as e:  # noqa: BLE001 - any network error: back off and reconnect
            log("stream error:", e)
        stop.wait(2)


class Pager:
    def __init__(self, a, index: dict):
        self.a, self.index = a, index
        self.cfg = index["config"]
        self.tabs = [p["tab"] for p in index["pages"]] + [c.HOT_TAB]
        self.lanes = {lid: c.Lane(id=lid, game=v["game"], fault=v["fault"]) for lid, v in index["lanes"].items()}
        cols, rows = self.cfg["hot_grid"]
        self.hot: list[str | None] = (index.get("hot", []) + [None] * (cols * rows))[: cols * rows]
        self.recency = {lid: -i for i, lid in enumerate(self.hot) if lid}  # higher = more recent
        self.clock = 0
        self.cur = -1
        self.next_flip = 0.0
        self.hold_until = 0.0
        self.held: tuple[str, str] | None = None  # (pane id, tab) highlighted by focus

    # zellij plumbing -------------------------------------------------------
    def z(self, *args: str, out: bool = False) -> str:
        cmd = ["zellij", "--session", self.a.session, "action", *args]
        if self.a.dry_run:
            log("[dry-run]", " ".join(args))
            return "[]" if out else ""
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            log("zellij", " ".join(args), "->", r.returncode, r.stderr.strip()[:200])
        return r.stdout

    def panes(self) -> list[dict]:
        try:
            return [p for p in json.loads(self.z("list-panes", "-j", "-a", out=True) or "[]") if not p["is_plugin"]]
        except ValueError:
            return []

    def find_pane(self, lane: str, tab: str | None = None) -> str | None:
        for p in self.panes():
            if (p.get("title") or "").startswith(lane + c.SEP) and (tab is None or p.get("tab_name") == tab):
                return f"terminal_{p['id']}"
        return None

    def slot_pane(self, i: int) -> str | None:
        path = self.slot_path(i)
        for p in self.panes():
            if path in (p.get("terminal_command") or ""):
                return f"terminal_{p['id']}"
        return None

    def slot_path(self, i: int) -> str:
        return os.path.join(self.cfg["slot_dir"], f"slot-{i:02d}")

    def goto(self, i: int):
        self.cur = i % len(self.tabs)
        self.z("go-to-tab-name", self.tabs[self.cur])

    # behaviours -----------------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        if self.held and now >= self.hold_until:
            pane, _ = self.held
            self.z("set-pane-color", "-p", pane, "--reset")
            if self.a.zoom:
                self.z("toggle-fullscreen", "-p", pane)
            self.held = None
            self.next_flip = now  # resume cycling right away
        if self.held is None and self.a.interval > 0 and now >= self.next_flip:
            self.goto(self.cur + 1)
            self.next_flip = now + self.a.interval

    def note_lane(self, d: dict) -> c.Lane:
        lid = d["lane_id"]
        l = self.lanes.get(lid) or c.Lane(id=lid)
        changed = False
        for k in ("game", "fault"):
            if d.get(k) and d[k] != getattr(l, k):
                setattr(l, k, d[k])
                changed = lid in self.lanes
        self.lanes[lid] = l
        if changed:  # recycled with a new config: retitle its panes
            for p in self.panes():
                if (p.get("title") or "").startswith(lid + c.SEP):
                    self.z("rename-pane", "-p", f"terminal_{p['id']}", l.title)
        return l

    def hot_add(self, lane: c.Lane):
        """Most-recent-failure set of fixed size; a newcomer takes an empty or the stalest slot."""
        self.clock += 1
        self.recency[lane.id] = self.clock
        if lane.id in self.hot:
            return
        if None in self.hot:
            i = self.hot.index(None)
        else:
            i = min(range(len(self.hot)), key=lambda j: self.recency.get(self.hot[j], -1e9))
            self.recency.pop(self.hot[i], None)
        self.hot[i] = lane.id
        tmp = self.slot_path(i) + ".tmp"
        if not self.a.dry_run:
            with open(tmp, "w") as f:
                f.write(c.lane_command(lane, self.cfg["cmd"], self.cfg["session_fmt"]) + "\n")
            os.replace(tmp, self.slot_path(i))
        pane = self.slot_pane(i)
        if pane:
            self.z("rename-pane", "-p", pane, lane.title)
        log(f"hot slot {i:02d} <- {lane.id}")

    def focus(self, lid: str):
        tab = self.index["lanes"].get(lid, {}).get("tab")
        if tab is None and lid in self.hot:
            tab = c.HOT_TAB
        if tab is None:
            log("focus: unknown lane", lid)
            return
        if self.held:  # release the previous focus first
            self.hold_until = 0
            self.tick()
        self.cur = self.tabs.index(tab)
        self.z("go-to-tab-name", tab)
        pane = self.find_pane(lid, tab) if tab != c.HOT_TAB else self.slot_pane(self.hot.index(lid))
        if pane:
            self.z("focus-pane-id", pane)
            self.z("set-pane-color", "-p", pane, "--bg", self.a.highlight)
            if self.a.zoom:
                self.z("toggle-fullscreen", "-p", pane)
            self.held = (pane, tab)
        self.hold_until = time.monotonic() + self.a.hold
        self.next_flip = self.hold_until
        log("focus", lid, "->", tab, pane)

    def handle(self, ev: str, d: dict):
        if ev == "failure" and d.get("lane_id") and d.get("type") != "agent_error":
            self.hot_add(self.note_lane(d))
        elif ev == "focus" and d.get("lane_id"):
            self.focus(d["lane_id"])
        elif ev == "state":
            for row in d.get("lanes") or []:
                if row.get("lane_id"):
                    self.note_lane(row)

    def seed_hot(self, state: dict):
        rows = [r for r in state.get("lanes", []) if (r.get("last_failure") or {}).get("ts")]
        for r in sorted(rows, key=lambda r: r["last_failure"]["ts"])[-len(self.hot):]:
            self.hot_add(self.note_lane(r))


def main(argv=None):
    p = argparse.ArgumentParser(prog="wall-pager", description=__doc__)
    p.add_argument("--session", default=os.environ.get("ZELLIJ_SESSION_NAME", "nexus-wall"), help="zellij session")
    p.add_argument("--scoreboard", default=os.environ.get("NEXUS_SCOREBOARD_URL", "http://localhost:8080"))
    p.add_argument("--out", default="out", help="wall-gen output directory (reads index.json)")
    p.add_argument("--interval", type=float, default=20, help="seconds per page; 0 disables cycling (default 20)")
    p.add_argument("--hold", type=float, default=30, help="seconds to stay on a focused lane (default 30)")
    p.add_argument("--highlight", default="#5a1a00", help="background for the focused pane")
    p.add_argument("--zoom", action="store_true", help="also fullscreen the focused pane while held")
    p.add_argument("--no-stream", action="store_true", help="only cycle pages")
    p.add_argument("--dry-run", action="store_true", help="log zellij actions instead of running them")
    a = p.parse_args(argv)

    with open(os.path.join(a.out, "index.json")) as f:
        pager = Pager(a, json.load(f))
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    if not a.no_stream:
        base = a.scoreboard.rstrip("/")
        try:
            pager.seed_hot(c.fetch_json(base + "/api/state"))
        except Exception as e:  # noqa: BLE001
            log("initial /api/state failed:", e)
        threading.Thread(target=sse, args=(base + "/stream", q, stop), daemon=True).start()
    log(f"paging {len(pager.tabs)} tabs every {a.interval:g}s in session {a.session}")
    try:
        while True:
            pager.tick()
            try:
                ev, d = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(d, dict):
                pager.handle(ev, d)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
