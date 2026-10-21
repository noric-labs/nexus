# harness: `nexus-lane`

The lane launcher for the Torment Nexus. It runs one game in tmux session `game` under one fault
profile, detects failures (crash, sanitizer, hang), reports them to the scoreboard as schema-v1
events, and restarts the game with the next seed until it is stopped. Interfaces are defined in
[`docs/CONTRACTS.md`](../docs/CONTRACTS.md).

## Install and test

```sh
export UV_CACHE_DIR=/state/uv-cache
uv sync                       # creates .venv with the nexus-lane script and pytest
uv run nexus-lane build-stubs # compiles stubs/*.c into stubs/bin (needs cc/gcc + clang)
uv run pytest -q              # unit tests, plus integration tests (need tmux, clang, go)
```

The integration tests build `../scoreboard` with `go build` (or use `NEXUS_SCOREBOARD_BIN`), start it
on a random free port with a fresh DB and short heats, run lanes on private tmux sockets with the stub
games, drive them through `nexus-lane keys`, and check `/api/state` and `/api/lanes/{id}`.

## Run a lane

```sh
NEXUS_LANE_ID=l-001 NEXUS_GAME=greed NEXUS_FAULT=none NEXUS_SEED=12345 \
NEXUS_MODEL=@cf/qwen/qwen3-30b-a3b-fp8 NEXUS_SCOREBOARD_URL=http://scoreboard.min.internal:8080 \
NEXUS_OUT=/home/out uv run nexus-lane run
```

Stop it with SIGTERM or SIGINT: the game is killed, `lane_end` is sent, and the tmux session is
removed (set `NEXUS_KEEP_SESSION=1` to keep it). Without `NEXUS_SCOREBOARD_URL` the lane runs offline
and only writes the spool file.

Try it locally with a stand-in and a second terminal:

```sh
NEXUS_LANE_ID=l-001 NEXUS_GAME=stub-crasher NEXUS_OUT=/tmp/lane uv run nexus-lane run
uv run nexus-lane keys 'abc'   # the crasher segfaults on the third key, then restarts
uv run nexus-lane screen
```

### Agent-side helpers

- `nexus-lane keys <text>` sends keys in the agent notation shared with `lane/game.ts`. Plain text
  is typed literally. Named keys go in angle brackets: `<Enter> <Esc> <Tab> <BSpace> <Space> <Up>
  <Down> <Left> <Right> <Home> <End> <PgUp> <PgDn> <Del> <Ins> <F1>..<F12> <C-x> <M-x> <C-M-x>`, and
  `<lt>` is a literal `<`. A newline is Enter. Arguments are concatenated. Use `--raw` to pass
  tmux key names directly, or `-l` to type everything literally. Each call adds to the session's
  `@nexus_keys` (key count) and `@nexus_sends` (call count) options.
- `nexus-lane screen` prints the plain screen text. `--json` prints a screen frame in the
  `/lanes/{id}/screen` shape instead.

Both use `NEXUS_TMUX_SOCKET` (a `tmux -L` name) and `NEXUS_TMUX_TARGET` (the session, default
`game`) when they are set.

## Environment

Contract variables: `NEXUS_LANE_ID` and `NEXUS_GAME` are required. The others default as follows:
`NEXUS_FAULT=none`, `NEXUS_SEED=1`, `NEXUS_MODEL=""`, `NEXUS_SCOREBOARD_URL` unset (offline), and
`NEXUS_OUT=/home/out`.

Extensions:

| Var | Default | Meaning |
| --- | --- | --- |
| `NEXUS_HANG_SECS` | 60 | hang after the screen is unchanged this long (0 turns detection off) |
| `NEXUS_HANG_MIN_KEYS` | 1 | ...and at least this many keys were sent in that window (0 = any frozen screen) |
| `NEXUS_HEARTBEAT_SECS` | 5 | heartbeat period (the heat is refreshed at the same time) |
| `NEXUS_SCREEN_HZ` | 2 | screen frame rate, capped at 2 |
| `NEXUS_RECYCLE_ON_HEAT` | 1 | restart the game with the next seed when the heat id changes (pit) |
| `NEXUS_MAX_RUNS` | 0 | stop after N game runs (0 = never) |
| `NEXUS_TMUX_SOCKET` / `NEXUS_TMUX_TARGET` | default / `game` | tmux socket and session |
| `NEXUS_GAMES_FILE`, `NEXUS_TMUX_CONF`, `NEXUS_STUBS_DIR` | in this dir | config locations |
| `NEXUS_AGENT_STATS` | `$NEXUS_OUT/agent/stats.json` | pi stats: turn/tokens copied into heartbeats |
| `NEXUS_AGENT_EVENTS` | `$NEXUS_OUT/agent/events.ndjson` | agent errors, forwarded as `agent_error` |

## How it works

- **Games** are defined in `games.toml`: the command template (`{seed}`, `{seed32}` = `seed %
  2147483646 + 1` for frotz, `{stubs}`, `{home}`), version, fixed `size`, `cwd` (Brogue runs in
  `$HOME/brogue`), `sanitizer`, `baseline_hashes` and `ok_exit_codes`. The entries are
  `frotz-zork1`, `brogue`, `greed`, `dcss`, `stub-crasher`, `stub-asan` and `stub-hang`. The real-game
  command lines have not been run here, because those packages are not installed in this sandbox.
- **tmux**: the launcher starts the server with `-f tmux.conf` (manual window size, no status line,
  `remain-on-exit`), then sizes the window per game. Restarts reuse the session through
  `respawn-pane`, so viewers and the agent never lose it.
- **Supervisor** (`python -m nexus_harness.supervise <run_dir>`): this runs in the pane. It forks
  the game (fault prefix included) in the pane's foreground process group, ignores job-control
  signals itself, and writes `pid` and then `status.json` (exit code or signal, plus core flag and
  maxrss) to `$NEXUS_OUT/runs/NNNN/`.
- **Faults**:
  - `none`.
  - `malloc-1pct` and `malloc-0.1pct` use `fiu-run -x -c 'enable_random name=libc/mm/*,probability=P'`
    with `FIU_PRNG_SEED`. If `fiu-run` is missing, the lane sends only `lane_end` with the detail
    `fault malloc-1pct skipped: fiu-run not found ...` and exits 2.
  - `winch-storm` is a seeded schedule (50-500 ms apart) of `resize-window` to a random size, plus
    `SIGWINCH` to the game. Every other step goes back to the base size. Other contract profiles are
    rejected the same way (`lane_end`, exit 2).
  - If `/usr/lib/torment/libtorment_seed.so` exists, the game also gets it in `LD_PRELOAD`, with
    `TORMENT_SEED` set.
- **Detectors**:
  - Crash: the game died from a signal, or its exit code is not in `ok_exit_codes`. The event's
    `detail` is `SIGSEGV`, or `exit N` for an exit code. There is no `stack_hash` yet, so the
    scoreboard clusters these by `crash|detail`.
  - Sanitizer: `ASAN_OPTIONS`/`UBSAN_OPTIONS` get `log_path=<run_dir>/{asan,ubsan}` (plus
    `print_stacktrace=1`, `detect_leaks=0` and `verify_asan_link_order=0`). The logs are polled every
    second, and each report is sent once its `SUMMARY` line appears. An ASan abort is reported as
    `sanitizer` only, not also as `crash`.
  - Hang: the watchdog described under Environment. After a hang the game is SIGKILLed and
    restarted. Under `winch-storm` the screen keeps changing, so hangs are not detected there.
- **`stack_hash`**: this is the first 12 hex characters of the sha1 of the top 5 in-project frames.
  Each frame is written as `func@basename(file)`, and the frames are joined by `\n`. Frames from
  sanitizer runtimes, libc, the loader, libstdc++, libgcc and ncurses/tinfo are dropped. When the
  first frame that is not a sanitizer or libc frame is in ncurses, the hash gets the prefix
  `ncurses:`. An unsymbolized frame is written as `+0xOFF@module`. See `detectors.py`.
- **Events** are built with a uuid4 `event_id` and a millisecond UTC `ts`, and appended to
  `$NEXUS_OUT/events.ndjson`. A background thread POSTs them to `/events` as NDJSON batches,
  retrying with backoff from 0.5 s doubling to 30 s. `heat_id` comes from `GET /api/state`: the
  `id` during the race, and the `next_id` during the pit. It is refreshed with every heartbeat.
  Screen frames are posted to `/lanes/{id}/screen` at most twice a second when the screen changes,
  and at least every 5 s.
- **Replay**: each failure gets `$NEXUS_OUT/replay/<event_id>.tar.zst` (`.tar.gz` before Python 3.14),
  which holds the event, the screen, the run spec and status, and the sanitizer report.
