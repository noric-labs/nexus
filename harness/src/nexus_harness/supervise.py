"""Supervisor that runs inside the tmux pane: forks the game, waits for it and records
the real exit status / signal to `<run_dir>/status.json`.

    python -m nexus_harness.supervise <run_dir>

`<run_dir>/spec.json` holds {"argv": [...], "env": {...}, "cwd": "..."}. The child stays
in the pane's foreground process group, so tmux key input and SIGWINCH reach it. The
supervisor itself ignores the terminal's job-control signals so that a stray ^C or ^Z sent
by the agent kills or stops only the game.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import sys
import time
from pathlib import Path


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m nexus_harness.supervise <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    spec = json.loads((run_dir / "spec.json").read_text())
    env = dict(os.environ)
    env.update(spec.get("env") or {})
    for k in spec.get("unset") or []:
        env.pop(k, None)
    cwd = spec.get("cwd")
    started = time.time()
    pid = os.fork()
    if pid == 0:
        try:
            # Python ignores SIGPIPE and SIGXFSZ, and an ignored signal stays ignored across
            # exec: restore the defaults so the game can die from them (rlimit --fsize).
            for s in (signal.SIGPIPE, signal.SIGXFSZ):
                signal.signal(s, signal.SIG_DFL)
            if cwd:
                os.chdir(cwd)
            # Fault profile rlimit-tight without prlimit: soft = hard, like prlimit --x=N.
            for name, val in (spec.get("rlimits") or {}).items():
                resource.setrlimit(getattr(resource, f"RLIMIT_{name}"), (int(val), int(val)))
            os.execvpe(spec["argv"][0], spec["argv"], env)
        except (OSError, ValueError, AttributeError) as e:
            try:
                (run_dir / "exec_error").write_text(f"{spec['argv'][0]}: {e}\n")
            finally:
                os._exit(127)
    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU,
              signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)
    (run_dir / "pid").write_text(f"{pid}\n")
    while True:
        try:
            _, status, rusage = os.wait4(pid, 0)
            break
        except InterruptedError:
            continue
    rec = {"pid": pid, "started": started, "ended": time.time(),
           "exit_code": None, "signal": None, "core_dumped": False,
           "maxrss_kb": rusage.ru_maxrss}
    if os.WIFSIGNALED(status):
        rec["signal"] = os.WTERMSIG(status)
        rec["core_dumped"] = os.WCOREDUMP(status)
    else:
        rec["exit_code"] = os.WEXITSTATUS(status)
    if (run_dir / "exec_error").exists():
        rec["exec_error"] = (run_dir / "exec_error").read_text().strip()
    _write_json(run_dir / "status.json", rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
