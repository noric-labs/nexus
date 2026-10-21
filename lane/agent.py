#!/usr/bin/env python3
"""Lane agent driver: keeps one pi RPC process playing the game.

Started by lane/agent.sh, which sets up the environment. This script:
  - starts `pi --mode rpc --no-builtin-tools -e lane/game.ts ...` with the lane's model;
  - sends the per-game goal prompt, and whenever a run settles sends a short
    "continue" prompt, so the agent plays indefinitely;
  - backs off on provider errors (429s, broker 5xx) and restarts pi if it exits;
  - appends agent errors to $NEXUS_OUT/agent/events.ndjson for the harness;
  - stops when game.ts reports a cap (stats.json "capped") or NEXUS_AGENT_SECONDS elapses.

Token and turn counts are written by game.ts to $NEXUS_OUT/agent/stats.json.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

LANE = os.path.dirname(os.path.abspath(__file__))
env = os.environ
OUT = env.get("NEXUS_OUT", "/home/out")
AGENT_DIR = os.path.join(OUT, "agent")
STATS = env.get("NEXUS_AGENT_STATS", os.path.join(AGENT_DIR, "stats.json"))
EVENTS = os.path.join(AGENT_DIR, "events.ndjson")
GAME = env.get("NEXUS_GAME", "")
MODEL = env.get("NEXUS_MODEL", "@cf/qwen/qwen3-30b-a3b-fp8")
PROVIDER = env.get("NEXUS_PROVIDER", "nexus")
THINKING = env.get("NEXUS_THINKING", "low")
# explore: try odd commands and menus (bug hunting, prompts/system.md); win: play for
# progress and score (prompts/system-win.md). host-up --no-faults selects win.
STYLE = "win" if env.get("NEXUS_PLAY_STYLE", "explore") == "win" else "explore"
DEADLINE = float(env.get("NEXUS_AGENT_SECONDS", "0") or 0)
PI = env.get("NEXUS_PI_BIN", "pi")
CONTINUE_MSG = env.get(
    "NEXUS_CONTINUE_PROMPT",
    "Keep playing. Look at the latest screen and act with send_keys (batch several keys).",
)
RPC_LOG = env.get("NEXUS_RPC_LOG", "")  # path to tee pi's stdout records, empty = off

start = time.time()
stop = threading.Event()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(msg):
    print(f"{now()} agent: {msg}", file=sys.stderr, flush=True)


def event(kind, detail):
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(EVENTS, "a") as f:
        f.write(json.dumps({"ts": now(), "type": kind, "lane_id": env.get("NEXUS_LANE_ID", ""),
                            "model": MODEL, "detail": str(detail)[:500]}) + "\n")


def capped():
    try:
        with open(STATS) as f:
            return json.load(f).get("capped") or ""
    except (OSError, ValueError):
        return ""


def read_prompt(name):
    p = os.path.join(LANE, "prompts", f"{name}.md")
    return open(p).read().strip() if os.path.exists(p) else None


def goal_prompt():
    override = env.get("NEXUS_GOAL_PROMPT")
    if override:
        return override
    # Release builds share the game's prompt: brogue-rel -> brogue.md.
    base = GAME[:-4] if GAME.endswith("-rel") else GAME
    return read_prompt(GAME) or read_prompt(base) or read_prompt("default")


def pi_cmd():
    cmd = [PI, "--mode", "rpc", "--offline",
           "--no-builtin-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
           "--no-context-files", "--no-themes",
           "-e", os.path.join(LANE, "game.ts"),
           "--system-prompt", os.path.join(LANE, "prompts", "system-win.md" if STYLE == "win" else "system.md"),
           "--provider", PROVIDER, "--model", MODEL, "--thinking", THINKING]
    if env.get("NEXUS_PI_SESSION_DIR"):
        cmd += ["--session-dir", env["NEXUS_PI_SESSION_DIR"]]
    else:
        cmd += ["--no-session"]
    return cmd


class Pi:
    def __init__(self):
        os.makedirs(AGENT_DIR, exist_ok=True)
        cwd = os.path.join(AGENT_DIR, "cwd")
        os.makedirs(cwd, exist_ok=True)
        self.stderr = open(os.path.join(AGENT_DIR, "pi.stderr.log"), "ab")
        self.rpclog = open(RPC_LOG, "a") if RPC_LOG else None
        self.p = subprocess.Popen(pi_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.stderr, cwd=cwd, start_new_session=True)
        self.settled = threading.Event()
        self.errors_in_run = []
        self.lock = threading.Lock()
        self.n = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, obj):
        with self.lock:
            self.n += 1
            obj.setdefault("id", f"d-{self.n}")
            try:
                self.p.stdin.write(json.dumps(obj).encode() + b"\n")
                self.p.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def prompt(self, text):
        self.settled.clear()
        self.errors_in_run = []
        self.send({"type": "prompt", "message": text})

    def _reader(self):
        for line in iter(self.p.stdout.readline, b""):
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            if self.rpclog:
                self.rpclog.write(line.decode("utf-8", "replace") + "\n")
                self.rpclog.flush()
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("type")
            if t == "agent_settled":
                self.settled.set()
            elif t == "message_end":
                m = rec.get("message") or {}
                if m.get("role") == "assistant" and m.get("stopReason") == "error":
                    self.errors_in_run.append(m.get("errorMessage") or "provider error")
            elif t == "response" and not rec.get("success"):
                self.errors_in_run.append(f"rpc {rec.get('command')}: {rec.get('error')}")
                if rec.get("command") == "prompt":
                    self.settled.set()
            elif t == "extension_ui_request" and rec.get("method") in ("select", "confirm", "input", "editor"):
                self.send({"type": "extension_ui_response", "id": rec.get("id"), "cancelled": True})
        self.settled.set()

    def alive(self):
        return self.p.poll() is None

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.p.pid, signal.SIGTERM)
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL)


STALL_SECS = float(env.get("NEXUS_STALL_SECS", "60") or 0)


def stats_age():
    """Seconds since game.ts last wrote stats (it does after every turn and action)."""
    try:
        return time.time() - os.path.getmtime(STATS)
    except OSError:
        return 0.0


def time_left():
    return DEADLINE <= 0 or time.time() - start < DEADLINE


def main():
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    goal = goal_prompt()
    backoff = 0.0
    restarts = 0
    log(f"lane={env.get('NEXUS_LANE_ID', '')} game={GAME} style={STYLE} model={PROVIDER}/{MODEL} stats={STATS}")
    while not stop.is_set() and time_left() and not capped():
        pi = Pi()
        log(f"pi started pid={pi.p.pid} (restart #{restarts})")
        first = True
        while not stop.is_set() and time_left() and pi.alive() and not capped():
            pi.prompt(goal if first else CONTINUE_MSG)
            first = False
            run_start = time.time()
            stalled = False
            while not pi.settled.wait(1.0):
                if stop.is_set() or not time_left():
                    break
                # A provider request can hang with no error; don't let the lane freeze.
                if STALL_SECS and min(stats_age(), time.time() - run_start) > STALL_SECS:
                    stalled = True
                    event("agent_error", f"run stalled > {STALL_SECS:.0f}s; aborting")
                    log(f"run stalled > {STALL_SECS:.0f}s; aborting")
                    pi.send({"type": "abort"})
                    if not pi.settled.wait(10.0):
                        log("abort did not settle; restarting pi")
                        pi.close()
                    break
            if stalled:
                continue
            if stop.is_set() or not time_left():
                break
            if pi.errors_in_run:
                detail = pi.errors_in_run[-1]
                event("agent_error", detail)
                backoff = min(60.0, max(2.0, backoff * 2))
                log(f"run error: {detail[:200]}; backing off {backoff:.0f}s")
                stop.wait(backoff)
            else:
                backoff = 0.0
                # The model replied without a tool call; nudge it after a short beat.
                stop.wait(1.0)
        pi.close()
        if stop.is_set() or not time_left() or capped():
            break
        restarts += 1
        event("agent_error", f"pi exited with code {pi.p.returncode}; restarting")
        backoff = min(60.0, max(2.0, backoff * 2))
        log(f"pi exited rc={pi.p.returncode}; restarting in {backoff:.0f}s")
        stop.wait(backoff)
    c = capped()
    log(f"stopping: {'cap ' + c if c else 'deadline/signal'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
