"""Builds a realistic lane turn: system prompt, the two game tools, an 80x24 screen and
recent actions, padded to roughly the target input-token count (default ~5k)."""

from __future__ import annotations

import json
import random

COLS, ROWS = 80, 24

SYSTEM_PROMPT = """\
You are a racing agent in Torment Nexus. You play a terminal game running in an 80x24
tmux pane. The game binary runs under fault injection (failed mallocs, short writes,
EINTR, SIGWINCH storms, clock warps, hostile input bytes), so it may crash, hang, draw
garbage, or behave strangely. Your job is to keep the game moving and to exercise as much
of its code as you can: explore, open menus, save and load, resize-sensitive screens,
inventory, help pages, unusual commands and edge-case input. You score points when the
game fails, so prefer actions that reach new code paths over actions that merely win.

You have exactly two tools and no shell:

- send_keys(keys): type a key sequence into the game, as one string. Batch several moves
  per call, up to about 5, so each request makes progress. Plain text is typed literally;
  named keys use angle brackets: <Enter>, <Esc>, <Tab>, <BSpace>, <Space>, <Up>, <Down>,
  <Left>, <Right>, <C-x> for ctrl, <F1>..<F12>.
- screen(): return the current 80x24 screen. You already receive the screen with every
  turn, so call it only if you believe the screen changed without your input.

Rules:
1. Every reply must be exactly one tool call. Do not write prose.
2. Never send C-c or C-z unless the game has been stuck on the same screen for 5 turns.
3. If you see a prompt asking to confirm quitting, answer n.
4. If the screen is blank or garbled, send Escape then Space, and keep going.
5. Vary your play: do not repeat the same key sequence more than 3 turns in a row.
6. If the game asks for a name, type a short one and press Enter.
7. Read the status line before acting; avoid dying early, a living game reaches more code.

The recent-actions log shows your last moves with a one-line note of what changed. Use it
to avoid loops. The screen below is the current state after your last action.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_keys",
            "description": (
                "Type a key sequence into the game, then return the updated screen. Batch "
                "several keys per call (e.g. 'jjjl' or 'open mailbox<Enter>'). Plain text is "
                "typed literally; named keys use angle brackets: <Enter> <Esc> <Tab> <BSpace> "
                "<Space> <Up> <Down> <Left> <Right> <C-x> (ctrl). Mirrors lane/game.ts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "Key sequence, e.g. 'look<Enter>' or '<Down><Down><Enter>'",
                    }
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screen",
            "description": "Return the current 80x24 screen of the game pane as text.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_MOVES = list("hjklyubn") + ["<Up>", "<Down>", "<Left>", "<Right>", "<Enter>", "<Esc>", "<Space>", "i", "s", ">", "<lt>", "g", "x", "?"]
_NOTES = [
    "moved; nothing new in view",
    "opened inventory, closed it",
    "picked up an item",
    "a monster came into view to the east",
    "door opened, corridor beyond",
    "searched; found nothing",
    "message log scrolled",
    "descended the stairs, new level drawn",
    "help screen shown, dismissed",
    "status line: HP dropped by 3",
    "menu prompt appeared and was cancelled",
    "screen unchanged after input",
]


def make_screen(rng: random.Random) -> str:
    """A Brogue/rogue-like 80x24 screen: message line, map with rooms, status line."""
    grid = [[" "] * COLS for _ in range(ROWS)]
    for _ in range(rng.randint(3, 6)):
        w, h = rng.randint(8, 22), rng.randint(4, 8)
        x, y = rng.randint(0, COLS - w - 1), rng.randint(1, ROWS - h - 3)
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                edge = yy in (y, y + h - 1) or xx in (x, x + w - 1)
                grid[yy][xx] = "#" if edge else "."
        grid[y + h // 2][x] = "+"
        for _ in range(rng.randint(0, 4)):
            grid[rng.randint(y + 1, y + h - 2)][rng.randint(x + 1, x + w - 2)] = rng.choice("!?$%)[]=/*kgoBj")
    for _ in range(rng.randint(10, 40)):
        grid[rng.randint(1, ROWS - 3)][rng.randint(0, COLS - 1)] = rng.choice(".#")
    grid[rng.randint(2, ROWS - 4)][rng.randint(2, COLS - 3)] = "@"
    msg = rng.choice(["You hear a distant clank.", "The goblin conjurer hits you.", "You see a scroll of teleportation.", "Welcome to the Dungeons of Doom!"])
    lines = ["".join(r) for r in grid]
    lines[0] = msg.ljust(COLS)[:COLS]
    lines[ROWS - 2] = ("Depth: %d  Str: 12  HP: %d/40  Gold: %d  Turn: %d" % (rng.randint(1, 9), rng.randint(5, 40), rng.randint(0, 900), rng.randint(100, 9000))).ljust(COLS)
    lines[ROWS - 1] = "[i]nventory [s]earch [z]rest [>]descend [?]help".ljust(COLS)
    return "\n".join(lines)


def _action_line(rng: random.Random, turn: int) -> str:
    keys = [rng.choice(_MOVES) for _ in range(rng.randint(1, 5))]
    return "turn %d: send_keys(%s) -> %s" % (turn, json.dumps("".join(keys)), rng.choice(_NOTES))


def estimate_tokens(obj) -> int:
    """Rough tokenizer-free estimate: ~4.2 chars per token over the JSON body (calibrated to
    o200k_base: a 5000-token estimate measured ~5.0k real tokens)."""
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return int(len(s) / 4.2)


# Per-model request extras that turn hidden reasoning off (or down): with it on, these
# models spend the whole output budget reasoning and never reach the tool call.
# Mirrors samplingParams in lane/models.json.
MODEL_EXTRA = {
    "@cf/openai/gpt-oss-20b": {"reasoning_effort": "low"},
}
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


def model_extra(model: str) -> dict:
    return MODEL_EXTRA.get(model, NO_THINKING)


def build_request(model: str, rng: random.Random, target_tokens: int = 5000, max_tokens: int = 200) -> dict:
    screen = make_screen(rng)
    prev = make_screen(rng)
    turn = rng.randint(200, 2000)
    actions: list[str] = []

    def body() -> dict:
        user = (
            "Game: brogue  Fault: malloc-1pct  Lane: l-042  Turn: %d\n\n"
            "Recent actions (oldest first):\n%s\n\n"
            "Previous screen:\n```\n%s\n```\n\n"
            "Current screen (80x24):\n```\n%s\n```\n\n"
            "Choose your next keys." % (turn, "\n".join(actions), prev, screen)
        )
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": 0.7,
            **model_extra(model),
        }

    # Grow the recent-actions log until the body reaches the target size.
    b = body()
    while estimate_tokens(b) < target_tokens and len(actions) < 2000:
        for _ in range(10):
            actions.append(_action_line(rng, turn - 400 + len(actions)))
        b = body()
    return b
