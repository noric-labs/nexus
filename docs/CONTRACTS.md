# Contracts

These are the interfaces shared between components built in parallel. If you change
something here, change every side of it.

## Repos and layout

- `noric-labs/nexus` (this repo):
  - `harness/`: lane launcher, faults, detectors (Python, uv-managed)
  - `scoreboard/`: Go service and embedded UI
  - `wall/`: zellij wall generator and pager
  - `lane/`: lane loadout, pi extension `game.ts`, `models.json`
  - `scaletest/`: inference probe and host-density ramp
- `noric-labs/torment-pkgs`: the Minimal sideload, with `packages/<name>/{build.ncl,build.sh}`.
  Recipes follow `gominimal/pkgs` conventions (see its AGENTS.md).

## Packages (torment-pkgs)

| Package | Provides |
| --- | --- |
| `libfiu` | `fiu-run`, `fiu-ctrl`, `/usr/lib/fiu_*_preload.so`, `libfiu.so` |
| `libfaketime` | `faketime`, `/usr/lib/faketime/libfaketime.so.1` |
| `torment-seed` | `/usr/lib/torment/libtorment_seed.so`. With `TORMENT_SEED=<u64>` set, it makes `getrandom`, reads of `/dev/urandom` and `/dev/random`, `rand`/`random`/`srand`, and `arc4random` deterministic. |
| `frotz` | `dfrotz` (dumb terminal) and `frotz` (curses) |
| `zork-stories` | `/usr/share/zork/zork1.z3`, `zork2.z3`, `zork3.z3` |
| `brogue-ce` | `brogue-ce` (terminal build) and `brogue-ce-asan` (ASan+UBSan build) |
| `greed` | `greed` |
| `dcss` | `crawl` (console build) and `crawl-asan` |

## Lane environment

The launcher reads these environment variables:

| Var | Example |
| --- | --- |
| `NEXUS_LANE_ID` | `l-042` |
| `NEXUS_GAME` | `brogue` \| `frotz-zork1` \| `greed` \| `dcss` |
| `NEXUS_FAULT` | a fault profile name (below) |
| `NEXUS_SEED` | `12345` (u64; used for both the game and the fault RNG). Games with narrower seeds get a reduced value: frotz `-s` takes an int where 0 means "use the clock", so it gets `seed % 2147483646 + 1`. |
| `NEXUS_MODEL` | `@cf/qwen/qwen3-30b-a3b-fp8` |
| `NEXUS_SCOREBOARD_URL` | `http://scoreboard.min.internal:8080` |
| `NEXUS_OUT` | `/home/out` (the lane writes its artifacts here; the box root is read-only) |

- The game runs in tmux session `game` at a fixed size: 80x24, or 100x34 for Brogue. Below
  100x34, Brogue only shows a resize prompt.
- Brogue writes saves, recordings and scores to its working directory, so it runs from
  `$HOME/brogue`.
- Curses frotz waits for one more key after `quit`.
- tmux is started with `-f harness/tmux.conf`. It sets `window-size manual` and a per-game
  `default-size`, so viewers attaching never resize the game. It also turns off the status line
  (so the game keeps all 24 rows) and sets `remain-on-exit` (so restarts keep the session).
- The pi agent drives the game through `tmux send-keys` and `tmux capture-pane`.

## Fault profiles

Each profile is a launcher plugin that wraps the game command:

| Profile | Mechanism |
| --- | --- |
| `none` | control lane |
| `malloc-1pct`, `malloc-0.1pct` | `fiu-run -x -c 'enable_random name=libc/mm/*,probability=P'`; `FIU_PRNG_SEED` is derived from the seed |
| `io-save-5pct` | libfiu `posix/io/rw/*` at 0.05 and `posix/io/oc/*` at 0.02 |
| `strace-enospc-save` | `strace -f --seccomp-bpf -o /dev/null -e trace=write,openat -e inject=write:error=ENOSPC:when=N+K` |
| `strace-eintr-read` | `-e inject=read:error=EINTR:when=N+7` |
| `winch-storm` | a seeded SIGWINCH plus tmux resize schedule every 50–500 ms |
| `rlimit-tight` | `prlimit --as=268435456 --nofile=16 --fsize=65536 --stack=262144 --` |
| `time-warp` | `faketime -f "@2038-01-19 03:14:00 x60"` |
| `keystorm` | every 20 agent keys, inject one hostile byte sequence (radamsa-mutated pool) |

## Event schema v1 (lane → scoreboard)

One JSON object per line:

```json
{"v":1,"event_id":"<uuid>","ts":"2026-10-21T12:00:00.123Z","heat_id":"h-0007",
 "lane_id":"l-042","game":"brogue","game_ver":"1.14","fault":"malloc-1pct",
 "seed":12345,"model":"@cf/qwen/qwen3-30b-a3b-fp8",
 "type":"crash|error_exit|sanitizer|hang|visual|agent_error|heartbeat|lane_start|lane_end",
 "signal":11,"exit_code":null,"stack_hash":"a1b2c3d4e5f6","detail":"heap-use-after-free",
 "turn":812,"tokens_in":4100000,"tokens_out":120000,"replay":"/out/replay/<event_id>.tar.zst"}
```

- `stack_hash` is the first 12 hex chars of sha1 over the top 5 in-project frames, as
  `func@file`, with libc, ncurses, asan and ubsan frames dropped. If the top frame is in
  ncurses, the hash is prefixed `ncurses:`.
- Heartbeats go out every 5 s. Fields other than identity and `turn`/tokens are optional on
  heartbeats.

## Scoreboard HTTP API (port 8080)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/events` | NDJSON body. Returns 202. Idempotent on `event_id`. |
| POST | `/lanes/{id}/screen` | JSON body `{"ts","cols","rows","cursor":[x,y],"lines":[...]}`; lanes send at most 2/s |
| GET | `/stream` | SSE. Event names: `state`, `failure`, `heat`, `pit`, `ticker`, `focus`. Supports `Last-Event-ID`. |
| GET | `/lanes/{id}/screen/stream` | SSE of screen frames for one lane |
| GET | `/api/state` | JSON snapshot: heat, lanes, scores, standings |
| GET | `/api/lanes/{id}` | config card and failure log |
| POST | `/wall/focus` | `{"lane_id"}`; rebroadcast as the SSE `focus` event, consumed by `wall-pager` |
| GET | `/` | race UI |

**Heats.** A heat lasts 15 min, then a 1-min pit. Durations and weights are configurable in
`scoreboard.toml`:

- During the pit, `pit` is broadcast and `lane/recycle` is expected to restart lanes.
- The next heat's `heat_id` arrives in the `heat` event. Lanes read it from
  `GET /api/state` at start.

**Scoring.**

```
score(lane, heat) = Σ weight[type] × 0.5^(n−1)
```

- n is the hit count of `(lane, stack_hash)` in the heat. Hangs and visual corruption with no
  stack hash use `type+detail` as the key.
- Default weights: `sanitizer=5`, `crash=3`, `hang=2`, `error_exit=1`, `visual=1`, `agent_error=0`.
- `error_exit` is a nonzero exit without a signal: the game noticed the fault and quit.
  Exits on SIGINT or SIGQUIT come from agent keystrokes and are reported as `agent_error`.
- Season points by heat finish position: 25, 18, 15, 12, 10, 8, 6, 4, 2, 1.

## Scoreboard behaviours (as implemented)

- **Heat ids:** formatted `h-%04d` and they continue across restarts.
  `/api/state.heat` = `{id, seq, phase:"race"|"pit", started_at, ends_at, pit_ends_at, next_id, remaining_ms}`.
  A lane that starts during the pit uses `next_id`.
- **When a failure scores:** an empty `heat_id` means the current heat. A failure scores only
  if it belongs to the current heat and arrives during the race, or during the pit with
  `ts <= ends_at`. Anything else is stored but not scored.
- **Decay key:** `stack_hash`, or `type|detail` when there is no hash. A stack hash shares its
  hit count across failure types. The cluster id is `<game>/<key>`. `agent_error` is never a
  cluster.
- **Season points:** awarded when the pit ends, only to lanes with a score above 0. Ties are
  broken by score, then by who reached it first, then by lane id.
- **Heartbeats** are not stored.
- **SSE payloads:**
  - `failure` = the event plus `cluster, n, points, scored, new_cluster, lane_score`.
  - `ticker` = `{kind: first_blood|new_cluster|lead_change, text, lane_id, heat_id, ts}`.
  - `pit` = `{heat, heat_id, next_id, podium}`.
  - `focus` = `{lane_id, ts}`.
- **`state` events:** every connection gets a full `state` first. Later `state` deltas list
  only the lanes that changed, but always carry complete scores and standings. They are not
  replayed.
- **Status codes:** `POST /events` returns 400 only when no line is valid.
  `POST /lanes/{id}/screen` and `POST /wall/focus` return 202.

## Sanitizer lanes

- Always set `log_path` in `ASAN_OPTIONS` and `UBSAN_OPTIONS`. Without it, reports go to
  stderr and corrupt the tmux screen.
- `crawl-asan` emits about 11 UBSan reports before the first turn, from `worley.cc`,
  `skills.cc`, `enum.h` and sqlite. Treat them as baseline stack hashes that score 0.
- Put `llvm-symbolizer` on PATH, or set `ASAN_SYMBOLIZER_PATH`, so that frames symbolize to
  `func@file` for `stack_hash`.

## Agent ↔ harness

- **Keys:** `nexus-lane keys "<keys>"` takes literal text plus named keys: `<Enter>`, `<Esc>`,
  `<Tab>`, `<BS>`, `<Space>`, the arrows `<Up>` `<Down>` `<Left>` `<Right>`, and `<C-x>` for
  Ctrl. The grammar is `lane/game.ts`.
- **Screen:** `nexus-lane screen` prints plain screen text. The cursor is read from tmux.
- **Stats:** the agent writes `$NEXUS_OUT/agent/stats.json`
  (`turn, tokens_in, tokens_out, actions, keys_sent, capped`). Heartbeats copy
  `turn` and the token counts from it.
- **Errors:** agent errors are appended to `$NEXUS_OUT/agent/events.ndjson`. The harness
  forwards them as `agent_error`.
- **Model endpoint:** pi uses `lane/models.json` (provider `nexus`), base URL
  `http://100.64.255.254:8899/workers-ai/v1` (the broker). Tests use `lane/mock_openai.py`.

## Harness MVP scope (M1)

- **Fault profiles** are defined in `harness/faults.toml` (`NEXUS_FAULTS_FILE` overrides it).
  The kinds are `none`, `libfiu` and `winch-storm`. A libfiu profile sets `points`,
  `probability` and `delay_s`. With `delay_s > 0`, the game starts under `fiu-run -x` and the
  points are enabled through `fiu-ctrl` once the delay has passed. That skips the startup
  crashes, which all look the same. An unknown profile ends the lane with exit 2.
- **Crash events** carry no `stack_hash` yet, because there is no core analysis.
- **Stack hash:** frames use the file basename, and libstdc++, libgcc and the loader are
  dropped along with libc.
- **Hangs** cannot be detected under `winch-storm`, because the screen keeps changing.
