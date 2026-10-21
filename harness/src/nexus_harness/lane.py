"""`nexus-lane run`: the lane main loop.

start tmux session `game` -> run the game under the supervisor (with the fault profile)
-> watch for crash / sanitizer / hang -> report -> restart with the next seed, until
SIGTERM/SIGINT.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from . import config as cfgmod
from .config import ConfigError, LaneEnv
from .detectors import HangWatchdog, SanitizerReport, SanitizerWatcher, classify_exit
from .events import EventSender, Identity, fetch_heat, http_json, make_event
from .tmux import Tmux, TmuxError, frame_hash

log = logging.getLogger("nexus.lane")

# Signals a keystroke can deliver to the game (^C, ^\); reported as agent_error, not crash.
KEYBOARD_SIGNALS = {2, 3}


class ScreenPoster:
    """Posts the newest frame to /lanes/{id}/screen from a background thread, so a slow
    scoreboard never stalls the lane loop. Frames that arrive while a post is in flight
    replace each other (only the latest is sent)."""

    def __init__(self, base_url: str | None, lane_id: str):
        self.url = f"{base_url.rstrip('/')}/lanes/{lane_id}/screen" if base_url else None
        self._frame: dict | None = None
        self._cv = threading.Condition()
        self.posted = 0
        if self.url:
            threading.Thread(target=self._run, name="screen-poster", daemon=True).start()

    def offer(self, frame: dict) -> None:
        if not self.url:
            return
        with self._cv:
            self._frame = frame
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._frame is None:
                    self._cv.wait()
                frame, self._frame = self._frame, None
            try:
                http_json("POST", self.url, json.dumps(frame).encode(), timeout=2.0)
                self.posted += 1
            except (OSError, ValueError) as e:
                log.debug("screen post failed: %s", e)


class Lane:
    def __init__(self, env: LaneEnv):
        self.env = env
        self.out = env.out
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "runs").mkdir(exist_ok=True)
        (self.out / "replay").mkdir(exist_ok=True)
        self.ident = Identity(lane_id=env.lane_id, game=env.game, fault=env.fault,
                              seed=env.seed, model=env.model)
        self.sender = EventSender(env.scoreboard_url, self.out / "events.ndjson")
        self.screens = ScreenPoster(env.scoreboard_url, env.lane_id)
        self.tmux = Tmux(socket=env.tmux_socket, conf=str(env.tmux_conf))
        self.stop = threading.Event()
        self.heat_changed = threading.Event()
        self.end_detail = "stopped"
        self.runs = 0
        self.failures = 0

    # -- events ---------------------------------------------------------------

    def emit(self, type_: str, **fields) -> dict:
        ev = make_event(self.ident, type_, **fields)
        self.sender.send(ev)
        if type_ not in ("heartbeat",):
            log.info("event %s %s", type_, {k: ev[k] for k in ("detail", "signal", "exit_code",
                                                                "stack_hash", "seed") if k in ev})
        return ev

    def failure(self, type_: str, run_dir: Path | None, report: str = "", **fields) -> dict:
        """Emit a failure event with a replay bundle."""
        ev = make_event(self.ident, type_, turn=self.turn(), **fields)
        try:
            ev["replay"] = str(self.bundle(ev, run_dir, report))
        except OSError as e:
            log.warning("replay bundle failed: %s", e)
        self.sender.send(ev)
        self.failures += 1
        log.info("FAILURE %s detail=%s hash=%s", type_, ev.get("detail"), ev.get("stack_hash"))
        return ev

    def bundle(self, ev: dict, run_dir: Path | None, report: str) -> Path:
        """Replay bundle: event, last screen, run spec/status and sanitizer report."""
        try:
            import compression.zstd  # noqa: F401  (Python 3.14+)
            path, mode = self.out / "replay" / f"{ev['event_id']}.tar.zst", "w:zst"
        except ImportError:
            path, mode = self.out / "replay" / f"{ev['event_id']}.tar.gz", "w:gz"
        members = {"event.json": json.dumps(ev, indent=1)}
        try:
            members["screen.txt"] = "\n".join(self.tmux.capture()["lines"])
        except (TmuxError, ValueError):
            pass
        if report:
            members["report.txt"] = report
        for name in ("spec.json", "status.json"):
            if run_dir and (run_dir / name).exists():
                members[name] = (run_dir / name).read_text()
        with tarfile.open(path, mode) as tf:
            for name, text in members.items():
                data = text.encode()
                ti = tarfile.TarInfo(f"{ev['event_id']}/{name}")
                ti.size, ti.mtime = len(data), int(time.time())
                tf.addfile(ti, io.BytesIO(data))
        return path

    def agent_stats(self) -> dict:
        """turn/tokens from the pi extension's stats file ({} when absent)."""
        try:
            st = json.loads(self.env.agent_stats.read_text())
        except (OSError, ValueError):
            return {}
        return {k: int(st[k]) for k in ("turn", "tokens_in", "tokens_out")
                if isinstance(st.get(k), (int, float))}

    def turn(self) -> int:
        """Agent turn (stats.json), else the number of `nexus-lane keys` calls."""
        t = self.agent_stats().get("turn")
        if t is not None:
            return t
        try:
            return self.tmux.send_count()
        except (TmuxError, OSError):
            return 0

    def forward_agent_events(self) -> None:
        """Tail $NEXUS_OUT/agent/events.ndjson and forward agent errors."""
        path = self.env.agent_events
        try:
            with open(path, "rb") as f:
                f.seek(self._agent_off)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1  # only complete lines
        self._agent_off += end
        for line in data[:end].splitlines():
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if not isinstance(a, dict) or a.get("type", "agent_error") != "agent_error":
                continue
            st = self.agent_stats()
            ev = make_event(self.ident, "agent_error", detail=str(a.get("detail", ""))[:300],
                            turn=st.get("turn"), tokens_in=st.get("tokens_in"),
                            tokens_out=st.get("tokens_out"))
            if isinstance(a.get("ts"), str) and a["ts"]:
                ev["ts"] = a["ts"]
            self.sender.send(ev)

    _agent_off = 0

    def _keys(self) -> int:
        try:
            return self.tmux.key_count()
        except (TmuxError, OSError):
            return 0

    # -- heartbeat / heat thread ----------------------------------------------

    def refresh_heat(self) -> None:
        if not self.env.scoreboard_url:
            return
        got = fetch_heat(self.env.scoreboard_url)
        if got is None:
            return
        hid, _ = got
        if hid and hid != self.ident.heat_id:
            old, self.ident.heat_id = self.ident.heat_id, hid
            if old:
                log.info("heat %s -> %s", old, hid)
                self.heat_changed.set()

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(self.env.heartbeat_secs):
            self.refresh_heat()
            self.forward_agent_events()
            st = self.agent_stats()
            self.emit("heartbeat", turn=self.turn(), tokens_in=st.get("tokens_in"),
                      tokens_out=st.get("tokens_out"))

    # -- game runs ------------------------------------------------------------

    def run_spec(self, game: cfgmod.Game, plan: cfgmod.FaultPlan, run_dir: Path) -> dict:
        argv, cwd = game.render(self.ident.seed, self.env.stubs_dir)
        env = {"NEXUS_LANE_ID": self.env.lane_id, "NEXUS_SEED": str(self.ident.seed)}
        env.update(plan.env)
        if os.path.exists(cfgmod.SEED_SHIM):
            env["TORMENT_SEED"] = str(self.ident.seed)
            env["LD_PRELOAD"] = " ".join(filter(None, [cfgmod.SEED_SHIM, os.environ.get("LD_PRELOAD")]))
        if game.sanitizer:
            common = "verify_asan_link_order=0:detect_leaks=0:symbolize=1:abort_on_error=0"
            env["ASAN_OPTIONS"] = ":".join(filter(None, [
                os.environ.get("ASAN_OPTIONS"), common, f"log_path={run_dir}/asan"]))
            env["UBSAN_OPTIONS"] = ":".join(filter(None, [
                os.environ.get("UBSAN_OPTIONS"), "print_stacktrace=1:halt_on_error=0",
                f"log_path={run_dir}/ubsan"]))
            sym = shutil.which("llvm-symbolizer")
            if sym and "ASAN_SYMBOLIZER_PATH" not in os.environ:
                env["ASAN_SYMBOLIZER_PATH"] = sym
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        return {"argv": plan.prefix + argv, "env": env, "cwd": cwd}

    def _pid(self, run_dir: Path) -> int | None:
        try:
            return int((run_dir / "pid").read_text())
        except (OSError, ValueError):
            return None

    def kill_game(self, run_dir: Path, wait: float = 5.0) -> dict | None:
        pid = self._pid(run_dir)
        if pid:
            for sig in (signal.SIGKILL,):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.time() + wait
        while time.time() < deadline:
            st = self._status(run_dir)
            if st:
                return st
            time.sleep(0.05)
        return None

    @staticmethod
    def _status(run_dir: Path) -> dict | None:
        try:
            return json.loads((run_dir / "status.json").read_text())
        except (OSError, ValueError):
            return None

    def report_sanitizers(self, game: cfgmod.Game, reports: list[SanitizerReport],
                          run_dir: Path) -> int:
        fatal = 0
        for r in reports:
            if r.stack_hash and r.stack_hash in game.baseline_hashes:
                log.info("baseline sanitizer report %s (%s) not sent", r.stack_hash, r.detail)
                continue
            self.failure("sanitizer", run_dir, report=r.text, detail=r.detail,
                         stack_hash=r.stack_hash or None)
            fatal += r.fatal
        return fatal

    def play_once(self, game: cfgmod.Game, plan: cfgmod.FaultPlan, k: int) -> str:
        """Run the game once. Returns why it ended: crash|exit|hang|heat|stop|error."""
        self.ident.seed = (self.env.seed + k) % cfgmod.U64
        run_dir = (self.out / "runs" / f"{k:04d}").resolve()
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        spec = self.run_spec(game, plan, run_dir)
        (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
        cols, rows = game.size
        log.info("run %d seed=%d: %s", k, self.ident.seed, " ".join(spec["argv"]))
        self.tmux.start([sys.executable, "-m", "nexus_harness.supervise", str(run_dir)],
                        cols, rows)
        hang_secs = game.hang_secs if game.hang_secs is not None else self.env.hang_secs
        wd = HangWatchdog(hang_secs, self.env.hang_min_keys)
        wd.reset(time.time(), self._keys())
        san = SanitizerWatcher(run_dir) if game.sanitizer else None
        storm = None
        if plan.winch_storm:
            self._storm_stop = False
            storm = threading.Thread(target=self._winch_storm, args=(run_dir, game.size, k),
                                     daemon=True)
            storm.start()
        self.heat_changed.clear()
        arm_at = time.time() + plan.delay_s if plan.delayed_cmds else None
        frame_period = 1.0 / self.env.screen_hz
        next_frame = next_san = 0.0
        last_posted = (None, 0.0)
        why = "exit"
        try:
            while True:
                now = time.time()
                st = self._status(run_dir)
                if st is not None:
                    break
                if self.stop.is_set():
                    self.kill_game(run_dir)
                    return "stop"
                if arm_at is not None and now >= arm_at:
                    arm_at = None
                    self._arm_faults(plan, run_dir, k)
                if self.heat_changed.is_set() and self.env.recycle_on_heat:
                    log.info("recycling for the new heat")
                    self.kill_game(run_dir)
                    return "heat"
                if san and now >= next_san:
                    next_san = now + 1.0
                    self.report_sanitizers(game, san.poll(), run_dir)
                if now >= next_frame:
                    next_frame = now + frame_period
                    try:
                        frame = self.tmux.capture()
                    except (TmuxError, ValueError):
                        frame = None
                    if frame:
                        h = frame_hash(frame)
                        if h != last_posted[0] or now - last_posted[1] >= 5.0:
                            self.screens.offer(frame)
                            last_posted = (h, now)
                        alive = self._pid(run_dir) is not None and self._status(run_dir) is None
                        if wd.observe(now, h, self._keys(), alive):
                            self.failure("hang", run_dir, detail="screen-frozen")
                            log.info("hang: screen frozen %.1fs", wd.frozen_for(now))
                            self.kill_game(run_dir)
                            return "hang"
                time.sleep(0.05)
        finally:
            self._storm_stop = True
            if storm:
                storm.join(2)
                self.tmux.resize(cols, rows)
        # The process exited on its own.
        fatal = self.report_sanitizers(game, san.poll(final=True), run_dir) if san else 0
        if st.get("exec_error"):
            raise ConfigError(f"cannot start game: {st['exec_error']}")
        crash = classify_exit(st, game.ok_exit_codes)
        if crash and not fatal and crash[1] in KEYBOARD_SIGNALS:
            # ^C / ^\ typed by the agent: not a software failure.
            detail, sig, code = crash
            self.failure("agent_error", run_dir, detail=f"game interrupted by {detail}",
                         signal=sig, exit_code=code)
            why = "interrupted"
        elif crash and not fatal and crash[1] is None:
            # Nonzero exit without a signal: the game noticed the fault and quit.
            detail, sig, code = crash
            self.failure("error_exit", run_dir, detail=detail, exit_code=code)
            why = "error_exit"
        elif crash and not fatal:
            detail, sig, code = crash
            self.failure("crash", run_dir, detail=detail, signal=sig, exit_code=code)
            why = "crash"
        elif crash:
            why = "crash"  # already reported as a sanitizer failure
        log.info("run %d ended (%s): %s", k, why, st)
        return why

    _storm_stop = False

    def _arm_faults(self, plan: cfgmod.FaultPlan, run_dir: Path, k: int) -> None:
        """Enable the profile's libfiu points in the running game via fiu-ctrl."""
        pid = self._pid(run_dir)
        if not pid:
            return
        argv = ["fiu-ctrl"]
        for c in plan.delayed_cmds:
            argv += ["-c", c]
        argv.append(str(pid))
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("run %d: arming faults failed: %s", k, e)
            return
        if r.returncode:
            log.warning("run %d: fiu-ctrl exited %d: %s", k, r.returncode, r.stderr.strip())
        else:
            log.info("run %d: faults armed after %.1fs: %s", k, plan.delay_s, "; ".join(plan.delayed_cmds))

    def _winch_storm(self, run_dir: Path, base: tuple[int, int], k: int) -> None:
        for delay, c, r in cfgmod.winch_schedule(self.ident.seed, base):
            if self._storm_stop or self.stop.wait(delay) or self._storm_stop:
                return
            self.tmux.resize(c, r)
            pid = self._pid(run_dir)
            if pid:
                try:
                    os.kill(pid, signal.SIGWINCH)
                except ProcessLookupError:
                    return

    # -- main -----------------------------------------------------------------

    def end(self, detail: str, rc: int) -> int:
        self.forward_agent_events()
        self.emit("lane_end", detail=detail, turn=self.turn())
        if not self.sender.close(10.0):
            log.warning("some events were not delivered (spooled in %s)", self.out / "events.ndjson")
        return rc

    def run(self) -> int:
        try:
            games = cfgmod.load_games(self.env.games_file)
        except (OSError, ValueError, KeyError) as e:
            return self.end(f"cannot load games file {self.env.games_file}: {e}", 2)
        game = games.get(self.env.game)
        if game is None:
            return self.end(f"unknown game {self.env.game!r} (games.toml has: {', '.join(games)})", 2)
        self.ident.game_ver = game.version
        try:  # forward only agent errors written from now on
            self._agent_off = self.env.agent_events.stat().st_size
        except OSError:
            self._agent_off = 0
        self.refresh_heat()
        try:
            plan = cfgmod.fault_plan(self.env.fault, self.env.seed,
                                     faults=cfgmod.load_faults(self.env.faults_file))
            argv, _ = game.render(self.env.seed, self.env.stubs_dir)
            for exe in (plan.prefix[:1] or []) + argv[:1]:
                if not (shutil.which(exe) or (os.sep in exe and os.access(exe, os.X_OK))):
                    raise ConfigError(f"game binary not found: {exe}")
            if not shutil.which("tmux"):
                raise ConfigError("tmux not found")
        except ConfigError as e:
            log.error("%s", e)
            return self.end(str(e), 2)

        self.emit("lane_start", turn=self.turn(),
                  detail=f"{game.name} {game.version} fault={plan.name}")
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        rc, k, fast = 0, 0, 0
        try:
            while not self.stop.is_set():
                t0 = time.time()
                why = self.play_once(game, plan, k)
                self.runs += 1
                k += 1
                if why == "stop" or (self.env.max_runs and self.runs >= self.env.max_runs):
                    if why != "stop":
                        self.end_detail = f"max runs ({self.env.max_runs}) reached"
                    break
                # Throttle a game that dies instantly over and over.
                fast = fast + 1 if time.time() - t0 < 2.0 and why != "heat" else 0
                if fast:
                    self.stop.wait(min(0.5 * 2 ** (fast - 1), 30.0))
        except ConfigError as e:
            log.error("%s", e)
            self.end_detail, rc = str(e), 2
        except TmuxError as e:
            log.error("%s", e)
            self.end_detail, rc = f"tmux error: {e}", 3
        finally:
            self.stop.set()
        rc = self.end(f"{self.end_detail}; runs={self.runs} failures={self.failures}", rc)
        if os.environ.get("NEXUS_KEEP_SESSION", "") in ("", "0"):
            self.tmux.kill()
        return rc


def setup_logging(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stderr), logging.FileHandler(out / "lane.log")):
        h.setFormatter(fmt)
        root.addHandler(h)


def run_lane(env: dict[str, str] | None = None) -> int:
    try:
        lane_env = LaneEnv.from_env(env)
    except ConfigError as e:
        print(f"nexus-lane: {e}", file=sys.stderr)
        return 2
    setup_logging(lane_env.out)
    lane = Lane(lane_env)

    def on_signal(signum, _frame):
        log.info("signal %d: stopping", signum)
        lane.stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return lane.run()
