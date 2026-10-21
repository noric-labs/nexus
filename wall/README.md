# wall

The zellij wall for the Torment Nexus. It runs on the Mac host, outside the sandboxes,
and shows every lane's `game` tmux session read-only. There are three entry points (the
project uses only the Python stdlib):

| Command | What it does |
| --- | --- |
| `wall-gen` | Reads lanes from the scoreboard's `GET /api/state` or from a lanes file. It writes one zellij layout per page, plus `wall.kdl` (all pages and the Hot tab), `hot.kdl`, `hot/slot-NN` and `index.json`. |
| `wall-pager` | Runs alongside zellij. It cycles the tabs, follows `failure` events on `GET /stream` into the Hot tab, and jumps to a lane on `focus` events. |
| `wall-slot` | Runs inside each Hot-tab pane. It executes the command in its slot file and restarts it whenever the file changes. |

## Pages

- Lanes are grouped by terminal size, then sorted by game and lane id. 80x24 lanes go on
  8x8 pages and Brogue (100x34) lanes go on 6x6 pages. Tabs are named like `80x24 1/2`.
  The last page of each size is padded with empty panes, so every pane on a page is the
  same size. A size whose lanes fit on one page gets the smallest near-square grid that
  holds them instead (3 lanes: 2x2), so a small run gets big panes.
- **One page.** `wall-gen --one-page` (or `host-up.sh --one-page`) puts every lane, up to 64,
  on a single tab sorted by game, in the smallest grid that holds them, with no Hot tab.
  Mixed sizes share the tab, so each game is clipped or padded to its pane. Run no
  `wall-pager` with it: there is nothing to cycle.
- Each pane is named `lane · game · fault` (for example `l-042 · brogue · malloc-1pct`).
- The layouts have no `default_tab_template` and no plugin panes, so there is no tab bar
  and no status bar. `config.kdl` turns pane frames on, starts in locked mode and turns off
  tips and session serialization.
- **Recycled lanes.** zellij's `close_on_exit=false` only holds an exited pane and waits
  for ENTER to rerun it; zellij 0.45 has no option or action to rerun automatically. So
  every page pane runs `sh -c 'while :; do <cmd>; sleep 3; done'` (`--retry`), and
  `close_on_exit=false` is still set. When a lane is destroyed, the pane shows
  `[l-042: detached, retrying in 3s]` and re-attaches once the lane is back.
- **Hot tab.** This is a fixed grid of `wall-slot` panes: `--hot-size` of them, by default
  the lane count up to 64 (8x8), sized the same way as a one-page grid. When a lane fails that is
  not already in Hot, `wall-pager` points the stalest slot at it (or an empty slot, if
  there is one). It does this by atomically rewriting `out/hot/slot-NN` and renaming that
  pane, so only that one pane re-attaches. The set is always the 64 lanes that failed most
  recently. It is seeded from `last_failure` in `/api/state` when the pager starts.
  `agent_error` failures are ignored.
- **Focus.** On a `focus` event, the pager runs `go-to-tab-name <page>`, then
  `focus-pane-id`, and tints the pane's background (`--highlight`). It holds there for
  `--hold` seconds (30 by default) with cycling paused. `--zoom` also fullscreens the pane
  while it is held. If the lane is not on any page but is in Hot, the pager jumps to it
  there.
- **Re-titling.** If a lane comes back from recycling with a different game or fault, the
  pager renames its panes from `state` events. **Run `wall-gen` again** when the set of
  lanes changes.

## Pane command template

`--cmd` is a shell template. It can use these fields, each shell-quoted:

| Field | Value |
| --- | --- |
| `{session}` | the session name, from `--session-fmt` (default `lane-{num}`) |
| `{lane_id}` | for example `l-042` |
| `{num}` | `42` |
| `{digits}` | `042` |
| `{game}` | the lane's game |
| `{fault}` | the lane's fault profile |

The default is:

```
min session exec {session} 'exec python3 /workbench/lane/ttyview.py'
```

`min session exec` gives the command no terminal, so a plain `tmux attach -r` fails
there with "not a terminal". `lane/ttyview.py` opens a pty inside the box and streams the
read-only attach through it. This is the command `scripts/host-up.sh` passes. For a local
demo without Minimal, use `--cmd 'tmux -L {session} attach -r -t game'`.

Keep the template to a single command. `wall-slot` runs it with `exec`, so stopping the
slot stops the attach client.

## Run on the Mac

```sh
cd wall
uv run wall-gen --scoreboard http://<scoreboard>:8080 --out out
ulimit -n 4096                                     # ~164 panes, each a pty; macOS terminals default to 256 fds
zellij delete-session nexus-wall 2>/dev/null
zellij --config config.kdl -s nexus-wall -n out/wall.kdl                    # in the fullscreen wall terminal, not inside zellij
uv run wall-pager --session nexus-wall --scoreboard http://<scoreboard>:8080 --out out   # in any other terminal
```

`wall-pager` options:

- `--interval` sets the seconds per tab (20 by default). `0` turns cycling off.
- `--dry-run` logs the zellij actions instead of running them.
- `--no-stream` only cycles the tabs.

`wall-gen --lanes-file f.json` accepts either an `/api/state` snapshot or a list of lane
objects. A single page can also be opened on its own with
`zellij --config config.kdl -n out/page-03.kdl`.

## Local demo

```sh
cd wall
./demo/demo.sh up 70     # 70 local tmux servers: socket lane-N, session "game", toy.sh inside
                         # (64 lanes at 80x24, 6 Brogue lanes at 100x34);
                         # writes demo/out/lanes.json and the layouts, with
                         #   --cmd 'tmux -L {session} attach -r -t game'
zellij --config config.kdl -s nexus-wall -n demo/out/wall.kdl
./demo/demo.sh down 70
```

To drive the pager, start a scoreboard and post `lane_start` and `crash` events for
`l-001`…`l-070`. Then run `wall-gen --scoreboard …`, start the pager, and send
`POST /wall/focus {"lane_id":"l-033"}`.

## Screen and font (MacBook Pro 16")

The panel is 3456x2234 device pixels. Retina scale is 2x, so a cell of 5x10 device pixels
is 2.5x5 points.

| Grid | Panes | Cells needed (with frames) | At 5x10 px |
| --- | --- | --- | --- |
| 80x24 | 8x8 of 82x26 | 656x208 | 3280x2080 px |
| 100x34 (Brogue) | 6x6 of 102x36 | 612x216 | 3060x2160 px |
| Whole screen, non-native fullscreen | | 691x223 | |
| Whole screen, native fullscreen (below the notch, about 2160 px tall) | | 691x216 | |

- **Use non-native fullscreen.** Native fullscreen leaves the Brogue pages with no spare
  rows. Non-native fullscreen gives 7 more rows.
- **Font size.** Use about 4 pt Menlo or SF Mono with zero padding. Most monospace fonts at
  4 pt land within about a pixel of 5x10. Then fine-tune the cell size:
  - Ghostty: `adjust-cell-width` and `adjust-cell-height`
  - kitty: `modify_font cell_width/cell_height`
  - WezTerm: `cell_width` and `line_height`
- **Configs.** Ready-made configs are in `terminals/`: `ghostty.conf`, `kitty.conf` and
  `wezterm.lua`.
- **Check the grid.** In the wall terminal, run `echo $(tput cols)x$(tput lines)`. You need
  at least 656x208 for the 8x8 pages and 612x216 for the 6x6 pages. If either is short,
  shrink the font slightly. zellij splits the extra space between the panes, and tmux fills
  the unused area with `·`.

## Troubleshooting

- **A plain shell instead of the wall.** You started it inside an existing zellij session,
  or reattached to an old `nexus-wall` session. Run
  `zellij kill-session nexus-wall; zellij delete-session nexus-wall --force` first, from a
  terminal that is not inside zellij (`$ZELLIJ` unset).
- **"There is no active session!" and an immediate exit.** You used `--layout` instead of
  `-n`. In zellij 0.45, `--layout` loads into an existing session.
