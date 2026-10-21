"""Thin tmux wrapper. The lane's game lives in session `game`; NEXUS_TMUX_SOCKET (a
`tmux -L` socket name) isolates several lanes on one host, e.g. in tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time

SESSION = "game"
KEYS_OPTION = "@nexus_keys"  # keys sent by the agent (read by the hang watchdog)
SENDS_OPTION = "@nexus_sends"  # `nexus-lane keys` calls (fallback turn counter)


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, socket: str | None = None, conf: str | None = None,
                 session: str | None = None):
        self.socket = socket if socket is not None else os.environ.get("NEXUS_TMUX_SOCKET")
        self.conf = conf
        self.session = session or os.environ.get("NEXUS_TMUX_TARGET") or SESSION

    def _base(self) -> list[str]:
        cmd = ["tmux"]
        if self.socket:
            cmd += ["-L", self.socket]
        if self.conf:
            cmd += ["-f", self.conf]
        return cmd

    def run(self, *args: str, check: bool = True) -> str:
        p = subprocess.run(self._base() + list(args), capture_output=True, text=True)
        if check and p.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout

    @property
    def target(self) -> str:
        return f"={self.session}:"

    def has_session(self) -> bool:
        return subprocess.run(self._base() + ["has-session", "-t", self.session],
                              capture_output=True).returncode == 0

    def start(self, argv: list[str], cols: int, rows: int, cwd: str | None = None) -> None:
        """Create the session running argv, or respawn its pane if it already exists."""
        cmdline = [*argv]
        extra = ["-c", cwd] if cwd else []
        if self.has_session():
            self.resize(cols, rows)
            self.run("respawn-pane", "-k", "-t", self.target, *extra, *cmdline)
        else:
            self.run("new-session", "-d", "-s", self.session, "-x", str(cols), "-y", str(rows),
                     *extra, *cmdline)
        for opt, val in (("window-size", "manual"), ("remain-on-exit", "on"), ("status", "off")):
            self.run("set-option", "-t", self.session, opt, val, check=False)
        self.run("set-window-option", "-t", self.target, "remain-on-exit", "on", check=False)
        self.resize(cols, rows)

    def resize(self, cols: int, rows: int) -> None:
        self.run("resize-window", "-t", self.target, "-x", str(cols), "-y", str(rows), check=False)

    def kill(self) -> None:
        self.run("kill-session", "-t", self.session, check=False)

    def send_keys(self, keys: list[str], literal: bool = False) -> None:
        """Raw tmux send-keys (tmux key-name semantics, or -l literal)."""
        args = ["send-keys", "-t", self.target] + (["-l"] if literal else [])
        self.run(*args, "--", *keys)
        self.bump(KEYS_OPTION, len(keys))
        self.bump(SENDS_OPTION, 1)

    def send_notation(self, text: str) -> int:
        """Send a key string in the agent notation (see keys.py) as one tmux command
        sequence. Returns the number of keys."""
        from .keys import parse_keys
        segs, count = parse_keys(text)
        args: list[str] = []
        for kind, val in segs:
            if args:
                args.append(";")
            args += ["send-keys", "-t", self.target] + (["-l", "--", _esc(val)] if kind == "lit" else [val])
        if args:
            self.run(*args)
        self.bump(KEYS_OPTION, count)
        self.bump(SENDS_OPTION, 1)
        return count

    def bump(self, option: str, by: int = 1) -> int:
        n = self.counter(option) + by
        self.run("set-option", "-t", self.session, option, str(n), check=False)
        return n

    def counter(self, option: str) -> int:
        out = self.run("show-options", "-v", "-t", self.session, option, check=False)
        try:
            return int(out.strip() or 0)
        except ValueError:
            return 0

    def key_count(self) -> int:
        return self.counter(KEYS_OPTION)

    def send_count(self) -> int:
        return self.counter(SENDS_OPTION)

    def capture(self) -> dict:
        """One screen frame in the /lanes/{id}/screen shape."""
        info = self.run("display-message", "-p", "-t", self.target,
                        "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y}").split()
        cols, rows, cx, cy = (int(x) for x in info[:4])
        text = self.run("capture-pane", "-p", "-t", self.target)
        lines = text.split("\n")
        if lines and lines[-1] == "" and len(lines) > rows:
            lines.pop()
        lines = (lines + [""] * rows)[:rows]
        return {"ts": _ts(), "cols": cols, "rows": rows, "cursor": [cx, cy], "lines": lines}


def frame_hash(frame: dict) -> str:
    h = hashlib.sha1()
    h.update(f"{frame['cols']}x{frame['rows']}\n".encode())
    h.update("\n".join(frame["lines"]).encode())
    return h.hexdigest()


def _ts() -> str:
    from .events import utc_ts
    return utc_ts(time.time())


def _esc(lit: str) -> str:
    """tmux treats an argument ending in ";" as a command separator; escape it."""
    return lit[:-1] + "\\;" if lit.endswith(";") else lit
