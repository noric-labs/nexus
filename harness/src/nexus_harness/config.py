"""Lane configuration: NEXUS_* environment, games.toml and fault profiles."""

from __future__ import annotations

import os
import random
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parents[2]
SEED_SHIM = "/usr/lib/torment/libtorment_seed.so"
U64 = 1 << 64


class ConfigError(Exception):
    """A configuration problem that ends the lane with a lane_end detail."""


@dataclass
class LaneEnv:
    lane_id: str
    game: str
    fault: str = "none"
    seed: int = 1
    model: str = ""
    scoreboard_url: str | None = None
    out: Path = Path("/home/out")
    # Extensions (not in CONTRACTS):
    games_file: Path = HARNESS_DIR / "games.toml"
    tmux_conf: Path = HARNESS_DIR / "tmux.conf"
    tmux_socket: str | None = None
    stubs_dir: Path = HARNESS_DIR / "stubs" / "bin"
    hang_secs: float = 60.0
    hang_min_keys: int = 1
    heartbeat_secs: float = 5.0
    screen_hz: float = 2.0
    recycle_on_heat: bool = True
    max_runs: int = 0  # 0 = unlimited
    agent_stats: Path | None = None  # default $NEXUS_OUT/agent/stats.json
    agent_events: Path | None = None  # default $NEXUS_OUT/agent/events.ndjson

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LaneEnv":
        e = os.environ if env is None else env
        missing = [k for k in ("NEXUS_LANE_ID", "NEXUS_GAME") if not e.get(k)]
        if missing:
            raise ConfigError(f"missing env: {', '.join(missing)}")
        try:
            seed = int(e.get("NEXUS_SEED", "1")) % U64
        except ValueError:
            raise ConfigError(f"NEXUS_SEED is not an integer: {e.get('NEXUS_SEED')!r}")
        d = cls(lane_id=e["NEXUS_LANE_ID"], game=e["NEXUS_GAME"],
                fault=e.get("NEXUS_FAULT") or "none", seed=seed,
                model=e.get("NEXUS_MODEL", ""),
                scoreboard_url=e.get("NEXUS_SCOREBOARD_URL") or None,
                out=Path(e.get("NEXUS_OUT") or "/home/out"))
        opt = {
            "NEXUS_GAMES_FILE": ("games_file", Path), "NEXUS_TMUX_CONF": ("tmux_conf", Path),
            "NEXUS_TMUX_SOCKET": ("tmux_socket", str), "NEXUS_STUBS_DIR": ("stubs_dir", Path),
            "NEXUS_HANG_SECS": ("hang_secs", float), "NEXUS_HANG_MIN_KEYS": ("hang_min_keys", int),
            "NEXUS_HEARTBEAT_SECS": ("heartbeat_secs", float),
            "NEXUS_SCREEN_HZ": ("screen_hz", float), "NEXUS_AGENT_STATS": ("agent_stats", Path),
            "NEXUS_AGENT_EVENTS": ("agent_events", Path), "NEXUS_MAX_RUNS": ("max_runs", int),
            "NEXUS_RECYCLE_ON_HEAT": ("recycle_on_heat", lambda v: v not in ("0", "false", "no")),
        }
        for var, (attr, conv) in opt.items():
            if e.get(var):
                setattr(d, attr, conv(e[var]))
        d.agent_stats = d.agent_stats or d.out / "agent" / "stats.json"
        d.agent_events = d.agent_events or d.out / "agent" / "events.ndjson"
        d.screen_hz = min(d.screen_hz, 2.0)  # contract: at most 2 frames/s
        return d


@dataclass
class Game:
    name: str
    command: list[str]
    version: str = ""
    size: tuple[int, int] = (80, 24)
    cwd: str | None = None
    sanitizer: bool = False
    baseline_hashes: set[str] = field(default_factory=set)
    ok_exit_codes: tuple[int, ...] = (0,)
    hang_secs: float | None = None

    def render(self, seed: int, stubs_dir: Path) -> tuple[list[str], str | None]:
        vars_ = {"seed": str(seed), "seed32": str(seed32(seed)), "stubs": str(stubs_dir),
                 "home": os.environ.get("HOME", "/root")}
        argv = [a.format(**vars_) for a in self.command]
        cwd = self.cwd.format(**vars_) if self.cwd else None
        return argv, cwd


def seed32(seed: int) -> int:
    """frotz -s takes an int where 0 means "use the clock"."""
    return seed % 2147483646 + 1


def load_games(path: Path) -> dict[str, Game]:
    with open(path, "rb") as f:
        raw = tomllib.load(f).get("games", {})
    games = {}
    for name, g in raw.items():
        games[name] = Game(
            name=name, command=list(g["command"]), version=str(g.get("version", "")),
            size=tuple(g.get("size", (80, 24))), cwd=g.get("cwd"),
            sanitizer=bool(g.get("sanitizer", False)),
            baseline_hashes=set(g.get("baseline_hashes", [])),
            ok_exit_codes=tuple(g.get("ok_exit_codes", (0,))),
            hang_secs=g.get("hang_secs"))
    return games


# ---------------------------------------------------------------------------
# Fault profiles
# ---------------------------------------------------------------------------

@dataclass
class FaultPlan:
    """What a fault profile does to the game: a command prefix, extra env, and whether
    the launcher runs the winch-storm schedule."""
    name: str
    prefix: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    winch_storm: bool = False


FAULTS = ("none", "malloc-1pct", "malloc-0.1pct", "winch-storm")


def fault_plan(name: str, seed: int, which=shutil.which) -> FaultPlan:
    """Raises ConfigError when the profile is unknown or its tool is missing."""
    if name == "none":
        return FaultPlan(name)
    if name in ("malloc-1pct", "malloc-0.1pct"):
        p = {"malloc-1pct": "0.01", "malloc-0.1pct": "0.001"}[name]
        if not which("fiu-run"):
            raise ConfigError(f"fault {name} skipped: fiu-run not found (install libfiu)")
        return FaultPlan(name,
                         prefix=["fiu-run", "-x", "-c", f"enable_random name=libc/mm/*,probability={p}"],
                         env={"FIU_PRNG_SEED": str(seed % 2147483647)})
    if name == "winch-storm":
        return FaultPlan(name, winch_storm=True)
    raise ConfigError(f"fault {name} not supported by this launcher (have: {', '.join(FAULTS)})")


def winch_schedule(seed: int, base: tuple[int, int], n: int | None = None):
    """Seeded winch-storm schedule: yields (delay_s, cols, rows). Every 50-500 ms the
    window is resized to a random size near the base; every other step returns to
    the base size so the game spends time at its real geometry."""
    rng = random.Random(f"winch-storm:{seed}")
    cols, rows = base
    i = 0
    while n is None or i < n:
        delay = rng.uniform(0.05, 0.5)
        if i % 2 == 0:
            c = max(20, cols + rng.randint(-cols // 2, cols // 3))
            r = max(5, rows + rng.randint(-rows // 2, rows // 3))
        else:
            c, r = cols, rows
        yield delay, c, r
        i += 1
