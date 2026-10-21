"""wall-gen: write one zellij KDL layout per wall page, plus wall.kdl (all pages + Hot)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import common as c


def hot_layout(lanes: list[c.Lane], cfg: dict, slot_dir: str) -> str:
    """Hot tab: a fixed grid of wall-slot panes; wall-pager repoints slots by rewriting files."""
    cols, rows = cfg["hot_grid"]
    os.makedirs(slot_dir, exist_ok=True)
    slots = []
    for i in range(cols * rows):
        lane = lanes[i] if i < len(lanes) else None
        path = os.path.join(slot_dir, f"slot-{i:02d}")
        with open(path, "w") as f:
            f.write(c.lane_command(lane, cfg["cmd"], cfg["session_fmt"]) + "\n" if lane else "")
        title = lane.title if lane else f"{c.HOT_TAB} {i:02d}"
        slots.append((lane, title,
                      [sys.executable, "-m", "nexus_wall.slot", path, str(cfg["retry"])]))
    return c.render_tab(c.HOT_TAB, slots, cols, rows)


def paginate(lanes: list[c.Lane]) -> list[dict]:
    """Group lanes by terminal size (then game) and chunk each group into grid pages."""
    by_size: dict[tuple[int, int], list[c.Lane]] = {}
    for l in lanes:
        by_size.setdefault(l.size, []).append(l)
    pages = []
    for size in sorted(by_size):
        group = sorted(by_size[size], key=lambda l: (l.game, c.natural_key(l.id)))
        cols, rows = c.GRIDS.get(size, c.GRIDS[c.DEFAULT_SIZE])
        per = cols * rows
        chunks = [group[i : i + per] for i in range(0, len(group), per)]
        if len(chunks) == 1:  # one page: shrink the grid to fit, so a few lanes get big panes
            cols, rows = c.fit_grid(len(group), (cols, rows))
        for i, chunk in enumerate(chunks, 1):
            pages.append({
                "tab": f"{size[0]}x{size[1]} {i}/{len(chunks)}",
                "size": list(size),
                "grid": [cols, rows],
                "lanes": chunk,
            })
    return pages


def main(argv=None):
    p = argparse.ArgumentParser(prog="wall-gen", description=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--scoreboard", default=os.environ.get("NEXUS_SCOREBOARD_URL", "http://localhost:8080"),
                     help="scoreboard base URL; lanes come from GET /api/state")
    src.add_argument("--lanes-file", help="JSON file: an /api/state snapshot or a list of lane objects")
    p.add_argument("--out", default="out", help="output directory (default: ./out)")
    p.add_argument("--cmd", default=c.DEFAULT_CMD,
                   help="pane command template; fields {session} {lane_id} {num} {digits} {game} {fault} "
                        f"(default: {c.DEFAULT_CMD!r})")
    p.add_argument("--session-fmt", default=c.DEFAULT_SESSION_FMT,
                   help=f"how {{session}} is built from the lane (default: {c.DEFAULT_SESSION_FMT!r})")
    p.add_argument("--retry", type=float, default=3,
                   help="seconds between re-attach attempts; 0 runs the command once (default 3)")
    p.add_argument("--hot-size", type=int, default=None,
                   help="lanes in the Hot tab (default: the lane count, at most 64, an 8x8 grid)")
    a = p.parse_args(argv)

    lanes = c.load_lanes(a.scoreboard, a.lanes_file)
    if not lanes:
        sys.exit("wall-gen: no lanes")
    if a.hot_size is None:
        a.hot_size = min(64, len(lanes))
    hot_cols = 8 if a.hot_size > 36 else 6
    hot_grid = list(c.fit_grid(a.hot_size, (hot_cols, max(hot_cols, -(-a.hot_size // hot_cols)))))
    cfg = {"cmd": a.cmd, "session_fmt": a.session_fmt, "retry": a.retry,
           "hot_size": a.hot_size, "slot_dir": os.path.abspath(os.path.join(a.out, "hot")), "hot_grid": hot_grid}

    os.makedirs(a.out, exist_ok=True)
    for f in os.listdir(a.out):  # drop stale pages from a previous, larger run
        if f.startswith("page-") and f.endswith(".kdl"):
            os.remove(os.path.join(a.out, f))

    pages = paginate(lanes)
    tabs, index = [], {"config": cfg, "pages": [], "lanes": {}}
    for n, pg in enumerate(pages, 1):
        cols, rows = pg["grid"]
        slots = [(l, l.title, c.pane_command(l, a.cmd, a.session_fmt, a.retry)) for l in pg["lanes"]]
        tab = c.render_tab(pg["tab"], slots, cols, rows)
        tabs.append(tab)
        fname = f"page-{n:02d}.kdl"
        with open(os.path.join(a.out, fname), "w") as f:
            f.write(c.render_layout([tab]))
        index["pages"].append({"tab": pg["tab"], "file": fname, "size": pg["size"], "grid": pg["grid"],
                               "lanes": [l.id for l in pg["lanes"]]})
        for l in pg["lanes"]:
            index["lanes"][l.id] = {"tab": pg["tab"], "title": l.title, "game": l.game, "fault": l.fault}

    failed = sorted((l for l in lanes if l.last_failure_ts), key=lambda l: l.last_failure_ts, reverse=True)
    index["hot"] = [l.id for l in failed[: a.hot_size]]
    hot = hot_layout(failed[: a.hot_size], cfg, os.path.abspath(os.path.join(a.out, "hot")))
    with open(os.path.join(a.out, "hot.kdl"), "w") as f:
        f.write(c.render_layout([hot]))
    with open(os.path.join(a.out, "wall.kdl"), "w") as f:
        f.write(c.render_layout(tabs + [hot]))
    with open(os.path.join(a.out, "index.json"), "w") as f:
        json.dump(index, f, indent=1)
    print(f"wall-gen: {len(lanes)} lanes -> {len(pages)} pages + {c.HOT_TAB} ({len(index['hot'])} lanes) in {a.out}/")
    for pg in index["pages"]:
        print(f"  {pg['file']}  {pg['tab']:<14} {len(pg['lanes'])} lanes")


if __name__ == "__main__":
    main()
