# Decision record

Newest first. Each entry gives the decision, why it was made, and what follows from it.
Commits are in the git history; the research is in the
[living doc](https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a).

## 2026-10-21

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
