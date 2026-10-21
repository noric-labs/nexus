"""libtorment_crashtrace.so + nexus_harness.crashtrace against the stub crasher (no tmux)."""

import hashlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from nexus_harness import crashtrace
from nexus_harness.cli import build_stubs


def sha12(*keys):
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")):
        pytest.skip("no C compiler")
    return build_stubs(tmp_path_factory.mktemp("stubs"))


def crash(lib, exe, run_dir: Path, site="deref", sig=signal.SIGSEGV, **env):
    run_dir.mkdir(parents=True, exist_ok=True)
    e = dict(os.environ, LD_PRELOAD=lib, TORMENT_CRASHTRACE_DIR=str(run_dir),
             STUB_CRASH_SITE=site, **env)
    p = subprocess.run([str(exe)], input=b"xxx", env=e, capture_output=True, timeout=20)
    assert p.returncode == -sig, p  # still dies from the signal itself
    traces = list(run_dir.glob("crash-*.txt"))
    assert len(traces) == 1
    pid = int(traces[0].stem.split("-")[1])
    return crashtrace.analyze(run_dir, {"pid": pid, "signal": int(sig)})


def test_same_site_same_hash_distinct_sites_distinct(crashtrace_lib, stubs, tmp_path):
    exe = stubs / "crasher"
    a1 = crash(crashtrace_lib, exe, tmp_path / "a1")
    a2 = crash(crashtrace_lib, exe, tmp_path / "a2")
    b = crash(crashtrace_lib, exe, tmp_path / "b", site="other")
    ab = crash(crashtrace_lib, exe, tmp_path / "ab", site="abort", sig=signal.SIGABRT)
    for t in (a1, a2, b, ab):
        assert t.complete and t.unwound and t.signal in (11, 6)
    assert a1.pid != a2.pid and a1.raw[0].rel == a2.raw[0].rel  # offsets survive ASLR
    assert a1.stack_hash == a2.stack_hash
    assert len({a1.stack_hash, b.stack_hash, ab.stack_hash}) == 3
    # The stubs are built with -g: frames symbolize to func@file, libc frames are dropped.
    if crashtrace._symbolizer():
        assert a1.stack_hash == sha12("stub_deref@crasher.c", "stub_turn@crasher.c",
                                      "main@crasher.c")
        assert b.stack_hash == sha12("stub_other@crasher.c", "stub_turn@crasher.c",
                                     "main@crasher.c")
        assert ab.stack_hash == sha12("stub_abort@crasher.c", "stub_turn@crasher.c",
                                      "main@crasher.c")
        rep = a1.report()
        assert "in stub_deref " in rep and "crasher.c:" in rep and a1.stack_hash in rep
        assert "frame 0 0x" in rep  # raw trace kept too


def test_stripped_binary_hashes_module_offsets(crashtrace_lib, stubs, tmp_path):
    if not shutil.which("strip"):
        pytest.skip("strip missing")
    exe = tmp_path / "crasher-stripped"
    subprocess.run(["strip", "-o", str(exe), str(stubs / "crasher")], check=True)
    a1 = crash(crashtrace_lib, exe, tmp_path / "a1")
    a2 = crash(crashtrace_lib, exe, tmp_path / "a2")
    b = crash(crashtrace_lib, exe, tmp_path / "b", site="other")
    assert a1.stack_hash and a1.stack_hash == a2.stack_hash != b.stack_hash
    assert all(f.func == "" and f.key().startswith("+0x") for f in a1.frames
               if f.kind() == "project")


def test_symtab_only_binary_uses_function_names(crashtrace_lib, stubs, tmp_path):
    if not shutil.which("strip") or not crashtrace._symbolizer():
        pytest.skip("strip or symbolizer missing")
    exe = tmp_path / "crasher-nodebug"  # like a release build: symbols, no DWARF
    subprocess.run(["strip", "-g", "-o", str(exe), str(stubs / "crasher")], check=True)
    a = crash(crashtrace_lib, exe, tmp_path / "a")
    assert a.stack_hash == sha12("stub_deref@crasher-nodebug", "stub_turn@crasher-nodebug",
                                 "main@crasher-nodebug")


def test_no_symbolizer_falls_back_to_offsets(crashtrace_lib, stubs, tmp_path):
    a = crash(crashtrace_lib, stubs / "crasher", tmp_path / "a")
    t = crashtrace.analyze(tmp_path / "a", {"signal": 11}, which=lambda: None)
    assert t.stack_hash and (t.stack_hash != a.stack_hash or not crashtrace._symbolizer())
    assert t.frames[0].key() == f"+{hex(a.raw[0].rel)}@crasher"


def test_disabled_without_dir_and_with_own_handler(crashtrace_lib, stubs, tmp_path):
    e = dict(os.environ, LD_PRELOAD=crashtrace_lib)
    e.pop("TORMENT_CRASHTRACE_DIR", None)
    p = subprocess.run([str(stubs / "crasher")], input=b"xxx", env=e, capture_output=True,
                       cwd=tmp_path, timeout=20)
    assert p.returncode == -signal.SIGSEGV and not list(tmp_path.glob("crash-*"))
    # A program whose SIGSEGV disposition is not SIG_DFL keeps it: python ignores SIGPIPE
    # and installs nothing for SEGV, so use an ignored SIGSEGV via a shell `trap ''`.
    sh = shutil.which("sh")
    p = subprocess.run([sh, "-c", "trap '' SEGV; exec \"$0\"", str(stubs / "crasher")],
                       input=b"xxx", env=dict(e, TORMENT_CRASHTRACE_DIR=str(tmp_path)),
                       capture_output=True, timeout=20)
    # SIG_IGN is inherited across exec; the kernel still kills on a real fault, but the
    # library must not have installed its handler (no trace written).
    assert p.returncode == -signal.SIGSEGV and not list(tmp_path.glob("crash-*"))


def test_parse_truncated_and_find(tmp_path):
    text = ("torment-crashtrace 1\npid 42\nsignal 11\ncode 1\naddr 0x0\nunwound 1\n"
            "frame 0 0x5555deadbeef 0x1234 /opt/game/bin\n"
            "frame 1 0x7fff00001000 0x22d94 /usr/lib/libc.so.6\n")
    t = crashtrace.parse(text)
    assert (t.pid, t.signal, t.complete, len(t.raw)) == (42, 11, False, 2)
    (tmp_path / "crash-42.txt").write_text(text)
    assert crashtrace.find(tmp_path, 42) == tmp_path / "crash-42.txt"
    assert crashtrace.find(tmp_path, 7, 11) == tmp_path / "crash-42.txt"
    assert crashtrace.find(tmp_path, 7, 6) is None
    t = crashtrace.analyze(tmp_path, {"pid": 42, "signal": 11}, which=lambda: None)
    # /opt/game/bin does not exist: unsymbolized module+offset, libc dropped.
    assert t.stack_hash == sha12("+0x1234@bin")
    assert "(truncated)" in t.report()


def test_library_resolution(tmp_path):
    assert crashtrace.library({"NEXUS_CRASHTRACE_LIB": "none"}) is None
    assert crashtrace.library({"NEXUS_CRASHTRACE_LIB": str(tmp_path / "missing.so")}) is None
    lib = tmp_path / "x.so"
    lib.write_bytes(b"")
    assert crashtrace.library({"NEXUS_CRASHTRACE_LIB": str(lib)}) == str(lib)
