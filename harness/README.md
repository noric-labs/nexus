# harness: `nexus-lane`

The lane launcher for the Torment Nexus. It runs one game in tmux session `game` under one fault
profile, detects failures (crash, sanitizer, hang, visual corruption), reports them to the scoreboard as schema-v1
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
| `NEXUS_VISUAL` | 1 | visual-corruption detector (0 turns it off) |
| `NEXUS_BLANK_SECS` | 2 | visual: blank screen this long after a non-blank one |
| `NEXUS_RESIZE_QUIET_SECS` | 0 | visual: skip the cursor, bad-cell and blank checks this long after a winch-storm resize |
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
  `$HOME/brogue`), `sanitizer`, `baseline_hashes`, `ok_exit_codes`, and for the visual detector
  `status_regex` (Brogue `Depth:`, DCSS `Health:`) with `status_secs` (default 30). The entries are
  `frotz-zork1`, `brogue`, `greed`, `dcss`, `stub-crasher`, `stub-asan`, `stub-hang` and
  `stub-glitch` (prints garbage, blanks the screen or hides its status line on command; see
  `stubs/glitch.c`). The real-game
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
    `SIGWINCH` to the game. Every other step goes back to the base size.
  - `strace-enospc-save` and `strace-eintr-read` prefix the game with `strace -f --seccomp-bpf -o
    /dev/null -e trace=... -e inject=SYSCALL:error=ERRNO:when=N+K --`. N and K are drawn from the
    seed within the profile's `first`/`step` ranges. strace exits with the game's exit code and
    re-raises its fatal signal, so classification is unchanged. LD_PRELOAD (crashtrace, seed, inherited)
    is handed to the game with `strace -E` and removed from strace's own environment, so the
    crashtrace handler never records strace itself. Before the lane starts, `strace` traces `true`
    once; if strace is missing or ptrace/seccomp is denied, the lane ends with `lane_end` and exit 2.
  - `rlimit-tight` prefixes `prlimit --as=268435456 --nofile=16 --fsize=65536 --stack=262144 --`.
    Without prlimit, the supervisor calls `setrlimit` in the game process (soft = hard). The 256 MB
    address-space cap breaks ASan builds, so use release builds.
  - `time-warp` preloads `/usr/lib/faketime/libfaketime.so.1` with `FAKETIME="@2038-01-19 03:14:00
    x60"` (not the `faketime` wrapper, which would replace LD_PRELOAD). If the library is missing,
    the lane ends with exit 2.
  - `keystorm`: every 20 agent keys (`@nexus_keys`), one hostile sequence from a seeded pool is
    written with `tmux send-keys -H`: truncated ESC/CSI/OSC, a lone 0x9b, NUL, invalid UTF-8,
    out-of-range SGR/X10 mouse reports, or a 4 KB bracketed paste. A third of the short ones get one
    byte mutated. The pool never contains `^C ^\ ^Z ^Q ^S ^O ^V`, which the tty would turn into
    signals or flow control. Injected bytes are not counted as agent keys. Each injection is
    logged in `runs/NNNN/keystorm.log`, which is also added to replay bundles.
  - Any other profile name ends the lane the same way (`lane_end`, exit 2).
  - The supervisor restores the default SIGPIPE and SIGXFSZ actions before exec. Python ignores
    both, and an ignored signal stays ignored across exec, so without this a game could never die
    from them.
  - If `/usr/lib/torment/libtorment_seed.so` exists, the game also gets it in `LD_PRELOAD`, with
    `TORMENT_SEED` set.
- **Detectors**:
  - Crash: the game died from a signal, or its exit code is not in `ok_exit_codes`. The event's
    `detail` is `SIGSEGV`, or `exit N` for an exit code. If `/usr/lib/torment/libtorment_crashtrace.so`
    exists (or `NEXUS_CRASHTRACE_LIB` points at it; `none` turns it off), it is preloaded with
    `TORMENT_CRASHTRACE_DIR=<run_dir>`. After a signal death `crashtrace.py` reads
    `crash-<pid>.txt`, symbolizes it (llvm-symbolizer, else addr2line) and sets `stack_hash`; the
    symbolized stack goes into the replay bundle as `report.txt`. Without a trace the
    scoreboard clusters crashes by `crash|detail`.
  - Sanitizer: `ASAN_OPTIONS`/`UBSAN_OPTIONS` get `log_path=<run_dir>/{asan,ubsan}` (plus
    `print_stacktrace=1`, `detect_leaks=0` and `verify_asan_link_order=0`). The logs are polled every
    second, and each report is sent once its `SUMMARY` line appears. An ASan abort is reported as
    `sanitizer` only, not also as `crash`.
  - Hang: the watchdog described under Environment. After a hang the game is SIGKILLed (with its
    descendants, e.g. the game under strace) and restarted. Under `winch-storm` the screen keeps
    changing, so hangs are not detected there.
  - Visual (`detectors.VisualDetector`): this runs on each screen capture while the game is alive.
    The `detail` is the condition:
    - `cursor-outside-pane`: x > cols or y >= rows. x == cols is tmux's normal pending-wrap state
      after writing the last column.
    - `bad-cells`: U+FFFD or a control character in a cell. tmux stores invalid UTF-8 as U+FFFD and
      C1 controls as cells, and it interprets C0 bytes instead of storing them.
    - `blank-screen`: every cell is blank for more than 2 s after a non-blank frame.
    - `status-missing`: `status_regex` has been off screen for `status_secs` after it first
      appeared in the run.

    Each condition is reported at most once per run, with no stack hash, so the scoreboard key is
    `visual|<condition>` per game.

    Under `winch-storm` (12 s per stub, 3 seeds each, about 110 captures per run) the stubs
    produced no hits. That included `stub-glitch`, which redraws on SIGWINCH with box and wide
    glyphs. tmux reflow produced no U+FFFD and never put the cursor outside the pane. So the
    resize quiet window (`NEXUS_RESIZE_QUIET_SECS`) defaults to off. A window of 1 s would turn
    these checks off for the whole storm, because resizes come every 50–500 ms.
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
