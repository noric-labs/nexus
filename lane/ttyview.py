#!/usr/bin/env python3
"""Read-only view of the lane's tmux session `game`, for the zellij wall.

`min session exec` is non-interactive (docs: reference/cli-min#session-exec): it
relays stdout/stderr but gives the command no terminal, and `tmux attach` refuses
to run without one ("open terminal failed: not a terminal"). This helper makes
the terminal inside the session: it opens a pty sized to the game window, runs
`tmux attach -f ignore-size -t game` on it, and copies the pty's output to stdout.
The wall pane renders that escape stream as if it were attached directly.

stdin is ignored, so the view is read-only in practice. It does not attach with `-r`:
tmux 3.7 resolves a bare `send-keys` to the session's client, and when that is a
read-only client it refuses with "client is read-only", which blocked every agent key
while the wall was open (D-036). `ignore-size` keeps the view from resizing the game. Runs until tmux detaches or exits,
which ends the exec; the wall's retry loop re-attaches.

    min session exec l-001 'exec python3 /workbench/lane/ttyview.py'

Env: NEXUS_TMUX_SOCKET (a tmux -L name), NEXUS_TMUX_TARGET (default `game`),
TMUX_TMPDIR (default $HOME/.nexus/tmux, the same default as scripts/lane.sh),
NEXUS_VIEW_TERM (default xterm-256color), NEXUS_VIEW_WAIT (seconds to wait for
the session to appear, default 30).
"""
import fcntl
import os
import pty
import struct
import subprocess
import sys
import termios
import time


def main() -> int:
    home = os.environ.get("HOME", "/home")
    os.environ.setdefault("TMUX_TMPDIR", os.path.join(home, ".nexus", "tmux"))
    os.environ["TERM"] = os.environ.get("NEXUS_VIEW_TERM", "xterm-256color")
    target = os.environ.get("NEXUS_TMUX_TARGET", "game")
    tmux = ["tmux"]
    if os.environ.get("NEXUS_TMUX_SOCKET"):
        tmux += ["-L", os.environ["NEXUS_TMUX_SOCKET"]]

    deadline = time.monotonic() + float(os.environ.get("NEXUS_VIEW_WAIT", "30"))
    size = None
    while time.monotonic() < deadline:
        r = subprocess.run(tmux + ["display-message", "-p", "-t", target,
                                   "#{window_width} #{window_height}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.split():
            size = [int(x) for x in r.stdout.split()]
            break
        time.sleep(1)
    if not size:
        sys.stderr.write(f"ttyview: no tmux session {target!r} (TMUX_TMPDIR={os.environ['TMUX_TMPDIR']})\r\n")
        return 1
    cols, rows = size

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.execvp(tmux[0], tmux + ["attach-session", "-f", "ignore-size", "-t", target])
    out = sys.stdout.buffer
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:  # EIO: the child closed the pty
            break
        if not data:
            break
        try:
            out.write(data)
            out.flush()
        except BrokenPipeError:
            os.kill(pid, 15)
            break
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) if status else 0


if __name__ == "__main__":
    sys.exit(main())
