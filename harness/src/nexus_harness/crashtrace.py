"""Crash stacks for signal deaths of builds without a sanitizer.

The game runs with `libtorment_crashtrace.so` in LD_PRELOAD (package torment-crashtrace)
and TORMENT_CRASHTRACE_DIR=<run_dir>. On SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT it writes
`crash-<pid>.txt`: the faulting pc and the return addresses, each as module path plus
offset from the module's load base, then dies from the same signal. Here those frames are
symbolized (llvm-symbolizer, else addr2line) and hashed with `detectors.stack_hash`, so
crash events get a `stack_hash` like sanitizer reports do.

Frame keys follow the sanitizer rules: `func@basename(file)` with DWARF, `func@module`
with only a symbol table, and `+0xOFF@module` for a stripped module. Frames in system
libraries (libc, loader, libstdc++, libgcc) and in preloaded fault/trace shims are dropped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import CRASHTRACE_LIB
from .detectors import Frame, stack_hash

# Preloaded helpers that are never the bug: this library, the seed shim, libfiu.
_SHIM_MODULES = ("libtorment_", "fiu_run_preload", "fiu_posix_preload", "libfiu")
_FRAME_RE = re.compile(r"^frame (\d+) 0x([0-9a-f]+) 0x([0-9a-f]+) (.*)$")
MAX_REPORT_FRAMES = 32


def library(env: dict[str, str] | None = None) -> str | None:
    """The preload library, or None. NEXUS_CRASHTRACE_LIB overrides the installed path
    (set it to `none` to turn crash traces off)."""
    e = os.environ if env is None else env
    path = e.get("NEXUS_CRASHTRACE_LIB") or CRASHTRACE_LIB
    if path in ("none", "0", "off"):
        return None
    return path if os.path.exists(path) else None


@dataclass
class RawFrame:
    index: int
    pc: int
    rel: int  # pc - module load base
    module: str  # "" when the address was not in a mapped file


@dataclass
class CrashTrace:
    pid: int | None
    signal: int | None
    raw: list[RawFrame]
    complete: bool  # the `end` line was written
    unwound: bool
    text: str  # the crash-<pid>.txt contents
    frames: list[Frame] = field(default_factory=list)  # symbolized, inlines expanded
    lines: list[str] = field(default_factory=list)  # ASan-style "#i 0x.. in f file (mod+off)"
    stack_hash: str = ""

    def report(self) -> str:
        """Short symbolized stack for the replay bundle (report.txt)."""
        head = f"torment-crashtrace: signal {self.signal} pid {self.pid}"
        if not self.complete:
            head += " (truncated)"
        body = self.lines[:MAX_REPORT_FRAMES]
        if len(self.lines) > MAX_REPORT_FRAMES:
            body.append(f"    ... {len(self.lines) - MAX_REPORT_FRAMES} more")
        return "\n".join([head, f"stack_hash: {self.stack_hash or '-'}", *body, "",
                          "raw:", self.text.rstrip(), ""])


def parse(text: str) -> CrashTrace:
    hdr: dict[str, str] = {}
    raw: list[RawFrame] = []
    complete = False
    for ln in text.splitlines():
        m = _FRAME_RE.match(ln)
        if m:
            mod = m.group(4)
            raw.append(RawFrame(int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16),
                                "" if mod == "?" else mod))
        elif ln == "end":
            complete = True
        elif " " in ln:
            k, v = ln.split(" ", 1)
            hdr[k] = v

    def num(k):
        try:
            return int(hdr[k])
        except (KeyError, ValueError):
            return None

    return CrashTrace(pid=num("pid"), signal=num("signal"), raw=raw, complete=complete,
                      unwound=hdr.get("unwound") == "1", text=text)


def find(run_dir: Path, pid: int | None = None, signal: int | None = None) -> Path | None:
    """The trace for the game's pid, else the newest trace with the same signal (the game
    may have been started through a wrapper that forked)."""
    run_dir = Path(run_dir)
    if pid and (run_dir / f"crash-{pid}.txt").exists():
        return run_dir / f"crash-{pid}.txt"
    cands = sorted(run_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            head = p.read_text(errors="replace")[:200]
        except OSError:
            continue
        if signal is None or f"\nsignal {signal}\n" in head:
            return p
    return None


# ---------------------------------------------------------------------------
# Symbolization
# ---------------------------------------------------------------------------

def _load_bias(module: str) -> int | None:
    """Lowest PT_LOAD vaddr (page aligned) of an ELF file: 0 for PIE executables and
    shared objects, the link address (e.g. 0x400000) for fixed-address executables.
    None when the file cannot be read as ELF."""
    try:
        with open(module, "rb") as f:
            ident = f.read(64)
            if len(ident) < 52 or ident[:4] != b"\x7fELF":
                return None
            is64, end = ident[4] == 2, "<" if ident[5] == 1 else ">"
            if is64:
                phoff, = struct.unpack_from(end + "Q", ident, 32)
                phentsize, phnum = struct.unpack_from(end + "HH", ident, 54)
            else:
                phoff, = struct.unpack_from(end + "I", ident, 28)
                phentsize, phnum = struct.unpack_from(end + "HH", ident, 42)
            f.seek(phoff)
            ph = f.read(phentsize * phnum)
    except OSError:
        return None
    lows = []
    for i in range(phnum):
        off = i * phentsize
        if is64:
            p_type, = struct.unpack_from(end + "I", ph, off)
            p_vaddr, = struct.unpack_from(end + "Q", ph, off + 16)
        else:
            p_type, = struct.unpack_from(end + "I", ph, off)
            p_vaddr, = struct.unpack_from(end + "I", ph, off + 8)
        if p_type == 1:  # PT_LOAD
            lows.append(p_vaddr)
    return (min(lows) & ~0xFFF) if lows else 0


def _symbolizer() -> tuple[str, str] | None:
    for kind, path in (("llvm", os.environ.get("ASAN_SYMBOLIZER_PATH")),
                       ("llvm", shutil.which("llvm-symbolizer")),
                       ("gnu", shutil.which("addr2line"))):
        if path and os.access(path, os.X_OK):
            return kind, path
    return None


def _clean(s: str | None) -> str:
    return "" if not s or s.startswith("??") else s


def _run_llvm(tool: str, module: str, addrs: list[int]) -> list[list[tuple[str, str]]]:
    r = subprocess.run([tool, "--output-style=JSON", "--demangle", "--inlines",
                        f"--obj={module}", *[hex(a) for a in addrs]],
                       capture_output=True, text=True, timeout=30)
    out = []
    for chunk in r.stdout.splitlines():
        chunk = chunk.strip()
        if not chunk:
            continue
        data = json.loads(chunk)
        for ent in data if isinstance(data, list) else [data]:
            syms = []
            for s in ent.get("Symbol") or []:
                fn, file = _clean(s.get("FunctionName")), _clean(s.get("FileName"))
                line = s.get("Line") or 0
                syms.append((fn, f"{file}:{line}" if file and line else file))
            out.append(syms)
    return out


def _run_gnu(tool: str, module: str, addrs: list[int]) -> list[list[tuple[str, str]]]:
    r = subprocess.run([tool, "-a", "-f", "-C", "-i", "-e", module, *[hex(a) for a in addrs]],
                       capture_output=True, text=True, timeout=30)
    out: list[list[tuple[str, str]]] = []
    lines = r.stdout.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("0x"):
            out.append([])
            i += 1
            continue
        if out and i + 1 < len(lines):
            fn, loc = _clean(lines[i]), lines[i + 1]
            file, _, line = loc.rpartition(":")
            file = _clean(file)
            line = line.split(" ")[0]
            out[-1].append((fn, f"{file}:{line}" if file and line.isdigit() and line != "0"
                            else file))
        i += 2
    return out


def symbolize(raw: list[RawFrame], which=_symbolizer) -> list[tuple[RawFrame, list[tuple[str, str]]]]:
    """For each raw frame, its (function, file[:line]) entries, innermost inline first.
    Return addresses (every frame but 0) are looked up at pc-1, the call instruction."""
    tool = which()
    looked: dict[int, list[tuple[str, str]]] = {}
    by_mod: dict[str, list[tuple[int, int]]] = {}
    for n, f in enumerate(raw):
        if f.module and os.path.isfile(f.module):
            by_mod.setdefault(f.module, []).append((n, f.rel - (1 if f.index > 0 else 0)))
    for mod, items in by_mod.items():
        bias = _load_bias(mod)
        if bias is None or tool is None:
            continue
        addrs = [rel + bias for _, rel in items]
        try:
            res = (_run_llvm if tool[0] == "llvm" else _run_gnu)(tool[1], mod, addrs)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if len(res) != len(items):
            continue
        for (n, _), syms in zip(items, res):
            looked[n] = syms
    return [(f, looked.get(n) or []) for n, f in enumerate(raw)]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _module_kind(module: str) -> str:
    return Frame(0, "", "", module).kind() if module else "project"


def _is_shim(module: str) -> bool:
    return os.path.basename(module).startswith(_SHIM_MODULES)


def frames_for_hash(sym: list[tuple[RawFrame, list[tuple[str, str]]]],
                    ) -> tuple[list[Frame], list[str]]:
    """Symbolized frames (as detectors.Frame) plus printable lines. Frames of shims are
    dropped. In a system library the module decides the kind (libc has DWARF whose file
    names, such as stdlib/abort.c, would otherwise look like project files)."""
    frames: list[Frame] = []
    lines: list[str] = []
    for f, syms in sym:
        if f.module and _is_shim(f.module):
            continue
        off = hex(f.rel)
        mod = f.module or "?"
        sys_mod = _module_kind(f.module) != "project"
        for fn, loc in syms or [("", "")]:
            file = re.sub(r"(?::\d+)+$", "", loc)
            i = len(frames)
            frames.append(Frame(i, fn, "" if sys_mod else file, mod, off))
            lines.append(f"    #{i} {hex(f.pc)} in {fn or '??'}"
                         f"{' ' + loc if loc else ''} ({mod}+{off})")
    return frames, lines


def analyze(run_dir: Path, status: dict | None = None, which=_symbolizer) -> CrashTrace | None:
    """Read, symbolize and hash the crash trace for a run, or None when there is none."""
    st = status or {}
    path = find(Path(run_dir), st.get("pid"), st.get("signal"))
    if path is None:
        return None
    try:
        ct = parse(path.read_text(errors="replace"))
    except OSError:
        return None
    if not ct.raw:
        return ct
    ct.frames, ct.lines = frames_for_hash(symbolize(ct.raw, which))
    ct.stack_hash = stack_hash(ct.frames)
    return ct
