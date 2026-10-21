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
