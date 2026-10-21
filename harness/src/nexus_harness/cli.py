"""nexus-lane CLI: `run` (the lane launcher), `keys` and `screen` (agent-side helpers
around tmux send-keys / capture-pane), `build-stubs` (local stand-in games)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import HARNESS_DIR
from .tmux import Tmux, TmuxError


def cmd_keys(a) -> int:
    t = Tmux(socket=a.socket)
    try:
        if a.raw or a.literal:
            t.send_keys(a.keys, literal=a.literal)
        else:
            t.send_notation("".join(a.keys))
    except TmuxError as e:
        print(f"nexus-lane keys: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_screen(a) -> int:
    t = Tmux(socket=a.socket)
    try:
        frame = t.capture()
    except TmuxError as e:
        print(f"nexus-lane screen: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(frame))
    else:
        print("\n".join(frame["lines"]))
    return 0


STUBS = {"crasher": False, "hang": False, "asan": True, "glitch": False}


def build_stubs(out: Path | None = None, src: Path | None = None) -> Path:
    src = src or HARNESS_DIR / "stubs"
    out = Path(out or os.environ.get("NEXUS_STUBS_DIR") or src / "bin")
    out.mkdir(parents=True, exist_ok=True)
    clang = shutil.which("clang")
    cc = shutil.which(os.environ.get("CC", "cc")) or clang or shutil.which("gcc")
    for name, sanitized in STUBS.items():
        if sanitized:
            if not clang:
                print("clang not found: skipping asan stub", file=sys.stderr)
                continue
            cmd = [clang, "-g", "-O0", "-fno-omit-frame-pointer", "-fsanitize=address,undefined"]
        else:
            cmd = [cc, "-g", "-O0"]
        subprocess.run(cmd + ["-o", str(out / name), str(src / f"{name}.c")], check=True)
    return out


def cmd_build_stubs(a) -> int:
    print(build_stubs(a.out))
    return 0


def cmd_run(a) -> int:
    from .lane import run_lane
    return run_lane()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nexus-lane", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the lane (configured by NEXUS_* env vars)")
    r.set_defaults(fn=cmd_run)
    k = sub.add_parser("keys", help="send keys to the game in the agent notation: plain text is "
                       "typed, <Enter> <Esc> <Tab> <BSpace> <Up> <C-x> <M-x> <F1> <lt> ... are keys "
                       "(arguments are concatenated)")
    k.add_argument("--raw", action="store_true", help="arguments are tmux send-keys key names")
    k.add_argument("-l", "--literal", action="store_true", help="type every argument literally")
    k.add_argument("--socket", help="tmux -L socket (default $NEXUS_TMUX_SOCKET)")
    k.add_argument("keys", nargs="+")
    k.set_defaults(fn=cmd_keys)
    s = sub.add_parser("screen", help="print the game screen")
    s.add_argument("--json", action="store_true", help="print a screen frame as JSON")
    s.add_argument("--socket", help="tmux -L socket (default $NEXUS_TMUX_SOCKET)")
    s.set_defaults(fn=cmd_screen)
    b = sub.add_parser("build-stubs", help="compile the local stand-in games")
    b.add_argument("--out", type=Path, help="output dir (default stubs/bin or $NEXUS_STUBS_DIR)")
    b.set_defaults(fn=cmd_build_stubs)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
