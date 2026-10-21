import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexus_harness.config import ConfigError, LaneEnv, fault_plan, seed32, winch_schedule
from nexus_harness.events import EventSender, Identity, fetch_heat, make_event

TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
IDENT = Identity(lane_id="l-042", game="brogue", fault="malloc-1pct", seed=2**64 - 1,
                 model="@cf/qwen/qwen3-30b-a3b-fp8", game_ver="1.14", heat_id="h-0007")
REQUIRED = {"v", "event_id", "ts", "heat_id", "lane_id", "game", "fault", "seed", "model", "type"}


def check_common(ev, type_):
    assert REQUIRED <= ev.keys()
    assert ev["v"] == 1 and ev["type"] == type_
    assert uuid.UUID(ev["event_id"])
    assert TS_RE.match(ev["ts"])
    assert ev["lane_id"] == "l-042" and ev["heat_id"] == "h-0007"
    assert ev["seed"] == 2**64 - 1  # full u64, JSON number
    assert json.loads(json.dumps(ev)) == ev
    assert all(v is not None for v in ev.values())


def test_crash_event_shape():
    ev = make_event(IDENT, "crash", signal=11, exit_code=None, detail="SIGSEGV", turn=812,
                    replay="/out/replay/x.tar.zst")
    check_common(ev, "crash")
    assert ev["signal"] == 11 and "exit_code" not in ev and ev["detail"] == "SIGSEGV"
    assert ev["game_ver"] == "1.14" and ev["turn"] == 812


def test_sanitizer_event_shape():
    ev = make_event(IDENT, "sanitizer", stack_hash="a1b2c3d4e5f6", detail="heap-use-after-free")
    check_common(ev, "sanitizer")
    assert re.fullmatch(r"(ncurses:)?[0-9a-f]{12}", ev["stack_hash"])


def test_hang_heartbeat_lane_events():
    check_common(make_event(IDENT, "hang", detail="screen-frozen", turn=3), "hang")
    hb = make_event(IDENT, "heartbeat", turn=5, tokens_in=10, tokens_out=2)
    check_common(hb, "heartbeat")
    assert hb["turn"] == 5
    check_common(make_event(IDENT, "lane_start"), "lane_start")
    check_common(make_event(IDENT, "lane_end", detail="stopped"), "lane_end")


def test_event_ids_unique_and_type_checked():
    assert make_event(IDENT, "crash")["event_id"] != make_event(IDENT, "crash")["event_id"]
    with pytest.raises(ValueError):
        make_event(IDENT, "explosion")


class Board(BaseHTTPRequestHandler):
    """Fake scoreboard: fails the first `fail` POSTs with 503."""
    fail = 0
    bodies: list = []
    state = {"heat": {"id": "h-0003", "phase": "race", "next_id": "h-0004"}}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        cls = type(self)
        if cls.fail > 0:
            cls.fail -= 1
            self.send_response(503)
            self.end_headers()
            return
        cls.bodies.append((self.headers["Content-Type"], body))
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'{"accepted":1}')

    def do_GET(self):
        data = json.dumps(type(self).state).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def board():
    Board.fail, Board.bodies = 0, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_sender_retries_with_same_event_ids(board, tmp_path):
    Board.fail = 2
    s = EventSender(board, tmp_path / "spool.ndjson", backoff_max=0.2)
    evs = [make_event(IDENT, "crash", detail=str(i)) for i in range(3)]
    for e in evs:
        s.send(e)
    assert s.close(10)
    assert s.failures == 2
    got = [json.loads(l) for _, b in Board.bodies for l in b.decode().splitlines()]
    assert [g["event_id"] for g in got] == [e["event_id"] for e in evs]
    assert all(ct == "application/x-ndjson" for ct, _ in Board.bodies)
    assert all(b.endswith(b"\n") for _, b in Board.bodies)
    spooled = (tmp_path / "spool.ndjson").read_text().splitlines()
    assert [json.loads(l)["event_id"] for l in spooled] == [e["event_id"] for e in evs]


def test_fetch_heat_race_and_pit(board):
    Board.state = {"heat": {"id": "h-0003", "phase": "race", "next_id": "h-0004"}}
    assert fetch_heat(board)[0] == "h-0003"
    Board.state = {"heat": {"id": "h-0003", "phase": "pit", "next_id": "h-0004"}}
    assert fetch_heat(board)[0] == "h-0004"
    assert fetch_heat("http://127.0.0.1:9") is None


def test_lane_env_parsing():
    env = LaneEnv.from_env({"NEXUS_LANE_ID": "l-1", "NEXUS_GAME": "greed",
                            "NEXUS_SEED": str(2**64 + 5), "NEXUS_HANG_SECS": "2.5",
                            "NEXUS_SCREEN_HZ": "10"})
    assert env.seed == 5 and env.fault == "none" and env.hang_secs == 2.5
    assert env.screen_hz == 2.0  # capped at the contract's 2 frames/s
    with pytest.raises(ConfigError):
        LaneEnv.from_env({"NEXUS_GAME": "greed"})


def test_faults_file_matches_defaults():
    from nexus_harness.config import HARNESS_DIR, DEFAULT_FAULTS, load_faults
    faults = load_faults(HARNESS_DIR / "faults.toml")
    for name, spec in DEFAULT_FAULTS.items():
        assert faults[name] == spec, name


def test_fault_profiles():
    assert fault_plan("none", 1).prefix == []
    with pytest.raises(ConfigError, match="fiu-run not found"):
        fault_plan("malloc-1pct", 1, which=lambda _: None)
    p = fault_plan("malloc-1pct", 2**40, which=lambda _: "/usr/bin/fiu-run")
    # Default: faults armed via fiu-ctrl after the game starts. No fiu-run wrapper: the
    # harness sets LD_PRELOAD itself so other preloads survive.
    assert p.prefix == []
    assert p.preload == ["/usr/lib/fiu_run_preload.so", "/usr/lib/fiu_posix_preload.so"]
    assert p.env["FIU_ENABLE"] == ""
    assert p.env["FIU_CTRL_FIFO"].endswith("/fiu-ctrl")
    assert p.delayed_cmds == ["enable_random name=libc/mm/*,probability=0.01"]
    assert p.delay_s == 5.0
    assert p.env["FIU_PRNG_SEED"] == str(2**40 % 2147483647)
    cold = {"cold": {"kind": "libfiu", "points": ["libc/mm/*", "posix/io/rw/*"],
                     "probability": 0.5, "delay_s": 0}}
    c = fault_plan("cold", 1, which=lambda _: "/usr/bin/fiu-run", faults=cold)
    assert c.prefix == []
    assert c.env["FIU_ENABLE"] == ("enable_random name=libc/mm/*,probability=0.5\n"
                                   "enable_random name=posix/io/rw/*,probability=0.5")
    assert c.delayed_cmds == []
    assert fault_plan("winch-storm", 1).winch_storm
    with pytest.raises(ConfigError):
        fault_plan("keystorm", 1)


def test_winch_schedule_seeded():
    a = list(winch_schedule(42, (80, 24), 20))
    assert a == list(winch_schedule(42, (80, 24), 20))
    assert a != list(winch_schedule(43, (80, 24), 20))
    assert all(0.05 <= d <= 0.5 for d, _, _ in a)
    assert all((c, r) == (80, 24) for _, c, r in a[1::2])


def test_seed32():
    assert seed32(0) == 1 and seed32(2147483646) == 1 and seed32(2**64 - 1) > 0
