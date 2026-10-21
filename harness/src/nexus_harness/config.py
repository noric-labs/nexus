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
CRASHTRACE_LIB = "/usr/lib/torment/libtorment_crashtrace.so"  # NEXUS_CRASHTRACE_LIB overrides
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
    faults_file: Path = HARNESS_DIR / "faults.toml"
    tmux_conf: Path = HARNESS_DIR / "tmux.conf"
    tmux_socket: str | None = None
    stubs_dir: Path = HARNESS_DIR / "stubs" / "bin"
    hang_secs: float = 60.0
    hang_min_keys: int = 1
    visual: bool = True  # visual-corruption detector on/off
    blank_secs: float = 2.0  # visual: blank screen this long after a non-blank one
    # visual: hold off the cursor/bad-cell/blank checks this long after a winch-storm
    # resize. 0 (off): the stub games showed no false positives under the storm.
    resize_quiet_secs: float = 0.0
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
            "NEXUS_GAMES_FILE": ("games_file", Path), "NEXUS_FAULTS_FILE": ("faults_file", Path), "NEXUS_TMUX_CONF": ("tmux_conf", Path),
            "NEXUS_TMUX_SOCKET": ("tmux_socket", str), "NEXUS_STUBS_DIR": ("stubs_dir", Path),
            "NEXUS_HANG_SECS": ("hang_secs", float), "NEXUS_HANG_MIN_KEYS": ("hang_min_keys", int),
            "NEXUS_HEARTBEAT_SECS": ("heartbeat_secs", float),
            "NEXUS_VISUAL": ("visual", lambda v: v not in ("0", "false", "no")),
            "NEXUS_BLANK_SECS": ("blank_secs", float),
            "NEXUS_RESIZE_QUIET_SECS": ("resize_quiet_secs", float),
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
    # Visual detector: a regex that the game's status line always matches while playing
    # (Brogue "Depth:", DCSS "Health:"); flagged when it has been missing for status_secs.
    status_regex: str | None = None
    status_secs: float = 30.0
    # Visual conditions that are normal for this game (e.g. dfrotz echoes raw escape
    # bytes for arrow keys, which would read as bad-cells).
    visual_ignore: frozenset[str] = frozenset()

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
            hang_secs=g.get("hang_secs"), status_regex=g.get("status_regex") or None,
            status_secs=float(g.get("status_secs", 30.0)),
            visual_ignore=frozenset(g.get("visual_ignore", [])))
    return games


# ---------------------------------------------------------------------------
# Fault profiles
# ---------------------------------------------------------------------------

@dataclass
class FaultPlan:
    """What a fault profile does to the game: a command prefix, extra env, whether the
    launcher runs the winch-storm schedule, and libfiu commands to send with fiu-ctrl
    `delay_s` seconds after the game starts (empty when faults are on from exec).

    The harness owns LD_PRELOAD (D-014): fault libraries go in `preload` and the launcher
    merges them with the crashtrace and seed shims. When the prefix is a tracer
    (`tracer=True`, strace) the merged LD_PRELOAD is handed to the tracee with strace's
    `-E` instead, so the shims are not loaded into strace itself."""
    name: str
    kind: str = "none"
    prefix: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    winch_storm: bool = False
    delayed_cmds: list[str] = field(default_factory=list)
    delay_s: float = 0.0
    preload: list[str] = field(default_factory=list)
    tracer: bool = False
    # setrlimit fallback when prlimit is missing: {"AS": bytes, ...}, applied by the
    # supervisor in the child before exec (soft = hard, like prlimit --x=N).
    rlimits: dict[str, int] = field(default_factory=dict)
    keystorm_every: int = 0  # inject one hostile sequence every N agent keys
    # Run once before the lane starts; a nonzero exit ends the lane (e.g. ptrace denied).
    preflight: list[str] = field(default_factory=list)


# Used when faults.toml is missing.
DEFAULT_FAULTS = {
    "none": {"kind": "none"},
    "malloc-1pct": {"kind": "libfiu", "points": "libc/mm/*", "probability": 0.01, "delay_s": 5.0},
    "malloc-0.1pct": {"kind": "libfiu", "points": "libc/mm/*", "probability": 0.001, "delay_s": 5.0},
    "winch-storm": {"kind": "winch-storm"},
}

FAKETIME_LIB = "/usr/lib/faketime/libfaketime.so.1"
# prlimit option -> resource.RLIMIT_<name>
RLIMIT_FIELDS = {"as": "AS", "nofile": "NOFILE", "fsize": "FSIZE", "stack": "STACK",
                 "core": "CORE", "cpu": "CPU", "data": "DATA", "nproc": "NPROC",
                 "memlock": "MEMLOCK"}


def load_faults(path: Path | None) -> dict[str, dict]:
    if path is None or not Path(path).exists():
        return dict(DEFAULT_FAULTS)
    with open(path, "rb") as f:
        return tomllib.load(f).get("faults", {})


def _range(spec: dict, key: str, default: tuple[int, int]) -> tuple[int, int]:
    v = spec.get(key, default)
    if isinstance(v, int):
        return v, v
    lo, hi = int(v[0]), int(v[1])
    return min(lo, hi), max(lo, hi)


def strace_when(name: str, seed: int, spec: dict) -> tuple[int, int]:
    """(N, K) for strace's `when=N+K`: fail the Nth matching call and every Kth after it.
    N comes from `first` ([lo, hi] or an int) and K from `step`, both seeded."""
    rng = random.Random(f"strace:{name}:{seed}")
    n = rng.randint(*_range(spec, "first", (100, 1000)))
    k = rng.randint(*_range(spec, "step", (50, 500)))
    return max(1, n), max(1, k)


def fault_plan(name: str, seed: int, which=shutil.which, faults: dict | None = None,
               exists=os.path.exists) -> FaultPlan:
    """Raises ConfigError when the profile is unknown or its tool is missing."""
    faults = DEFAULT_FAULTS if faults is None else faults
    spec = faults.get(name)
    if spec is None:
        raise ConfigError(f"fault {name} not defined (have: {', '.join(faults)})")
    kind = spec.get("kind", "none")
    if kind == "none":
        return FaultPlan(name, kind)
    if kind == "winch-storm":
        return FaultPlan(name, kind, winch_storm=True)
    if kind == "libfiu":
        if not which("fiu-run"):
            raise ConfigError(f"fault {name} skipped: fiu-run not found (install libfiu)")
        points = spec.get("points", "libc/mm/*")
        names = [points] if isinstance(points, str) else list(points)
        prob = float(spec.get("probability", 0.01))
        cmds = [f"enable_random name={n},probability={prob}" for n in names]
        delay = float(spec.get("delay_s", 0) or 0)
        if delay > 0 and not which("fiu-ctrl"):
            raise ConfigError(f"fault {name} skipped: fiu-ctrl not found (install libfiu)")
        # Do what fiu-run does, but set LD_PRELOAD ourselves: fiu-run overwrites it,
        # which would drop the crashtrace and seed preloads.
        libdir = Path(which("fiu-run")).resolve().parent.parent / "lib"
        env = {"FIU_PRNG_SEED": str(seed % 2147483647),
               "FIU_ENABLE": "\n".join(cmds) if delay <= 0 else "",
               "FIU_CTRL_FIFO": os.path.join(os.environ.get("TMPDIR", "/tmp"), "fiu-ctrl")}
        return FaultPlan(name, kind, env=env, preload=[str(libdir / "fiu_run_preload.so"),
                                                       str(libdir / "fiu_posix_preload.so")],
                         delayed_cmds=cmds if delay > 0 else [], delay_s=delay)
    if kind == "strace":
        exe = which("strace")
        if not exe:
            raise ConfigError(f"fault {name} skipped: strace not found (install strace)")
        syscall = str(spec.get("syscall", "write"))
        error = str(spec.get("error", "ENOSPC"))
        trace = spec.get("trace", syscall)
        trace = trace if isinstance(trace, str) else ",".join(trace)
        if syscall not in trace.split(","):
            trace = f"{trace},{syscall}"
        n, k = strace_when(name, seed, spec)
        prefix = ["strace", "-f", "--seccomp-bpf", "-o", "/dev/null", "-e", f"trace={trace}",
                  "-e", f"inject={syscall}:error={error}:when={n}+{k}"]
        return FaultPlan(name, kind, prefix=prefix, tracer=True,
                         preflight=["strace", "-f", "--seccomp-bpf", "-o", "/dev/null",
                                    "-e", f"trace={trace}", "--", "true"])
    if kind == "rlimit":
        limits = {}
        for opt, res in RLIMIT_FIELDS.items():
            if opt in spec:
                limits[opt] = int(spec[opt])
        if not limits:
            raise ConfigError(f"fault {name}: rlimit profile sets no limits")
        if which("prlimit"):
            return FaultPlan(name, kind, prefix=["prlimit"] + [f"--{o}={v}" for o, v in
                                                               limits.items()] + ["--"])
        # util-linux missing: the supervisor calls setrlimit in the child instead.
        return FaultPlan(name, kind, rlimits={RLIMIT_FIELDS[o]: v for o, v in limits.items()})
    if kind == "faketime":
        lib = str(spec.get("lib", FAKETIME_LIB))
        if not exists(lib):
            raise ConfigError(f"fault {name} skipped: {lib} not found (install libfaketime)")
        ft = str(spec.get("faketime", "@2038-01-19 03:14:00 x60"))
        # FAKETIME_DONT_RESET: children of the game keep the warped clock.
        return FaultPlan(name, kind, env={"FAKETIME": ft, "FAKETIME_DONT_RESET": "1"},
                         preload=[lib])
    if kind == "keystorm":
        every = int(spec.get("every", 20))
        if every < 1:
            raise ConfigError(f"fault {name}: keystorm every must be >= 1")
        return FaultPlan(name, kind, keystorm_every=every)
    raise ConfigError(f"fault {name}: unknown kind {kind!r}")


# ---------------------------------------------------------------------------
# keystorm: hostile input bytes
# ---------------------------------------------------------------------------

KEYSTORM_POOL: list[tuple[str, bytes]] = [
    ("csi-truncated", b"\x1b["),
    ("csi-params-truncated", b"\x1b[1;"),
    ("csi-sgr-truncated", b"\x1b[38;5"),
    ("ss3-truncated", b"\x1bO"),
    ("esc-lone", b"\x1b"),
    ("osc-unterminated", b"\x1b]0;"),
    ("csi-8bit", b"\x9b"),
    ("csi-8bit-params", b"\x9b2J"),
    ("nul", b"\x00"),
    ("utf8-bad-continuation", b"\xc3\x28"),
    ("utf8-ff", b"\xff"),
    ("utf8-truncated", b"\xe2\x82"),
    ("utf8-surrogate", b"\xed\xa0\x80"),
    ("utf8-overlong", b"\xc0\xaf"),
    ("mouse-sgr-huge", b"\x1b[<0;9999;9999M"),
    ("mouse-sgr-zero", b"\x1b[<3;0;0m"),
    ("mouse-sgr-negative", b"\x1b[<64;-1;-1M"),
    ("mouse-x10-overflow", b"\x1b[M\xff\xff\xff"),
    ("paste-4k", b""),  # built per draw: bracketed paste with 4 KB of seeded bytes
]


# Bytes the tty (cbreak mode) turns into signals or flow control: ^C ^\ ^Z ^Q ^S ^O ^V.
# A keystorm must not produce them: they would read as agent_error or as a frozen screen.
TTY_SPECIAL = frozenset(b"\x03\x1c\x1a\x11\x13\x0f\x16")


def keystorm_payloads(seed: int):
    """Seeded stream of (name, bytes) hostile key sequences. About a third of the
    short entries get one byte mutated (flipped, duplicated or dropped)."""
    rng = random.Random(f"keystorm:{seed}")
    while True:
        name, data = rng.choice(KEYSTORM_POOL)
        if name == "paste-4k":
            # Nothing from TTY_SPECIAL.
            body = bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz0123456789 \r\n\t\x1b\x7f")
                         for _ in range(4096))
            yield name, b"\x1b[200~" + body + b"\x1b[201~"
            continue
        if len(data) > 1 and rng.random() < 0.33:
            i = rng.randrange(len(data))
            op = rng.randrange(3)
            if op == 0:
                mut = data[:i] + bytes([data[i] ^ (1 << rng.randrange(8))]) + data[i + 1:]
            elif op == 1:
                mut = data[:i] + data[i:i + 1] * 2 + data[i + 1:]
            else:
                mut = data[:i] + data[i + 1:]
            if not any(b in TTY_SPECIAL for b in mut):
                data, name = mut, name + "~"
        yield name, data


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
