# Decision record

Newest first. Each entry gives the decision, why it was made, and what follows from it.
Commits are in the git history; the research is in the
[living doc](https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a).

## 2026-10-21

### D-030 Multi-VM topology: named VMs, a gateway per VM, `own_ip` lanes (proposed)
**Findings** from the `gominimal/minimal` source at 248b259 (filed as gominimal/inbox#1153):
- **Named VMs.** Sessions share one VM. More VMs come from `minvmd --vm NAME`, and
  `min --vm NAME` targets one. `exec` needs `--vm`; only `attach` finds a box's VM by
  itself. Box names must be unique machine-wide.
- **Host alias.** An `own_ip` box cannot reach the host alias `100.64.255.254` on any
  port. Only `host_ip` boxes can, today.
- **Cross-VM.** There is no box-to-box traffic between VMs: every VM uses the same
  100.64/16.
- **The `host-up.sh` hang.** `session exec` returns when its child exits. The Mac `min`
  waits for ssh stdout EOF, and an ssh ControlMaster/ControlPersist setting can hold
  that open. This is the suspect, not yet confirmed.

**Proposed topology** (needs user sign-off):
- **Per VM:** a `gw-<vm>` box on `host_ip` running a relay. Inference goes to the broker
  on the host alias; events and screens go to the scoreboard through the host alias at
  :8080, which is forwarded from the scoreboard box.
- **Lanes:** `own_ip`, each with its own IP. Egress is allowed only to the gateway's /32;
  no internet, and pi is vendored.
- **Scoreboard:** `own_ip` with `--ingress 8080`, forwarded to the Mac's `localhost:8080`.

**Risk:** the design depends on `host_ip` boxes keeping their host-alias access.

### D-029 First live mixed race: real resize bugs found; qwen3 and GLM preferred
6 live lanes, 5 min (`slice-mixed`, about $0.25), all four models.

| Lane | Model | Result |
| --- | --- | --- |
| bastet, winch-storm | GLM | ASan SEGV in ncurses `wnoutrefresh` via `Bastet::BorderedWindow::RedrawBorder()` (`Ui.cpp:78`): it redraws with stale geometry after a resize. Plus 3 visual. |
| nsnake-rel, winch-storm | Gemma | 2 distinct SIGABRT clusters in ncurses frames, 1 error_exit, 1 visual |
| brogue-rel, malloc-20pct | qwen3 | 18 crashes in 2 clusters (61 actions) |
| brogue-rel, malloc-20pct | Gemma | the same `0c484e81b05e` cluster: hashes are stable across models and runs |
| frotz, strace-eintr-read | gpt-oss | no failures (54 actions) |
| greed, rlimit-tight | qwen3 | no failures (60 actions) |

**Gemma:** 6 stall aborts across its two lanes, and only 13 actions in 5 min in the
Brogue one. That matches its slow p95 tail (D-027). qwen3 and GLM took 43–61 actions in
the same window.

**Decision:** qwen3 and GLM are the default lane models, gpt-oss is the third, and Gemma
gets few lanes.
**Note:** ncurses-namespaced hashes (`ncurses:…`) are keeping the library's crashes
apart from the games' own, as designed.

### D-028 All four models play live once the proxy normalises requests
**Problem:** the live toolfix check found qwen3 and gpt-oss failing with 400 "Bad input"
after the first turns. Workers AI's schema for those models rejects two things pi sends:
- OpenAI multi-part `content` arrays;
- `content: null` on assistant tool-call turns.

GLM and Gemma accept both.

**Fix:** `toolfix.py` now joins text-only part arrays into a string and sets null
assistant content to `""`. `NEXUS_TOOLFIX_DUMP` keeps rejected request bodies for
debugging.

**Result** (3 live lanes, 4 min each, about $0.25):

| Model | Actions | Structured tool calls | Upstream errors |
| --- | --- | --- | --- |
| qwen3 | 48 | 50/50 | 0 |
| glm-4.7-flash | 44 | 50/50 | 0 |
| gpt-oss | 43 | 88/93 (1 recovered) | 0 |

In pi's streaming mode the models mostly return structured calls, so text recovery is a
small safety net and request normalisation was the real fix. This revises D-016:
qwen3, glm and gpt-oss are usable, not just Gemma.

### D-027 Phase A results: no rate limit found up to 360 req/min per model
22,687 live requests, **$15.20 actual** (qwen3 $3.21, gpt-oss $11.42, glm $0.20, gemma
$0.38). The full table is in `docs/probe-phaseA-2026-10-21.txt`.

**Rate limits:**
- No 429s at any step, including A2: about 840 req/min across the account with qwen3 and
  gpt-oss each at 360.
- The limit looks per model, and the ceiling is above what we tested.

**Per model:**

| Model | Throughput | p95 latency | Errors | Tool calls |
| --- | --- | --- | --- | --- |
| qwen3 | 360 req/min | about 0.8 s | about 1% client timeouts (hung requests, see D-017) | needs toolfix |
| gpt-oss | 360 req/min | about 2 s | 3–8% 5xx at higher rates | needs toolfix |
| GLM | stopped at step 1 (60 req/min) | 6 s (A1), 15.7 s (A2) | — | — |
| Gemma | stopped at step 1 (60 req/min) | 5.8 s (A1), 7.6 s (A2) | — | 73–80% valid |

GLM and Gemma were stopped by the p95 > 5 s rule, not by rate limits. Their p50 stayed
at 1–2 s; the slow tail is what tripped the rule.

**Implications:**
- At 12 req/min per lane (one action per 5 s), qwen3 and gpt-oss each carry 30 or more
  lanes. The lane count is now bounded by host capacity (phase B) and toolfix quality,
  not by Workers AI.
- GLM and Gemma suit a few lanes each; their 6–16 s tails slow those lanes but the stall
  watchdog (60 s) covers them.
- The planned "about 25 lanes per model" (D-006) is conservative.

### D-026 AFL++ fuzz lanes deferred to the user
An attempt to delegate the M2 AFL++ fuzzing work was cut off by a safety classifier
before it started, and nothing was written. The item stays open until the user scopes it.

### D-025 DCSS ambient UBSan reports are baselined
`crawl-asan` emitted 13 distinct UBSan stack hashes in 2 min across 3 fault-free lanes,
about 50–66 points per lane.
**Decision:** every hash seen with `fault=none` counts as ambient noise and goes in
`baseline_hashes` (scored 0). Otherwise DCSS lanes would win every race without any fault
injection. New reports reached through play still score: the first rerun found a left
shift of a negative value in `chardump.cc:1358`, reached from melee combat.
**Also:** the DCSS character name is now per seed (`n{seed32}`). Lanes sharing one
`$HOME/.crawl` in a box had hit "game already being played" and exited 1.

### D-024 Race UI: canvas pixel art, no build step, no external assets
- **Format:** plain ES modules embedded in the Go binary, drawn on one 960x540 canvas
  scaled 2x for 1920x1080.
- **Font:** a hand-drawn bitmap font, so there's no third-party licence to track.
- **Demo:** `scoreboard/tools/fake_lanes.py` drives it with fake lanes.
- **Verification:** headless only (40/40 checks plus screenshots in `docs/img/`).
- **Still unchecked:** real-browser rendering, WebAudio and SSE reconnect. These need the
  user's second display.

### D-023 New fault kinds and the visual detector are verified in a 6-lane mock run
A 3-minute mock-model slice (`slice-new` task) with these lanes:

| Lane | Fault | Result |
| --- | --- | --- |
| bastet | winch-storm | 2 visual (cursor-outside-pane, blank-screen); Bastet has no SIGWINCH handling |
| nsnake-rel | strace-eintr-read | ran under strace with the preloads passed through; no failure in 3 min |
| pacman-rel | malloc-20pct | faults armed; exits 0, so nothing was scored |
| brogue | keystorm | no failure |
| greed | rlimit-tight | 1 hang (screen-frozen) |
| frotz | time-warp | 1 visual (bad-cells): a false positive, since dfrotz echoes arrow-key escape bytes |

**Decision:** a per-game `visual_ignore` list in `games.toml`; frotz ignores `bad-cells`.
**Also:** the supervisor bug that left SIGPIPE and SIGXFSZ ignored in games is fixed, and
the wall has size classes for `brogue-rel` and Pacman (29x32 on a 20x6 grid).

### D-022 Text tool-call recovery proxy (`lane/toolfix.py`), on by default
**Decision:** pi talks to a local proxy that forwards requests to the broker. When a reply
has no structured `tool_calls`, the proxy rebuilds one from the text. That covers each
shape seen in D-016: a `<tool_call>` block in content or reasoning, `functions.NAME {json}`,
`NAME(args)`, and a bare arguments object.
- Only tools the request declared are matched, and arguments are checked against the
  schema.
- It never invents a call.
- Every recovery is logged to `toolfix.jsonl`.

**Evidence (mock):** each failing shape went from 0 to about 30 actions per 15 s.
**Pending:** a live check on qwen3, glm and gpt-oss. It waits until the phase A probe
finishes, so live lanes don't distort the rate-limit measurements (the limits are per
model and per account).

### D-021 New games: Bastet, nSnake and Pacman4console are packaged and build in Minimal
All three ship a release and an ASan+UBSan build (`<game>` and `<game>-rel` in
`games.toml`). Each is real-time, so it stresses timing and hang detection more than
agent play.

| Game | Version | Min size | Randomness | Notes |
| --- | --- | --- | --- | --- |
| Bastet | 0.43.2 | 80x24 | `srandom(time)`, pinned by torment-seed | Boost 1.91 dropped `boost/tr1`, so the build adds a one-line header shim instead of patching the source. No SIGWINCH handling, so a good target for winch-storm. |
| nSnake | 3.0.1 | 80x24 | `srand(time)`, pinned by torment-seed | Exits 1 below 80x24. |
| Pacman4console | 1.3 | 29x32 | `srand(time)`, pinned by torment-seed | Busy-waits, so each lane uses a full core. Every exit is 0, so `error_exit` never fires. Its 29x32 size is a new wall size class. |

### D-020 Run the full phase A probe (user-approved, up to about $25)
**Scope:** all 4 models.
- Ramp: 60 → 360 requests/min, 5 min per step.
- Modes: A1 (each model alone), then A2 (all at once).
- Hard cap: 45,000 requests.
- The stop rule (429 > 1% or p95 > 5 s) ends each model's ramp early.

**Why all 4:** only Gemma makes valid tool calls today (D-016), but rate limits and
latency still apply to all of them, and the recovery shim may bring the other three back.
**Output:** the measured ceiling sets the lane count (PLAN, scale test).

### D-019 Overnight autonomy: no large live inference spend
Work continues unattended overnight. Live Workers AI runs are limited to small validation
slices (under about $0.25 each), except for the phase A probe, which the user approved
(D-020).

### D-018 Add Bastet, nSnake and Pacman4console
These are requested by the user. All three are real-time terminal games, so they mainly
stress timing, resize (SIGWINCH) and hang detection. An LLM acting every 5 s plays them
poorly.

### D-017 Stall watchdog in the agent driver
**Problem:** a live Gemma run froze the Zork lane after 7 turns. A provider request hung
with no error.
**Decision:** if `stats.json` is not updated for 60 s (`NEXUS_STALL_SECS`), `agent.py`
aborts the run and restarts pi if needed.
**Result:** Zork went from 7 turns to 56 in the same 4 minutes.

### D-016 Gemma 4 is the working live model; the other three need tool-call recovery
**Finding:** through Workers AI (smoke test, about $0.05 total), only
`@cf/google/gemma-4-26b-a4b-it` returns structured `tool_calls`:
- 86% of calls valid;
- about 3 keys per call;
- p50 latency 1.1 s.

The other three models emit the call as text instead:
- qwen3 puts `<tool_call>{…}` in its reasoning;
- glm writes `<tool_call>send_keys("…")` in its content;
- gpt-oss writes a bare `{"keys":…}` in its content.

`tool_choice: "required"` doesn't help.
**Decision:** live lanes use Gemma. A text-tool-call recovery shim is being built for the
other three.

### D-015 Hidden reasoning off by default
With reasoning on, every model spent the whole output budget reasoning and never made
the tool call. The `samplingParams` in `lane/models.json` set
`chat_template_kwargs.enable_thinking=false`, or `reasoning_effort=low` for gpt-oss. The
probe mirrors this.

### D-014 The harness owns `LD_PRELOAD`; it does not use the `fiu-run` wrapper
`fiu-run` overwrites `LD_PRELOAD`, which silently dropped the crashtrace and torment-seed
preloads. The harness now sets `FIU_ENABLE`, `FIU_CTRL_FIFO` and `LD_PRELOAD` itself.

### D-013 Crash stack hashes via the `torment-crashtrace` preload
An async-signal-safe handler records the pc and return addresses as module+offset, then
the process dies from the same signal. The harness symbolizes the trace (llvm-symbolizer,
falling back to addr2line).
**Result:** 13 Brogue crashes split into 6 clusters, and the hashes are stable across runs.

### D-012 `error_exit` is a separate failure type (weight 1)
A nonzero exit without a signal means the game noticed the fault and quit. It scores 1,
not the 3 a crash scores.

### D-011 Fault activation is configurable (`harness/faults.toml`)
A libfiu profile sets `points`, `probability` and `delay_s`. The default `delay_s` is 5 s:
the faults are enabled through `fiu-ctrl` once the game is running.
**Why:** faults from exec just crash the game during startup (Brogue segfaulted 4 ms in,
every time).
**Finding:** games allocate little once running, so 1% rarely fires. `malloc-20pct` makes
Brogue crash mid-play.

### D-010 ASan builds don't get libfiu malloc faults
ASan replaces malloc, so the libfiu `libc/mm` points never fire. Malloc lanes use the
release builds (`brogue-rel`).

### D-009 Keyboard signals are not crashes
A game killed by SIGINT or SIGQUIT is reported as `agent_error`: the agent typed ^C or ^\.

### D-008 Lanes run as real Minimal sessions via `session exec`
`min session run` blocks, forwards no environment, and runs a separate box. So
`host-up.sh` starts lanes with `min session exec … setsid nohup`.
`session exec` gives the command no TTY, so the wall streams each lane through
`lane/ttyview.py` instead of `tmux attach`.

### D-007 Sideload recipes reference upstream with `upstream "<name>"`
Relative `../pkg` imports do not resolve across layers. The sideload repo must be public
for the daemon to clone it. Reported as gominimal/inbox#1151.

### D-006 One Cloudflare account
The plan is about 25 lanes per model at one action every 5 s, with several keys per call.
The real ceiling comes from the scale test.

### D-005 Cloudflare stays off until the slice works against the mock
Done. The mock slice passed, then the user enabled Cloudflare.

## 2026-10-20

### D-004 Browser race design
- Retro pixel arcade on a second display.
- 15-minute heats with a 1-minute pit.
- Weighted score that decays on repeats: sanitizer 5, crash 3, hang 2, error_exit 1,
  visual 1.
- Standings by lane, by fault config (constructors), by game, and a season table.

### D-003 Zellij wall on the MacBook Pro 16"
8x8 pages of 80x24 lanes at 5x10 px cells, plus a Hot tab of the lanes that failed most
recently.

### D-002 Stack
Minimal sessions, a pi agent through gominimal/broker to Workers AI, and a Go + SQLite + SSE
scoreboard. Games and fault tools are packaged in a sideload repo, noric-labs/torment-pkgs.

### D-001 First games
dfrotz + Zork, Brogue CE and DCSS (they have built-in seeds), with greed as a smoke test.
