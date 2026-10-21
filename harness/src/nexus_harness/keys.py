"""Agent key notation, mirrored from lane/game.ts `parseKeys` (keep the two in sync).

Plain text is typed literally; named keys go in angle brackets: <Enter> <Esc> <Tab>
<BSpace> <Space> <Up> <Down> <Left> <Right> <Home> <End> <PgUp> <PgDn> <Del> <Ins>
<F1>..<F12> <C-x> (ctrl) <M-x> (alt) <C-M-x>, <lt> (a literal "<"). Unknown <...> is
typed literally. A literal newline is Enter; "\\r" is dropped.
"""

from __future__ import annotations

import re

NAMED = {
    "enter": "Enter", "ret": "Enter", "return": "Enter", "cr": "Enter",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "btab": "BTab",
    "bspace": "BSpace", "backspace": "BSpace", "bs": "BSpace",
    "space": "Space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "pgup": "PPage", "pageup": "PPage", "ppage": "PPage",
    "pgdn": "NPage", "pagedown": "NPage", "npage": "NPage",
    "del": "DC", "delete": "DC", "dc": "DC",
    "ins": "IC", "insert": "IC", "ic": "IC",
}
_TAG = re.compile(r"<([^<>\s]{1,12})>")


def parse_keys(s: str) -> tuple[list[tuple[str, str]], int]:
    """Returns (segments, key count); a segment is ("lit", text) or ("key", tmux key name)."""
    segs: list[tuple[str, str]] = []
    lit = ""
    count = 0
    i = 0
    while i < len(s):
        if s[i] == "<":
            m = _TAG.match(s, i)
            if m:
                raw = m.group(1)
                low = raw.lower()
                if low == "lt":
                    lit += "<"
                    count += 1
                    i = m.end()
                    continue
                key = None
                if low in NAMED:
                    key = NAMED[low]
                elif re.fullmatch(r"f([1-9]|1[0-2])", low):
                    key = low.upper()
                elif re.fullmatch(r"[cm]-.", raw, re.I | re.S):
                    key = f"{raw[0].upper()}-{raw[2]}"
                elif re.fullmatch(r"c-[cm]-.", raw, re.I | re.S):
                    key = f"C-M-{raw[4]}"
                if key:
                    if lit:
                        segs.append(("lit", lit))
                        lit = ""
                    segs.append(("key", key))
                    count += 1
                    i = m.end()
                    continue
        if s[i] == "\n":
            if lit:
                segs.append(("lit", lit))
                lit = ""
            segs.append(("key", "Enter"))
        elif s[i] != "\r":
            lit += s[i]
        count += 1
        i += 1
    if lit:
        segs.append(("lit", lit))
    return segs, count
