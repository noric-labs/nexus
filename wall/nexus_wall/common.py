"""Shared bits for wall-gen and wall-pager: lanes, pane commands, KDL rendering."""

from __future__ import annotations

import json
import re
import shlex
import urllib.request
from dataclasses import dataclass

# Lane terminal size per game (CONTRACTS.md: 80x24, or 100x34 for Brogue) and the
# page grid used for that size (PLAN.md wall decision).
SIZES = {"brogue": (100, 34)}
DEFAULT_SIZE = (80, 24)
GRIDS = {(80, 24): (8, 8), (100, 34): (6, 6)}  # (cols, rows) of panes per page

# Default pane command. `min session exec` runs on the Mac host; `{session}` is the
# Minimal session name of the lane. Override with --cmd (see README).
DEFAULT_CMD = "min session exec {session} -- tmux attach -r -t game"
DEFAULT_SESSION_FMT = "lane-{num}"
# Re-attach loop: recycled lanes come back without zellij having to re-run anything.
RETRY_SH = 'while :; do {cmd}; printf "\\r\\n\\033[2m[{lane}: detached, retrying in {delay}s]\\033[0m\\r\\n"; sleep {delay}; done'
HOT_TAB = "Hot"
SEP = " · "  # " · "


@dataclass
class Lane:
    id: str
    game: str = "?"
    fault: str = "?"
    last_failure_ts: str = ""

    @property
    def size(self) -> tuple[int, int]:
        return SIZES.get(self.game, DEFAULT_SIZE)

    @property
    def title(self) -> str:
        return SEP.join([self.id, self.game or "?", self.fault or "?"])


def lane_from_json(d: dict) -> Lane:
    lf = d.get("last_failure") or {}
    return Lane(
        id=d.get("lane_id") or d.get("id"),
        game=d.get("game") or "?",
        fault=d.get("fault") or "?",
        last_failure_ts=lf.get("ts", "") if isinstance(lf, dict) else "",
    )


def fetch_json(url: str, timeout: float = 10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def load_lanes(scoreboard: str | None, lanes_file: str | None) -> list[Lane]:
    """Lanes from a file (an /api/state snapshot or a JSON list) or GET /api/state."""
    if lanes_file:
        with open(lanes_file) as f:
            data = json.load(f)
    else:
        data = fetch_json(scoreboard.rstrip("/") + "/api/state")
    rows = data.get("lanes", []) if isinstance(data, dict) else data
    lanes = [lane_from_json(r) for r in rows if (r.get("lane_id") or r.get("id"))]
    return sorted(lanes, key=lambda l: natural_key(l.id))


def natural_key(s: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def lane_fields(lane: Lane, session_fmt: str) -> dict:
    digits = "".join(re.findall(r"\d+", lane.id)) or lane.id
    f = {
        "lane_id": lane.id,
        "digits": digits,
        "num": str(int(digits)) if digits.isdigit() else digits,
        "game": lane.game,
        "fault": lane.fault,
    }
    f["session"] = session_fmt.format(**f)
    return f


def lane_command(lane: Lane, cmd_tmpl: str, session_fmt: str) -> str:
    """The templated attach command for a lane, as a shell string."""
    f = {k: shlex.quote(v) for k, v in lane_fields(lane, session_fmt).items()}
    return cmd_tmpl.format(**f)


def pane_command(lane: Lane, cmd_tmpl: str, session_fmt: str, retry: float) -> list[str]:
    """argv for the pane: sh -c '<re-attach loop around the templated command>'."""
    cmd = lane_command(lane, cmd_tmpl, session_fmt)
    if retry <= 0:
        return ["sh", "-c", cmd]
    return ["sh", "-c", RETRY_SH.format(cmd=cmd, lane=lane.id.replace('"', ""), delay=retry)]


def kdl_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def placeholder_pane(indent: str) -> str:
    # Empty slot: keeps every pane on a page the same size.
    return f'{indent}pane name=" " command="tail" {{ args "-f" "/dev/null"; }}\n'


def render_tab(name: str, slots: list[tuple[Lane, str, list[str]] | None], cols: int, rows: int) -> str:
    """One tab: `rows` rows of `cols` panes of (lane, title, argv); None slots become placeholders."""
    slots = list(slots) + [None] * (cols * rows - len(slots))
    out = [f"    tab name={kdl_str(name)} {{\n"]
    for r in range(rows):
        out.append('        pane split_direction="vertical" {\n')
        for c in range(cols):
            s = slots[r * cols + c]
            if s is None:
                out.append(placeholder_pane("            "))
                continue
            _, title, argv = s
            args = " ".join(kdl_str(a) for a in argv[1:])
            out.append(
                f"            pane name={kdl_str(title)} command={kdl_str(argv[0])}"
                f" close_on_exit=false {{ args {args}; }}\n"
            )
        out.append("        }\n")
    out.append("    }\n")
    return "".join(out)


def render_layout(tabs: list[str]) -> str:
    # No default_tab_template and no plugin panes: no tab bar, no status bar.
    return "layout {\n" + "".join(tabs) + "}\n"
