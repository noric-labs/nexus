import os
import shutil
import subprocess
import time
import uuid

import pytest

from nexus_harness.detectors import HangWatchdog
from nexus_harness.tmux import Tmux, frame_hash


def test_frozen_screen_with_keys_is_hang():
    w = HangWatchdog(secs=10, min_keys=1)
    w.reset(0, 0)
    assert not w.observe(0, "a", 0)
    assert not w.observe(5, "a", 1)
    assert not w.observe(9.9, "a", 2)
    assert w.observe(10.0, "a", 2)


def test_screen_change_resets():
    w = HangWatchdog(secs=10, min_keys=1)
    w.reset(0, 0)
    w.observe(0, "a", 0)
    w.observe(8, "a", 3)
    assert not w.observe(9, "b", 3)  # changed
    assert not w.observe(18, "b", 4)
    assert w.observe(19, "b", 4)


def test_idle_game_without_keys_is_not_hang():
    w = HangWatchdog(secs=10, min_keys=1)
    w.reset(0, 0)
    w.observe(0, "a", 0)
    assert not w.observe(100, "a", 0)
    # ...unless configured to flag any frozen screen
    w0 = HangWatchdog(secs=10, min_keys=0)
    w0.reset(0, 0)
    w0.observe(0, "a", 0)
    assert w0.observe(10, "a", 0)


def test_dead_process_is_not_hang():
    w = HangWatchdog(secs=1, min_keys=0)
    w.reset(0, 0)
    w.observe(0, "a", 0)
    assert not w.observe(5, "a", 0, alive=False)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux missing")
def test_tmux_capture_hash_and_key_counter():
    t = Tmux(socket=f"nexus-test-{uuid.uuid4().hex[:8]}")
    try:
        t.start(["cat"], 80, 24)
        f1 = t.capture()
        assert (f1["cols"], f1["rows"], len(f1["lines"])) == (80, 24, 24)
        assert t.key_count() == 0
        t.send_keys(["hello"], literal=True)
        t.send_keys(["Enter"])
        assert t.key_count() == 2
        deadline = time.time() + 3
        while time.time() < deadline and "hello" not in "\n".join(t.capture()["lines"]):
            time.sleep(0.05)
        f2 = t.capture()
        assert "hello" in f2["lines"][0]
        assert frame_hash(f1) != frame_hash(f2)
        assert frame_hash(f2) == frame_hash(t.capture())
    finally:
        t.run("kill-server", check=False)
