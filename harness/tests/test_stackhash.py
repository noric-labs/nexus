import hashlib
from pathlib import Path

import pytest

from nexus_harness.detectors import (Frame, SanitizerWatcher, classify_exit, parse_frame,
                                     parse_sanitizer_log, stack_hash)

DATA = Path(__file__).parent / "data"


def sha12(*keys):
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]


def test_parse_frame_forms():
    f = parse_frame("    #0 0xaaaa in main /p/t.c:1:64")
    assert (f.index, f.func, f.file, f.module) == (0, "main", "/p/t.c", "")
    f = parse_frame("    #2 0xffff in __libc_start_main@GLIBC_2.17 (/usr/lib/libc.so.6+0x22ed4)")
    assert (f.func, f.module, f.offset) == ("__libc_start_main@GLIBC_2.17", "/usr/lib/libc.so.6", "0x22ed4")
    f = parse_frame("    #7 0x5 in std::vector<int, std::allocator<int> >::push_back(int const&) /x/v.h:12:4")
    assert f.func == "std::vector<int, std::allocator<int> >::push_back(int const&)"
    assert f.file == "/x/v.h"
    f = parse_frame("    #1 0x7f in waddnstr (/usr/lib/libncursesw.so.6+0x1234) (BuildId: 4567ef01)")
    assert (f.func, f.module) == ("waddnstr", "/usr/lib/libncursesw.so.6")
    f = parse_frame("    #0 0x7f  (/opt/game/bin+0x1234)")
    assert (f.func, f.module, f.offset) == ("", "/opt/game/bin", "0x1234")
    assert parse_frame("SUMMARY: AddressSanitizer: x") is None


def test_frame_kinds():
    k = lambda line: parse_frame(line).kind()
    assert k("#0 0x1 in free /build/compiler-rt/lib/asan/asan_malloc_linux.cpp:51:3") == "sanitizer"
    assert k("#0 0x1 in __interceptor_strlen (/usr/lib/libclang_rt.asan.so+0x10)") == "sanitizer"
    assert k("#0 0x1 in __ubsan_handle_add_overflow (/x/libclang_rt.ubsan.so+0x1)") == "sanitizer"
    assert k("#2 0x1 in __libc_start_call_main libc-start.c") == "libc"
    assert k("#0 0x1 in __strlen_avx2 (/usr/lib/libc.so.6+0x18b3c1)") == "libc"
    assert k("#0 0x1 in _start (/game+0x350ac)") == "libc"
    assert k("#1 0x1 in waddnstr (/usr/lib/libncursesw.so.6+0x1234)") == "ncurses"
    assert k("#1 0x1 in doupdate /src/ncurses/tty/tty_update.c:7") == "ncurses"
    assert k("#3 0x1 in printString /b/src/platform/curses-platform.c:88:5") == "project"


def test_asan_uaf_report():
    [r] = parse_sanitizer_log((DATA / "asan_uaf.log").read_text())
    assert r.tool == "asan" and r.fatal and r.complete
    assert r.detail == "heap-use-after-free"
    # Only the first (access) stack counts; libc/_start dropped; basenames only.
    assert [f.key() for f in r.frames if f.kind() == "project"] == [
        "stub_use_after_free@asan.c", "main@asan.c"]
    assert r.stack_hash == sha12("stub_use_after_free@asan.c", "main@asan.c")
    assert len(r.stack_hash) == 12 and int(r.stack_hash, 16) >= 0


def test_hash_ignores_build_dir_line_numbers_and_addresses():
    a = (DATA / "asan_uaf.log").read_text()
    b = (a.replace("/home/build/nexus", "/srv/other/checkout")
          .replace(":12:12", ":99:1").replace("0xaaaaad38746c", "0x123456"))
    assert parse_sanitizer_log(a)[0].stack_hash == parse_sanitizer_log(b)[0].stack_hash


def test_ncurses_prefix_and_top5():
    [r] = parse_sanitizer_log((DATA / "asan_ncurses_segv.log").read_text())
    assert r.detail == "SEGV"
    keys = ["printString@curses-platform.c", "refreshScreen@IO.c", "mainInputLoop@IO.c",
            "rogueMain@RogueMain.c",
            "std::vector<int, std::allocator<int> >::push_back(int const&)@stl_vector.h"]
    assert r.stack_hash == "ncurses:" + sha12(*keys)  # main@main.c is frame 6: cut


def test_no_ncurses_prefix_when_project_frame_on_top():
    frames = [parse_frame(l) for l in [
        "#0 0x1 in __interceptor_memcpy (/x/libclang_rt.asan.so+0x1)",
        "#1 0x1 in drawTile /b/IO.c:5:1",
        "#2 0x1 in wrefresh (/usr/lib/libncursesw.so.6+0x10)",
        "#3 0x1 in main /b/main.c:3:1"]]
    assert stack_hash(frames) == sha12("drawTile@IO.c", "main@main.c")


def test_empty_stack_has_no_hash():
    assert stack_hash([]) == ""
    assert stack_hash([parse_frame("#0 0x1 in __libc_start_main (/usr/lib/libc.so.6+0x1)")]) == ""


def test_ubsan_reports_and_completeness():
    text = (DATA / "ubsan.log").read_text()
    reps = parse_sanitizer_log(text)
    assert [r.complete for r in reps] == [True, True, False]
    a, b, c = reps
    assert a.tool == "ubsan" and not a.fatal
    assert a.detail == "ubsan: signed integer overflow"
    assert a.stack_hash == sha12("worley::noise(double, double, double)@worley.cc",
                                 "dgn_build@dungeon.cc", "main@main.cc")
    # No stack trace: falls back to the error location.
    assert b.detail.startswith("ubsan: load of value N")
    assert b.stack_hash == sha12("?@enum.h")
    assert c.detail == "ubsan: shift exponent N is too large for N-bit type T"
    assert parse_sanitizer_log(text, final=True)[2].complete


def test_watcher_yields_each_report_once(tmp_path):
    log = tmp_path / "ubsan.123"
    lines = (DATA / "ubsan.log").read_text()
    log.write_text(lines)
    w = SanitizerWatcher(tmp_path)
    assert len(w.poll()) == 2
    assert w.poll() == []
    with open(log, "a") as f:
        f.write("SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior x\n")
    assert [r.detail for r in w.poll()] == ["ubsan: shift exponent N is too large for N-bit type T"]
    (tmp_path / "unrelated.txt").write_text("==1==ERROR: AddressSanitizer: SEGV\n")
    assert w.poll(final=True) == []


@pytest.mark.parametrize("status,expect", [
    ({"signal": 11, "exit_code": None}, ("SIGSEGV", 11, None)),
    ({"signal": 6, "exit_code": None}, ("SIGABRT", 6, None)),
    ({"signal": None, "exit_code": 0}, None),
    ({"signal": None, "exit_code": 3}, ("exit 3", None, 3)),
    ({"signal": None, "exit_code": 139}, ("SIGSEGV", 11, 139)),
])
def test_classify_exit(status, expect):
    assert classify_exit(status) == expect


def test_classify_exit_ok_codes():
    assert classify_exit({"exit_code": 1}, ok_exit_codes=(0, 1)) is None
