"""Fault plan construction for every kind (no tools needed: `which`/`exists` are faked),
plus the parts that run locally when the tool is present."""

import json
import os
import resource
import signal
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from nexus_harness import config as c
from nexus_harness.lane import Lane, descendants

FAULTS = c.load_faults(c.HARNESS_DIR / "faults.toml")


def fake_which(*present):
    return lambda exe: f"/usr/bin/{exe}" if exe in present else None


def test_all_contract_profiles_are_defined():
    for name in ("none", "malloc-1pct", "malloc-0.1pct", "io-save-5pct", "strace-enospc-save",
                 "strace-eintr-read", "winch-storm", "rlimit-tight", "time-warp", "keystorm"):
        assert name in FAULTS, name


# -- strace -------------------------------------------------------------------

def test_strace_enospc_plan():
    p = c.fault_plan("strace-enospc-save", 12345, which=fake_which("strace"), faults=FAULTS)
    assert p.kind == "strace" and p.tracer and not p.preload
    assert p.prefix[:5] == ["strace", "-f", "--seccomp-bpf", "-o", "/dev/null"]
    assert p.prefix[5:7] == ["-e", "trace=write,openat"]
    assert p.prefix[7] == "-e"
    head, when = p.prefix[8].split(":when=")
    assert head == "inject=write:error=ENOSPC"
    n, k = map(int, when.split("+"))
    assert 300 <= n <= 3000 and 20 <= k <= 200
    assert p.preflight[0] == "strace" and p.preflight[-2:] == ["--", "true"]


def test_strace_eintr_plan_and_seeding():
    def when(seed):
        p = c.fault_plan("strace-eintr-read", seed, which=fake_which("strace"), faults=FAULTS)
        assert p.prefix[6] == "trace=read"
        assert p.prefix[8].startswith("inject=read:error=EINTR:when=")
        return p.prefix[8].rsplit("=", 1)[1]
    assert when(1) == when(1)  # deterministic per seed
    assert all(w.endswith("+7") for w in map(when, range(20)))
    assert len({when(s) for s in range(20)}) > 5  # N varies with the seed
    assert all(20 <= int(when(s).split("+")[0]) <= 200 for s in range(20))


def test_strace_syscall_is_always_traced():
    faults = {"x": {"kind": "strace", "syscall": "openat", "error": "EMFILE", "trace": "read",
                    "first": 5, "step": 3}}
    p = c.fault_plan("x", 1, which=fake_which("strace"), faults=faults)
    assert "trace=read,openat" in p.prefix and "inject=openat:error=EMFILE:when=5+3" in p.prefix


def test_strace_missing_is_config_error():
    with pytest.raises(c.ConfigError, match="strace not found"):
        c.fault_plan("strace-enospc-save", 1, which=fake_which(), faults=FAULTS)


def test_strace_run_spec_hands_preload_to_tracee(tmp_path, monkeypatch):
    """The shims go to the game with `strace -E`, not into strace's own environment."""
    monkeypatch.setenv("LD_PRELOAD", "/inherited.so")
    monkeypatch.setenv("NEXUS_CRASHTRACE_LIB", "/ct.so")
    monkeypatch.setattr(os.path, "exists", lambda p: True)  # pretend the shims exist
    lane = _lane(tmp_path)
    p = c.fault_plan("strace-eintr-read", 7, which=fake_which("strace"), faults=FAULTS)
    game = c.Game("g", ["{stubs}/glitch", "-s", "{seed}"])
    spec = lane.run_spec(game, p, tmp_path)
    argv = spec["argv"]
    i = argv.index("-E")
    assert argv[i + 1].startswith("LD_PRELOAD=/ct.so ")
    assert argv[i + 1].endswith(f"{c.SEED_SHIM} /inherited.so")
    assert argv[i + 2] == "--" and argv[i + 3].endswith("/glitch")
    assert "LD_PRELOAD" not in spec["env"] and spec["unset"] == ["LD_PRELOAD"]
    assert spec["env"]["TORMENT_CRASHTRACE_DIR"] == str(tmp_path)


# -- rlimit -------------------------------------------------------------------

def test_rlimit_plan_with_prlimit():
    p = c.fault_plan("rlimit-tight", 1, which=fake_which("prlimit"), faults=FAULTS)
    assert p.prefix == ["prlimit", "--as=268435456", "--nofile=16", "--fsize=65536",
                        "--stack=262144", "--"]
    assert not p.rlimits


def test_rlimit_plan_without_prlimit_uses_setrlimit():
    p = c.fault_plan("rlimit-tight", 1, which=fake_which(), faults=FAULTS)
    assert p.prefix == [] and p.rlimits == {"AS": 268435456, "NOFILE": 16, "FSIZE": 65536,
                                            "STACK": 262144}


def test_rlimit_run_spec_carries_rlimits(tmp_path):
    p = c.fault_plan("rlimit-tight", 1, which=fake_which(), faults=FAULTS)
    spec = _lane(tmp_path).run_spec(c.Game("g", ["true"]), p, tmp_path)
    assert spec["rlimits"]["NOFILE"] == 16 and spec["argv"] == ["true"]


def _supervise(tmp_path, argv, **extra):
    run = tmp_path / f"run-{uuid.uuid4().hex[:6]}"
    run.mkdir()
    (run / "spec.json").write_text(json.dumps({"argv": argv, "env": {}, **extra}))
    subprocess.run([sys.executable, "-m", "nexus_harness.supervise", str(run)], check=True,
                   timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return run, json.loads((run / "status.json").read_text())


PRINT_LIMITS = ("import resource as r, sys; open(sys.argv[1], 'w').write(' '.join("
                "str(r.getrlimit(getattr(r, 'RLIMIT_' + n))[1]) "
                "for n in ('AS', 'NOFILE', 'FSIZE', 'STACK')))")


def test_supervisor_setrlimit_fallback(tmp_path):
    p = c.fault_plan("rlimit-tight", 1, which=fake_which(), faults=FAULTS)
    out = tmp_path / "limits.txt"
    _, st = _supervise(tmp_path, [sys.executable, "-c", PRINT_LIMITS, str(out)],
                       rlimits=p.rlimits)
    assert st["exit_code"] == 0
    assert out.read_text().split() == ["268435456", "16", "65536", "262144"]


@pytest.mark.skipif(not shutil.which("prlimit"), reason="prlimit (util-linux) missing")
def test_prlimit_prefix_sets_limits_and_keeps_signal(tmp_path):
    p = c.fault_plan("rlimit-tight", 1, faults=FAULTS)
    assert p.prefix[0] == "prlimit"
    out = tmp_path / "limits.txt"
    _, st = _supervise(tmp_path, p.prefix + ["sh", "-c", 'ulimit -Hn > "$0"', str(out)])
    assert st["exit_code"] == 0 and out.read_text().strip() == "16"
    # The setrlimit fallback sets the same limits prlimit does.
    out2 = tmp_path / "limits2.txt"
    _supervise(tmp_path, p.prefix + [sys.executable, "-c", PRINT_LIMITS, str(out2)])
    assert out2.read_text().split() == ["268435456", "16", "65536", "262144"]
    # prlimit execs the game, so a signal death reaches the supervisor unchanged.
    _, st = _supervise(tmp_path, p.prefix + ["sh", "-c", "kill -SEGV $$"])
    assert st["signal"] == 11


def test_supervisor_restores_sigpipe_and_sigxfsz(tmp_path):
    """Python ignores both, and an ignored signal survives exec: without the reset a game
    could never die from SIGXFSZ (rlimit-tight --fsize) or SIGPIPE."""
    for sig in ("XFSZ", "PIPE"):
        _, st = _supervise(tmp_path, ["sh", "-c", f"kill -{sig} $$; exit 7"])
        assert st["signal"] == getattr(signal, f"SIG{sig}"), st


# -- faketime -----------------------------------------------------------------

def test_faketime_plan():
    p = c.fault_plan("time-warp", 1, faults=FAULTS, exists=lambda path: True)
    assert p.preload == [c.FAKETIME_LIB] and p.prefix == []
    assert p.env["FAKETIME"] == "@2038-01-19 03:14:00 x60"


def test_faketime_missing_lib_is_config_error():
    with pytest.raises(c.ConfigError, match="libfaketime.so.1 not found"):
        c.fault_plan("time-warp", 1, faults=FAULTS, exists=lambda path: False)


def test_faketime_preload_is_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CRASHTRACE_LIB", "/ct.so")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    p = c.fault_plan("time-warp", 1, faults=FAULTS, exists=lambda path: True)
    spec = _lane(tmp_path).run_spec(c.Game("g", ["true"]), p, tmp_path)
    assert spec["env"]["LD_PRELOAD"].split()[:3] == ["/ct.so", c.FAKETIME_LIB, c.SEED_SHIM]
    assert spec["env"]["FAKETIME"].startswith("@2038")


@pytest.mark.skipif(not os.path.exists(c.FAKETIME_LIB), reason="libfaketime missing")
def test_faketime_warps_the_clock(tmp_path):
    p = c.fault_plan("time-warp", 1, faults=FAULTS)
    out = tmp_path / "year"
    _supervise(tmp_path, ["sh", "-c", 'date -u +%Y > "$0"', str(out)],
               env={**p.env, "LD_PRELOAD": " ".join(p.preload)})
    assert out.read_text().strip() == "2038"


# -- keystorm -----------------------------------------------------------------

def test_keystorm_plan():
    p = c.fault_plan("keystorm", 1, faults=FAULTS)
    assert p.keystorm_every == 20 and not p.prefix and not p.preload
    with pytest.raises(c.ConfigError):
        c.fault_plan("k", 1, faults={"k": {"kind": "keystorm", "every": 0}})


def test_keystorm_payloads_are_seeded_and_hostile():
    def take(seed, n=300):
        g = c.keystorm_payloads(seed)
        return [next(g) for _ in range(n)]
    a, b = take(5), take(5)
    assert a == b and a != take(6)
    names = {n.rstrip("~") for n, _ in a}
    assert {"csi-8bit", "nul", "paste-4k", "mouse-sgr-huge", "utf8-ff"} <= names
    assert any(n.endswith("~") for n, _ in a)  # some are mutated
    for n, d in a:
        assert d and not any(b in c.TTY_SPECIAL for b in d)
        if n == "paste-4k":
            assert d.startswith(b"\x1b[200~") and d.endswith(b"\x1b[201~") and len(d) == 4108


class FakeTmux:
    def __init__(self):
        self.sent = []

    def send_hex(self, data):
        self.sent.append(data)


def test_keystorm_injects_once_per_n_keys(tmp_path):
    lane = _lane(tmp_path)
    lane.tmux = FakeTmux()
    st = {"base": 100, "done": 0, "gen": c.keystorm_payloads(1)}
    for keys, total in ((105, 0), (119, 0), (120, 1), (121, 1), (139, 1), (140, 2), (300, 6)):
        lane._keystorm(st, keys, 20, tmp_path)
        assert len(lane.tmux.sent) == total, keys  # a burst injects at most 4 at once
    log = (tmp_path / "keystorm.log").read_text().splitlines()
    assert len(log) == 6 and "keys=120" in log[0]


# -- misc ---------------------------------------------------------------------

def test_descendants_finds_grandchildren():
    p = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    try:
        import time
        deadline = time.time() + 5
        while time.time() < deadline and not descendants(p.pid):
            time.sleep(0.05)
        assert descendants(p.pid)
    finally:
        for d in descendants(p.pid):
            os.kill(d, 9)
        p.kill()
        p.wait()


def _lane(tmp_path: Path) -> Lane:
    env = c.LaneEnv.from_env({"NEXUS_LANE_ID": "l-t", "NEXUS_GAME": "g",
                              "NEXUS_OUT": str(tmp_path / "out")})
    return Lane(env)
