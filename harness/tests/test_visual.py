import shutil
import time
import uuid

import pytest

from nexus_harness import config as c
from nexus_harness.cli import build_stubs
from nexus_harness.detectors import (BAD_CELLS, BLANK_SCREEN, CURSOR_OUTSIDE, STATUS_MISSING,
                                     VisualDetector)
from nexus_harness.tmux import Tmux


def frame(lines, cursor=(0, 0), cols=80, rows=24):
    return {"cols": cols, "rows": rows, "cursor": list(cursor),
            "lines": (list(lines) + [""] * rows)[:rows]}


OK = frame(["hello", "", "Depth: 3"])
BLANK = frame([])


def conds(hits):
    return [h.condition for h in hits]


def test_clean_frames_are_quiet():
    v = VisualDetector("Depth:", 5)
    for t in range(100):
        assert v.observe(t, OK) == []


def test_cursor_outside_pane_but_not_pending_wrap():
    v = VisualDetector()
    assert v.observe(0, frame(["x"], cursor=(80, 23))) == []  # tmux pending-wrap state
    assert conds(v.observe(1, frame(["x"], cursor=(3, 24)))) == [CURSOR_OUTSIDE]
    assert v.observe(2, frame(["x"], cursor=(81, 0))) == []  # once per run
    v.reset(3)
    assert conds(v.observe(4, frame(["x"], cursor=(81, 0)))) == [CURSOR_OUTSIDE]


@pytest.mark.parametrize("bad", ["�", "\x9b", "\x85", "\x01", "\x7f"])
def test_bad_cells(bad):
    v = VisualDetector()
    hits = v.observe(0, frame(["fine", f"ab{bad}cd"]))
    assert conds(hits) == [BAD_CELLS] and "row 1 col 2" in hits[0].report


def test_unicode_glyphs_are_not_bad_cells():
    v = VisualDetector()
    assert v.observe(0, frame(["─│ café 漢 @ # ·"])) == []


def test_blank_screen_after_content():
    v = VisualDetector(blank_secs=2)
    assert v.observe(0, BLANK) == []  # blank before anything was drawn: startup
    assert v.observe(1, BLANK) == [] and v.observe(5, BLANK) == []
    v.observe(6, OK)
    assert v.observe(7, BLANK) == []
    assert v.observe(9, BLANK) == []  # exactly 2 s: not yet
    assert conds(v.observe(9.5, BLANK)) == [BLANK_SCREEN]
    v.observe(10, OK)
    v.observe(11, BLANK)
    assert v.observe(20, BLANK) == []  # once per run


def test_blank_flicker_and_dead_game_are_not_flagged():
    v = VisualDetector(blank_secs=2)
    v.observe(0, OK)
    for t in range(1, 20):  # curses clear+redraw: blank frames never last 2 s
        assert v.observe(t, BLANK if t % 2 else OK) == []
    v.observe(20, BLANK)
    assert v.observe(30, BLANK, alive=False) == []
    assert v.observe(31, BLANK) == []  # timer restarted after the dead frame


def test_quiet_suspends_resize_sensitive_checks():
    v = VisualDetector(blank_secs=2)
    v.observe(0, OK)
    assert v.observe(1, frame(["�"], cursor=(99, 99)), quiet=True) == []
    v.observe(2, BLANK)
    assert v.observe(5, BLANK, quiet=True) == []
    assert v.observe(6, BLANK) == []  # blank timer restarted by the quiet frame
    assert conds(v.observe(8.5, BLANK)) == [BLANK_SCREEN]


def test_status_missing():
    v = VisualDetector("Depth:", status_secs=5)
    title = frame(["Welcome"])
    assert v.observe(0, title) == [] and v.observe(100, title) == []  # never seen: menus
    v.observe(101, OK)
    menu = frame(["Inventory"])
    assert v.observe(103, menu) == []
    assert v.observe(106, menu) == []  # exactly 5 s
    assert conds(v.observe(106.5, menu)) == [STATUS_MISSING]
    assert v.observe(200, menu) == []


def test_no_status_regex_means_no_status_check():
    v = VisualDetector(None)
    v.observe(0, OK)
    assert v.observe(1000, frame(["x"])) == []


def test_games_toml_status_regexes():
    games = c.load_games(c.HARNESS_DIR / "games.toml")
    assert games["brogue"].status_regex == "Depth:"
    assert games["dcss"].status_regex == "Health:"
    assert games["stub-glitch"].status_regex and games["greed"].status_regex is None


@pytest.mark.skipif(not shutil.which("tmux") or not (shutil.which("gcc") or shutil.which("clang")),
                    reason="tmux or a C compiler missing")
def test_glitch_stub_through_tmux(tmp_path):
    """The real capture path: garbage output shows up as U+FFFD/C1 cells, a cleared
    screen as blank, and the status line disappears with `s`."""
    stubs = build_stubs(tmp_path)
    t = Tmux(socket=f"nexus-vis-{uuid.uuid4().hex[:8]}", conf=str(c.HARNESS_DIR / "tmux.conf"))
    v = VisualDetector("Depth:", status_secs=0.5, blank_secs=0.5)
    seen: list[str] = []

    def pump(secs):
        end = time.time() + secs
        while time.time() < end:
            seen.extend(conds(v.observe(time.time(), t.capture())))
            time.sleep(0.05)

    try:
        t.start([str(stubs / "glitch")], 80, 24)
        pump(0.5)
        assert seen == []
        t.send_keys(["s"], literal=True)
        pump(1.0)
        assert seen == [STATUS_MISSING]
        t.send_keys(["r"], literal=True)
        t.send_keys(["g"], literal=True)
        pump(0.5)
        assert seen == [STATUS_MISSING, BAD_CELLS]
        t.send_keys(["b"], literal=True)
        pump(1.2)
        assert seen == [STATUS_MISSING, BAD_CELLS, BLANK_SCREEN]
    finally:
        t.run("kill-server", check=False)
