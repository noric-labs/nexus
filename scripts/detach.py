#!/usr/bin/env python3
"""Start a long-running job fully detached from the caller, then return at once.

    python3 scripts/detach.py --log FILE [--cwd DIR] -- CMD [ARGS...]

`min session exec` returns only when nothing holds its output channel open. A plain
`setsid nohup cmd >log 2>&1 </dev/null &` still passes every other inherited file
descriptor (fd 3 and up) to the job, and one of those can keep exec waiting (D-031/D-032).

This launcher double-forks, starts a new session, points stdin at /dev/null and
stdout/stderr at the log, closes every other fd, and execs the command. The parent
exits once the grandchild has been spawned.
"""
import argparse
import os
import sys


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True)
    p.add_argument("--cwd")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        p.error("missing command")
    if os.fork():
        return 0  # caller returns immediately
    os.setsid()
    if os.fork():
        os._exit(0)  # the session leader exits; the job can never reacquire a tty
    if a.cwd:
        os.chdir(a.cwd)
    os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
    null = os.open(os.devnull, os.O_RDONLY)
    log = os.open(a.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(null, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.closerange(3, os.sysconf("SC_OPEN_MAX") if hasattr(os, "sysconf") else 65536)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        os.write(2, f"detach: cannot exec {cmd[0]}: {e}\n".encode())
        os._exit(127)


if __name__ == "__main__":
    sys.exit(main())
