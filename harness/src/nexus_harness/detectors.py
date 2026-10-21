"""Failure detectors: sanitizer log parsing, stack hashing, crash classification and
the hang watchdog. Everything here is pure (no tmux, no network) so it is unit-testable.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal as _signal
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Stack frames and stack_hash
# ---------------------------------------------------------------------------

# "    #3 0xaaaa in func name (maybe with spaces) /path/file.c:12:5"
# "    #2 0xffff in __libc_start_main@GLIBC_2.17 (/usr/lib/libc.so.6+0x22ed4)"
# "    #0 0xffff  (/usr/lib/libfoo.so+0x1234)"
_FRAME_RE = re.compile(r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s*(?:in\s+(.*?))?\s*(\(\S+\)|\S+)?\s*$")
_MODULE_RE = re.compile(r"^\((?P<mod>[^+()]+)(?:\+(?P<off>0x[0-9a-fA-F]+))?\)$")
_LINECOL_RE = re.compile(r"(?::\d+)+$")

_SANITIZER_FUNCS = ("__asan", "__ubsan", "__sanitizer", "__interceptor_", "___interceptor_",
                    "__lsan", "__interception")
_SANITIZER_PATHS = ("compiler-rt/", "sanitizer_common", "/asan/", "/ubsan/", "/lsan/",
                    "/interception/", "libclang_rt", "libasan", "libubsan", "liblsan")
_LIBC_MODULES = ("libc.so", "libc-", "libm.so", "libpthread", "ld-linux", "ld-musl",
                 "libdl.so", "librt.so", "libstdc++", "libgcc_s", "libc++", "libc++abi",
                 "libunwind")
_LIBC_FUNCS = ("__libc_", "__GI_", "_dl_", "__restore_rt")
_LIBC_PATHS = ("sysdeps/", "libc-start.c", "/glibc", "/musl", "/csu/", "nptl/")
_NCURSES = ("ncurses", "libtinfo", "libpanel", "libform", "libmenu")


@dataclass(frozen=True)
class Frame:
    index: int
    func: str  # may be "" when unsymbolized
    file: str  # source path without :line:col, or ""
    module: str  # module path for unsymbolized frames, or ""
    offset: str = ""

    @property
    def where(self) -> str:
        return self.file or self.module

    def kind(self) -> str:
        """One of: sanitizer, libc, ncurses, project."""
        f, w = self.func, self.where
        if f.startswith(_SANITIZER_FUNCS) or any(p in w for p in _SANITIZER_PATHS):
            return "sanitizer"
        base = os.path.basename(w)
        if any(base.startswith(m) or m in base for m in _NCURSES) or "/ncurses" in w:
            return "ncurses"
        if (any(m in base for m in _LIBC_MODULES) or any(p in w for p in _LIBC_PATHS)
                or f.startswith(_LIBC_FUNCS) or f in ("_start", "start_thread", "clone")):
            return "libc"
        return "project"

    def key(self) -> str:
        """`func@file` with the file reduced to its basename (build dirs differ
        between hosts); unsymbolized frames become `+0xOFF@module`."""
        func = self.func or (self.offset and f"+{self.offset}") or "?"
        where = os.path.basename(self.where) or "?"
        return f"{func}@{where}"


_BUILDID_RE = re.compile(r"\s*\(BuildId: [0-9a-fA-F]+\)\s*$")


def parse_frame(line: str) -> Frame | None:
    m = _FRAME_RE.match(_BUILDID_RE.sub("", line))
    if not m:
        return None
    idx, func, loc = int(m.group(1)), (m.group(2) or "").strip(), (m.group(3) or "")
    module = offset = file = ""
    mm = _MODULE_RE.match(loc)
    if mm:
        module, offset = mm.group("mod"), mm.group("off") or ""
    elif loc:
        file = _LINECOL_RE.sub("", loc)
    if func.startswith("(") and not loc:  # "#0 0x.. (mod+off)" parsed as func
        mm = _MODULE_RE.match(func)
        if mm:
            func, module, offset = "", mm.group("mod"), mm.group("off") or ""
    return Frame(idx, func, file, module, offset)


def parse_frames(lines: list[str]) -> list[Frame]:
    """Frames of the first stack in `lines` (stops at the first gap after a stack)."""
    out: list[Frame] = []
    for ln in lines:
        fr = parse_frame(ln)
        if fr is None:
            if out:
                break
            continue
        if out and fr.index == 0:  # a second stack began
            break
        out.append(fr)
    return out


def stack_hash(frames: list[Frame], depth: int = 5) -> str:
    """CONTRACTS: first 12 hex chars of sha1 over the top `depth` in-project frames as
    `func@file` (joined with newlines), with libc, ncurses, asan and ubsan frames dropped.
    Prefixed `ncurses:` when the top frame (after sanitizer and libc frames) is in ncurses.
    Returns "" when there is nothing to hash."""
    kinds = [(f, f.kind()) for f in frames]
    top = next((k for f, k in kinds if k not in ("sanitizer", "libc")), None)
    keys = [f.key() for f, k in kinds if k == "project"][:depth]
    prefix = "ncurses:" if top == "ncurses" else ""
    if not keys and prefix:  # crash entirely inside ncurses: hash its frames instead
        keys = [f.key() for f, k in kinds if k == "ncurses"][:depth]
    if not keys:
        return ""
    return prefix + hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Sanitizer reports
# ---------------------------------------------------------------------------

_ASAN_ERR = re.compile(r"^==\d+==\s*ERROR: (AddressSanitizer|LeakSanitizer|MemorySanitizer|"
                       r"ThreadSanitizer|UndefinedBehaviorSanitizer): ?(.*)$")
_UBSAN_ERR = re.compile(r"^(?P<loc>\S+?):\d+(?::\d+)?: runtime error: (?P<msg>.*)$")
_SUMMARY = re.compile(r"^SUMMARY: \w+Sanitizer")
_NUMS = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+\b")


@dataclass
class SanitizerReport:
    tool: str  # asan | lsan | ubsan | ...
    detail: str
    frames: list[Frame]
    text: str
    fatal: bool
    complete: bool
    offset: int = 0  # line offset within the log file (identity for dedup)
    stack_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.stack_hash = stack_hash(self.frames)


def _asan_detail(tool: str, rest: str) -> str:
    if tool == "LeakSanitizer":
        return "memory-leak"
    word = rest.split(" on ")[0].split(" in ")[0].strip()
    word = word.split()[0] if word else "error"
    return word  # heap-use-after-free, SEGV, stack-buffer-overflow, ...


def _ubsan_detail(msg: str) -> str:
    head = msg.split(":")[0]
    head = _NUMS.sub("N", head)
    head = re.sub(r"'[^']*'", "T", head)
    words = head.split()[:10]
    return "ubsan: " + " ".join(words)


def parse_sanitizer_log(text: str, final: bool = False) -> list[SanitizerReport]:
    """Split a sanitizer log into reports. A report is complete once its SUMMARY line
    has been written (or when `final`, i.e. the process has exited)."""
    lines = text.splitlines()
    starts: list[tuple[int, str, str, bool]] = []
    for i, ln in enumerate(lines):
        m = _ASAN_ERR.match(ln)
        if m:
            tool = {"AddressSanitizer": "asan", "LeakSanitizer": "lsan",
                    "UndefinedBehaviorSanitizer": "ubsan"}.get(m.group(1), m.group(1).lower())
            starts.append((i, tool, _asan_detail(m.group(1), m.group(2)), True))
            continue
        m = _UBSAN_ERR.match(ln)
        if m:
            starts.append((i, "ubsan", _ubsan_detail(m.group("msg")), False))
    out = []
    for n, (i, tool, detail, fatal) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = lines[i:end]
        complete = any(_SUMMARY.match(b) for b in block) or n + 1 < len(starts) or final
        frames = parse_frames(block[1:])
        if not frames and tool == "ubsan":  # no print_stacktrace: use the error location
            m = _UBSAN_ERR.match(lines[i])
            frames = [Frame(0, "", m.group("loc"), "")] if m else []
        out.append(SanitizerReport(tool, detail, frames, "\n".join(block), fatal, complete, i))
    return out


class SanitizerWatcher:
    """Polls `<prefix>.<pid>` log files in a run directory and yields each report once."""

    def __init__(self, run_dir: Path, prefixes=("asan", "ubsan")):
        self.run_dir = Path(run_dir)
        self.prefixes = prefixes
        self.seen: set[tuple[str, int]] = set()

    def poll(self, final: bool = False) -> list[SanitizerReport]:
        new = []
        for p in sorted(self.run_dir.iterdir()) if self.run_dir.exists() else []:
            if not any(p.name.startswith(pre + ".") for pre in self.prefixes):
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for r in parse_sanitizer_log(text, final=final):
                k = (p.name, r.offset)
                if r.complete and k not in self.seen:
                    self.seen.add(k)
                    new.append(r)
        return new


# ---------------------------------------------------------------------------
# Crash classification
# ---------------------------------------------------------------------------

def classify_exit(status: dict, ok_exit_codes=(0,)) -> tuple[str, int | None, int | None] | None:
    """Map the supervisor's status record to (detail, signal, exit_code) for a crash
    event, or None for a normal exit."""
    sig = status.get("signal")
    code = status.get("exit_code")
    if sig:
        try:
            name = _signal.Signals(sig).name
        except ValueError:
            name = f"SIG{sig}"
        return name, sig, None
    if code is not None and code not in ok_exit_codes:
        if code > 128 and code - 128 < 65:  # shell-style "killed by signal" exit
            try:
                return _signal.Signals(code - 128).name, code - 128, code
            except ValueError:
                pass
        return f"exit {code}", None, code
    return None


# ---------------------------------------------------------------------------
# Hang watchdog
# ---------------------------------------------------------------------------

class HangWatchdog:
    """A hang is: the process is alive and the pane content (hash) has not changed for
    `secs` seconds, while at least `min_keys` agent key sends happened in that window
    (set min_keys=0 to flag any frozen screen)."""

    def __init__(self, secs: float, min_keys: int = 1):
        self.secs = secs
        self.min_keys = min_keys
        self.reset(0.0, 0)

    def reset(self, now: float, keys: int) -> None:
        self.last_hash: str | None = None
        self.changed_at = now
        self.keys_at_change = keys

    def observe(self, now: float, screen_hash: str, keys: int, alive: bool = True) -> bool:
        """Feed one capture. Returns True when the lane counts as hung."""
        if screen_hash != self.last_hash:
            self.last_hash = screen_hash
            self.changed_at = now
            self.keys_at_change = keys
            return False
        if not alive or self.secs <= 0:
            return False
        return (now - self.changed_at >= self.secs
                and keys - self.keys_at_change >= self.min_keys)

    def frozen_for(self, now: float) -> float:
        return now - self.changed_at
