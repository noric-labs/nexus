"""End to end: the real scoreboard binary with short heats, lanes running the stand-in
games under tmux, keys sent through `nexus-lane keys`, failures asserted via the API."""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from nexus_harness.cli import build_stubs

pytestmark = pytest.mark.integration
HARNESS = Path(__file__).resolve().parents[1]
SCOREBOARD_SRC = HARNESS.parent / "scoreboard"

need = [t for t in ("tmux", "clang") if not shutil.which(t)]
if need:
    pytest.skip(f"missing tools: {need}", allow_module_level=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def wait_for(fn, timeout=30, every=0.2, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}; last={last!r}")


@pytest.fixture(scope="module")
def scoreboard_bin(tmp_path_factory):
    if os.environ.get("NEXUS_SCOREBOARD_BIN"):
        return os.environ["NEXUS_SCOREBOARD_BIN"]
    if not shutil.which("go"):
        pytest.skip("go missing and NEXUS_SCOREBOARD_BIN unset")
    out = tmp_path_factory.mktemp("sb") / "scoreboard"
    env = dict(os.environ, CGO_ENABLED="0")
    env.setdefault("GOCACHE", "/state/gocache")
    env.setdefault("GOMODCACHE", "/state/gomodcache")
    subprocess.run(["go", "build", "-o", str(out), "."], cwd=SCOREBOARD_SRC, env=env, check=True)
    return str(out)


def start_board(scoreboard_bin, d, heat="120s", pit="3s"):
    """A private scoreboard: fresh DB, random free port (other agents share this host)."""
    port = free_port()
    p = subprocess.Popen([scoreboard_bin, "-config", str(d / "none.toml"), "-db", str(d / "sb.db"),
                          "-listen", f"127.0.0.1:{port}", "-heat", heat, "-pit", pit],
                         stdout=open(d / "sb.log", "w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    wait_for(lambda: _ok(url), 15, what="scoreboard up")
    return p, url


@pytest.fixture(scope="module")
def board(scoreboard_bin, tmp_path_factory):
    p, url = start_board(scoreboard_bin, tmp_path_factory.mktemp("board"))
    yield url
    p.terminate()
    p.wait(5)


def _ok(url):
    try:
        return get(url + "/api/state")
    except OSError:
        return None


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    return build_stubs(tmp_path_factory.mktemp("stubs"))


class LaneProc:
    def __init__(self, board, stubs, tmp, lane_id, game, fault="none", **extra):
        self.socket = f"nexus-it-{uuid.uuid4().hex[:8]}"
        self.out = tmp / lane_id
        self.env = dict(os.environ, NEXUS_LANE_ID=lane_id, NEXUS_GAME=game, NEXUS_FAULT=fault,
                        NEXUS_SEED="1000", NEXUS_MODEL="stub-model", NEXUS_SCOREBOARD_URL=board,
                        NEXUS_OUT=str(self.out), NEXUS_TMUX_SOCKET=self.socket,
                        NEXUS_STUBS_DIR=str(stubs), NEXUS_HEARTBEAT_SECS="1", **extra)
        self.p = subprocess.Popen([sys.executable, "-m", "nexus_harness.cli", "run"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "nexus_harness.cli", *args], env=self.env,
                              capture_output=True, text=True)

    def screen(self):
        r = self.cli("screen")
        return r.stdout if r.returncode == 0 else ""

    def keys(self, *keys):
        assert self.cli("keys", *keys).returncode == 0

    def wait_screen(self, pattern, timeout=15):
        return wait_for(lambda: re.search(pattern, self.screen()), timeout, what=pattern)

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
        out, _ = self.p.communicate(timeout=20)
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        return self.p.returncode, out


def lane_state(board, lane_id):
    for l in get(board + "/api/state")["lanes"]:
        if l["lane_id"] == lane_id:
            return l
    return None


def failures(board, lane_id):
    return get(f"{board}/api/lanes/{lane_id}")["failures"]


def test_lanes_score_failures(board, stubs, tmp_path):
    heat = get(board + "/api/state")["heat"]["id"]
    crash = LaneProc(board, stubs, tmp_path, "l-crash", "stub-crasher", "winch-storm")
    asan = LaneProc(board, stubs, tmp_path, "l-asan", "stub-asan", STUB_UBSAN="1")
    hang = LaneProc(board, stubs, tmp_path, "l-hang", "stub-hang", NEXUS_HANG_SECS="2")
    lanes = [crash, asan, hang]
    try:
        # crasher: 3 keys -> SIGSEGV; it restarts with the next seed; 3 more -> SIGSEGV.
        for _ in range(2):
            crash.wait_screen(r"keys: 0 / 3")
            for _ in range(3):
                crash.keys("x")
                time.sleep(0.2)
            wait_for(lambda: crash.out.joinpath("events.ndjson").exists() and
                     "SIGSEGV" in crash.out.joinpath("events.ndjson").read_text(), 10,
                     what="crash event")
            time.sleep(0.5)
        # asan: key 1 -> UBSan signed overflow (non-fatal); key 3 -> heap-use-after-free.
        asan.wait_screen(r"keys: 0 / 3")
        for _ in range(3):
            asan.keys("<Space>")
            time.sleep(0.3)
        # hang: one key, then the stub stops reading; keep "playing" until the watchdog fires.
        hang.wait_screen(r"hang stand-in")
        hang.keys("z<Enter>")
        wait_for(lambda: (hang.keys("<Up>"), lane_state(board, "l-hang") and
                          lane_state(board, "l-hang")["hits"] >= 1)[1], 20, 0.5, "hang scored")

        c = wait_for(lambda: (s := lane_state(board, "l-crash")) and s["hits"] >= 2 and s, 15,
                     what="crash lane scored")
        a = wait_for(lambda: (s := lane_state(board, "l-asan")) and s["hits"] >= 2 and s, 15,
                     what="asan lane scored")
        h = lane_state(board, "l-hang")

        # Scoring: crash 3 + 3*0.5 (same "crash|SIGSEGV" key), sanitizer 5 + 5 (two hashes), hang 2.
        assert c["score"] == pytest.approx(4.5) and c["fault"] == "winch-storm"
        assert a["score"] == pytest.approx(10.0) and a["clusters"] == 2
        assert h["score"] == pytest.approx(2.0)
        for s in (c, a, h):
            assert s["heat_id"] == heat and s["model"] == "stub-model" and s["status"] == "running"
            assert s["game_ver"] == "stub"

        cf = failures(board, "l-crash")
        assert {f["type"] for f in cf} == {"crash"}
        assert all(f["signal"] == 11 and f["detail"] == "SIGSEGV" and f["scored"] for f in cf)
        assert sorted(f["seed"] for f in cf) == [1000, 1001]  # restarted with the next seed
        assert all(Path(f["replay"]).exists() for f in cf)

        af = failures(board, "l-asan")
        assert {f["type"] for f in af} == {"sanitizer"}  # ASan exit is not also a crash
        details = {f["detail"]: f["stack_hash"] for f in af}
        assert set(details) == {"heap-use-after-free", "ubsan: signed integer overflow"}
        assert all(re.fullmatch(r"[0-9a-f]{12}", h_) for h_ in details.values())
        assert all(f["cluster"] == f"stub-asan/{f['stack_hash']}" for f in af)

        hf = failures(board, "l-hang")
        assert [f["type"] for f in hf] == ["hang"] and hf[0]["detail"] == "screen-frozen"

        # Turn: without agent stats it is the number of `nexus-lane keys` calls...
        wait_for(lambda: lane_state(board, "l-crash")["turn"] >= 6, 5, what="crash turn")
        # ...with them, heartbeats copy turn/tokens from $NEXUS_OUT/agent/stats.json, and
        # agent errors from $NEXUS_OUT/agent/events.ndjson are forwarded as agent_error.
        agent = asan.out / "agent"
        agent.mkdir(exist_ok=True)
        (agent / "stats.json").write_text(json.dumps(
            {"v": 1, "turn": 7, "tokens_in": 4100, "tokens_out": 120, "actions": 7,
             "keys_sent": 30, "capped": ""}))
        with open(agent / "events.ndjson", "a") as f:
            f.write(json.dumps({"ts": "2026-10-21T12:00:00.000Z", "type": "agent_error",
                                "lane_id": "l-asan", "model": "stub-model",
                                "detail": "429 rate limited"}) + "\n")
        wait_for(lambda: (s := lane_state(board, "l-asan"))["turn"] == 7
                 and s["tokens_in"] == 4100 and s["tokens_out"] == 120, 8, what="agent stats")
        ae = wait_for(lambda: [f for f in failures(board, "l-asan") if f["type"] == "agent_error"],
                      8, what="agent_error forwarded")
        assert ae[0]["detail"] == "429 rate limited" and ae[0]["points"] == 0
        assert lane_state(board, "l-asan")["score"] == pytest.approx(10.0)

        # Screen frames reach the scoreboard.
        fr = get(f"{board}/api/lanes/l-asan/screen")
        assert fr["cols"] == 80 and fr["rows"] == 24 and len(fr["lines"]) == 24
    finally:
        results = {l.env["NEXUS_LANE_ID"]: l.stop() for l in lanes}
    for lid, (rc, out) in results.items():
        assert rc == 0, out
    for lid in results:
        assert wait_for(lambda: lane_state(board, lid)["status"] == "ended", 5, what="ended")
    ev = [json.loads(l) for l in (crash.out / "events.ndjson").read_text().splitlines()]
    types = [e["type"] for e in ev]
    assert types[0] == "lane_start" and types[-1] == "lane_end" and "heartbeat" in types


@pytest.mark.skipif(bool(shutil.which("fiu-run")), reason="fiu-run is installed")
def test_malloc_fault_without_libfiu_ends_lane(board, stubs, tmp_path):
    lane = LaneProc(board, stubs, tmp_path, "l-fiu", "stub-crasher", "malloc-1pct")
    rc, out = lane.stop() if lane.p.wait(20) is None else (lane.p.returncode, lane.p.stdout.read())
    assert rc == 2, out
    ev = [json.loads(l) for l in (lane.out / "events.ndjson").read_text().splitlines()]
    assert [e["type"] for e in ev] == ["lane_end"]
    assert "fiu-run not found" in ev[0]["detail"] and ev[0]["fault"] == "malloc-1pct"
    s = wait_for(lambda: lane_state(board, "l-fiu"), 5, what="lane on board")
    assert s["status"] == "ended"


def test_new_heat_recycles_game_with_next_seed(scoreboard_bin, stubs, tmp_path):
    p, url = start_board(scoreboard_bin, tmp_path, heat="3s", pit="2s")
    try:
        lane = LaneProc(url, stubs, tmp_path, "l-recycle", "stub-crasher")
        first = get(url + "/api/state")["heat"]
        wait_for(lambda: (lane.out / "runs" / "0001").exists(), 15, what="recycle")
        rc, out = lane.stop()
        assert rc == 0, out
        log = (lane.out / "lane.log").read_text()
        assert "recycling for the new heat" in log
        ev = [json.loads(l) for l in (lane.out / "events.ndjson").read_text().splitlines()]
        heats = [e["heat_id"] for e in ev]
        assert heats[0] == first["id"] and heats[-1] != first["id"]
        assert ev[-1]["seed"] >= 1001
    finally:
        p.terminate()
        p.wait(5)


def test_crash_gets_stack_hash_from_crashtrace(board, stubs, tmp_path, crashtrace_lib):
    import tarfile
    lane = LaneProc(board, stubs, tmp_path, "l-trace", "stub-crasher",
                    NEXUS_CRASHTRACE_LIB=crashtrace_lib, STUB_CRASH_SITE="other")
    try:
        for n in (1, 2):
            lane.wait_screen(r"keys: 0 / 3")
            for _ in range(3):
                lane.keys("x")
                time.sleep(0.2)
            wait_for(lambda: len(failures(board, "l-trace")) >= n, 15, what=f"crash {n}")
            time.sleep(0.5)
        cf = failures(board, "l-trace")
        hashes = {f["stack_hash"] for f in cf}
        assert len(hashes) == 1 and re.fullmatch(r"[0-9a-f]{12}", next(iter(hashes)))
        assert all(f["type"] == "crash" and f["detail"] == "SIGSEGV" for f in cf)
        assert all(f["cluster"] == f"stub-crasher/{f['stack_hash']}" for f in cf)
        assert lane_state(board, "l-trace")["score"] == pytest.approx(4.5)
        with tarfile.open(cf[0]["replay"]) as tf:
            rep = next(m for m in tf.getmembers() if m.name.endswith("/report.txt"))
            text = tf.extractfile(rep).read().decode()
        assert "torment-crashtrace: signal 11" in text and "stub_other" in text
        spec = json.loads((lane.out / "runs" / "0000" / "spec.json").read_text())
        assert spec["env"]["LD_PRELOAD"].split()[0] == crashtrace_lib
        assert spec["env"]["TORMENT_CRASHTRACE_DIR"].endswith("runs/0000")
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


# -- visual corruption and the new fault kinds ---------------------------------

def events_of(lane, type_=None):
    path = lane.out / "events.ndjson"
    if not path.exists():
        return []
    ev = [json.loads(l) for l in path.read_text().splitlines()]
    return [e for e in ev if type_ is None or e["type"] == type_]


def test_visual_corruption_scores_once_per_condition(board, stubs, tmp_path):
    lane = LaneProc(board, stubs, tmp_path, "l-vis", "stub-glitch")
    try:
        lane.wait_screen(r"Depth: 1")
        lane.keys("g")  # garbage cells
        wait_for(lambda: events_of(lane, "visual"), 10, what="bad-cells")
        lane.keys("b")  # blank: blank-screen after 2 s, status-missing after 5 s
        wait_for(lambda: len(failures(board, "l-vis")) >= 3, 15, what="three visual failures")
        lane.keys("r")
        lane.keys("g")
        lane.keys("b")  # the same conditions again: debounced for this run
        time.sleep(6)
        vf = failures(board, "l-vis")
        assert sorted(f["detail"] for f in vf) == ["bad-cells", "blank-screen", "status-missing"]
        assert all(f["type"] == "visual" and not f["stack_hash"] for f in vf)
        assert {f["cluster"] for f in vf} == {f"stub-glitch/visual|{f['detail']}" for f in vf}
        assert lane_state(board, "l-vis")["score"] == pytest.approx(3.0)
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


def test_rlimit_tight_kills_big_writer_with_sigxfsz(board, stubs, tmp_path):
    lane = LaneProc(board, stubs, tmp_path, "l-rlim", "stub-glitch", "rlimit-tight",
                    STUB_FILE=str(tmp_path / "big.out"))
    try:
        lane.wait_screen(r"Depth: 1")
        spec = json.loads((lane.out / "runs" / "0000" / "spec.json").read_text())
        if shutil.which("prlimit"):
            assert spec["argv"][:2] == ["prlimit", "--as=268435456"]
        else:
            assert spec["rlimits"]["FSIZE"] == 65536
        lane.keys("f")
        cf = wait_for(lambda: failures(board, "l-rlim"), 10, what="SIGXFSZ crash")
        assert cf[0]["type"] == "crash" and cf[0]["signal"] == 25 and cf[0]["detail"] == "SIGXFSZ"
        wait_for(lambda: (lane.out / "runs" / "0001" / "pid").exists(), 15, what="restart")
        lane.wait_screen(r"keys: 0")
        lane.keys("x")  # exit 3 passes through prlimit unchanged
        ee = wait_for(lambda: [f for f in failures(board, "l-rlim") if f["type"] == "error_exit"],
                      10, what="error_exit")
        assert ee[0]["exit_code"] == 3
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


def test_keystorm_injects_hostile_bytes(board, stubs, tmp_path):
    faults = tmp_path / "faults.toml"
    faults.write_text('[faults.ks]\nkind = "keystorm"\nevery = 3\n')
    inlog = tmp_path / "input.bin"
    lane = LaneProc(board, stubs, tmp_path, "l-ks", "stub-glitch", "ks",
                    NEXUS_FAULTS_FILE=str(faults), STUB_INPUT_LOG=str(inlog))
    try:
        lane.wait_screen(r"Depth: 1")
        for _ in range(7):
            lane.keys("a")
            time.sleep(0.15)
        log = lane.out / "runs" / "0000" / "keystorm.log"
        wait_for(lambda: log.exists() and len(log.read_text().splitlines()) == 2, 10,
                 what="two injections")
        time.sleep(1.0)
        assert len(log.read_text().splitlines()) == 2
        injected = sum(int(l.rsplit("len=", 1)[1]) for l in log.read_text().splitlines())
        data = inlog.read_bytes()
        assert data.count(b"a") >= 7 and len(data) == 7 + injected
        # Injected bytes are not agent keys.
        assert subprocess.run(["tmux", "-L", lane.socket, "show-options", "-v", "-t", "game",
                               "@nexus_keys"], capture_output=True, text=True).stdout.strip() == "7"
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


def test_winch_storm_raises_no_visual_false_positives(board, stubs, tmp_path):
    """Measured: the stand-ins (including one that redraws on SIGWINCH with wide glyphs)
    produce no visual hits under the storm, so the resize quiet window defaults to off."""
    lane = LaneProc(board, stubs, tmp_path, "l-storm", "stub-glitch", "winch-storm")
    try:
        lane.wait_screen(r"glitch stand-in")
        for _ in range(8):
            lane.keys("a")
            time.sleep(1)
        assert events_of(lane, "visual") == []
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


@pytest.mark.parametrize("fault,tool,msg", [
    ("strace-eintr-read", "strace", "strace not found"),
    ("time-warp", "/usr/lib/faketime/libfaketime.so.1", "libfaketime.so.1 not found"),
])
def test_missing_fault_tool_ends_lane(board, stubs, tmp_path, fault, tool, msg):
    if shutil.which(tool) or os.path.exists(tool):
        pytest.skip(f"{tool} is installed")
    lane = LaneProc(board, stubs, tmp_path, f"l-miss-{fault}", "stub-crasher", fault)
    assert lane.p.wait(20) == 2
    ev = events_of(lane)
    assert [e["type"] for e in ev] == ["lane_end"] and msg in ev[0]["detail"]
    lane.stop()


@pytest.mark.skipif(not shutil.which("strace"), reason="strace missing")
def test_strace_keeps_real_exit_status_and_preloads(board, stubs, tmp_path, crashtrace_lib):
    """strace -f exits with the tracee's status and re-raises its fatal signal; the
    crashtrace shim reaches the game (via -E), not strace."""
    lane = LaneProc(board, stubs, tmp_path, "l-strace", "stub-crasher", "strace-eintr-read",
                    NEXUS_CRASHTRACE_LIB=crashtrace_lib, STUB_CRASH_SITE="other")
    try:
        wait_for(lambda: (lane.out / "runs" / "0000" / "spec.json").exists() or
                 lane.p.poll() is not None, 15, what="first run")
        if lane.p.poll() is not None:
            ev = events_of(lane)
            pytest.skip(f"strace cannot trace here: {ev[-1]['detail'] if ev else ''}")
        spec = json.loads((lane.out / "runs" / "0000" / "spec.json").read_text())
        assert spec["argv"][0] == "strace" and "LD_PRELOAD" not in spec["env"]
        assert any(a.startswith(f"LD_PRELOAD={crashtrace_lib}") for a in spec["argv"])
        lane.wait_screen(r"keys: 0 / 3")
        for _ in range(3):
            lane.keys("x")
            time.sleep(0.2)
        cf = wait_for(lambda: failures(board, "l-strace"), 15, what="crash under strace")
        assert cf[0]["type"] == "crash" and cf[0]["signal"] == 11
        assert re.fullmatch(r"[0-9a-f]{12}", cf[0]["stack_hash"] or "")
        st = json.loads((lane.out / "runs" / "0000" / "status.json").read_text())
        assert st["signal"] == 11
        with tarfile_open(cf[0]["replay"]) as tf:
            rep = next(m for m in tf.getmembers() if m.name.endswith("/report.txt"))
            assert "stub_other" in tf.extractfile(rep).read().decode()
    finally:
        rc, out = lane.stop()
    assert rc == 0, out


def tarfile_open(path):
    import tarfile
    return tarfile.open(path)
