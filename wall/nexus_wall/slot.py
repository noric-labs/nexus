"""wall-slot: run the command in a slot file, restart it when the file changes.

Each Hot-tab pane runs `wall-slot <dir>/slot-NN`. wall-pager rewrites the file
(a shell command, or empty) to point the slot at another lane, so the Hot tab is
never re-laid-out: only the slot whose lane changed re-attaches.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time


def read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def stop(p: subprocess.Popen | None) -> None:
    if p is None or p.poll() is not None:
        return
    # The child stays in our process group so it keeps the pane's tty in the
    # foreground (tmux attach needs that). It is started with `exec`, so p is the
    # attach client itself (keep --cmd a single command).
    p.terminate()
    try:
        p.wait(2)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    sys.stdout.write("\033[0m\033[H\033[2J")
    sys.stdout.flush()


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    # Keep zellij's replies to terminal queries off the screen (see common.NO_ECHO).
    subprocess.run(["stty", "-echo", "-icanon"], stderr=subprocess.DEVNULL)
    if not args:
        sys.exit("usage: wall-slot SLOT_FILE [RETRY_SECONDS]")
    path, retry = args[0], float(args[1]) if len(args) > 1 else 3.0
    cmd, child, next_start = None, None, 0.0
    signal.signal(signal.SIGTERM, lambda *_: (stop(child), sys.exit(0)))
    try:
        while True:
            want = read(path)
            if want != cmd:
                stop(child)
                child, cmd, next_start = None, want, 0.0
                if not cmd:
                    sys.stdout.write("\033[2m(empty hot slot)\033[0m\r\n")
                    sys.stdout.flush()
            if cmd and (child is None or child.poll() is not None) and time.monotonic() >= next_start:
                if child is not None:  # it exited: lane gone or recycling
                    sys.stdout.write(f"\r\n\033[2m[detached, retrying in {retry:g}s]\033[0m\r\n")
                    sys.stdout.flush()
                    child, next_start = None, time.monotonic() + retry
                    continue
                child = subprocess.Popen(["sh", "-c", "exec " + cmd])
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop(child)


if __name__ == "__main__":
    main()
