# Decision record

Newest first. Each entry gives the decision, why it was made, and what follows from it.
Commits are in the git history; the research is in the
[living doc](https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a).

## 2026-10-21

### D-041 24 lanes by default, all on Workers AI; Claude and local-model profiles wired but parked
**Decision:** `host-up.sh` now starts `scripts/lanes-24.txt` with `-n 24` by default:
- l-001..l-012: the 12-lane set (6 qwen3, 6 GLM-4.7-Flash).
- l-013..l-023: 11 more Workers AI lanes. 4 `thinker` (DeepSeek, at its
  `max_concurrent_lanes`) on Zork, Brogue, Greed and DCSS; 2 `glm53` on Zork and DCSS;
  2 `fast` (qwen3) and 3 `glm-flash` on the real-time games.
- l-024: 1 more `fast` (qwen3).

The default is all Workers AI. The `local` profile (`gemma-4-26b-a4b-it-qat-mlx` in LM
Studio on the Mac) played l-024 in a live test and stays defined for opt-in use.

Every lane runs one fault its game is sensitive to: 10 profiles across the 24 lanes, with
the lanes-16 rules (malloc only on non-ASan builds, so none on DCSS; no `winch-storm` on
Brogue). The first cut copied the no-faults watch set, so the tower showed `none` for every
lane and every hull had the same colour. `--no-faults` still strips them.

The first version had 6 `opus-fast` (Claude Opus 5.5, fast mode) and 5 `haiku` (Claude
Haiku 5.5) lanes in place of l-013..l-023. They were swapped for Workers AI because the
broker had no working Anthropic credential (see Blocker). The profiles and models stay in
`lane/agents.json` and `lane/models.json`.

**How:** pi models may set their own `api`, `baseUrl` and `headers` (pi's models.json
schema), so the new models stay in the `nexus` provider and bypass toolfix, which only
fronts Workers AI:
- Claude: `api: anthropic-messages` at the broker's `/anthropic` route
  (`http://100.64.255.254:8899/anthropic`); the broker injects the key. Opus fast mode is the
  `anthropic-beta: fast-mode-2026-02-01` header plus `speed: "fast"` in `samplingParams`.
  Both models send `output_config.effort: low`. Thinking can't be disabled on Opus 5.5, so
  `reasoning` stays false and pi sends no thinking config.
- Local: `api: openai-completions` at `http://100.64.255.254:1234/v1`. The host alias
  forwards to the Mac's loopback, so host_ip lanes reach LM Studio without a broker route.
  Gemma 4 26B-A4B answers a 1K-token turn in about 1 s with native tool calls.
  `qwen3.8-27b-mlx` was tried first and dropped: it ignores `enable_thinking: false` and
  spent 13 s reasoning per turn, so the lane stalled.

**Cost:** Opus fast mode is $8/$40 per MTok (2x standard Opus). Six lanes at about one
action per 1-3 s with about 1-2K input tokens per action cost on the order of $100-250 an
hour. Haiku 5.5 ($0.10/$0.50) and the local model are negligible.
**Blocker (2026-10-21):** every `/anthropic` credential in the broker failed. The OpenRouter
key gets 401. The Claude subscription OAuth token gets a bare `rate_limit_error` with no
rate-limit headers: Anthropic refuses it for a third-party agent. The API key gets "credit
balance is too low". Opus fast mode needs an API key with credit.
**Also:** a "built with Minimal" badge in the README (`docs/assets/built-with-minimal.svg`)
and a pixel version in the race UI's infield and pit screen.

### D-040 Agent configuration file (`lane/agents.json`)
**Problem:** a lane's model came from the lanes file, its style from a global flag, and its
pacing from a global default. D-037 showed that the right settings depend on the game
(DeepSeek for Zork and DCSS, qwen3 for the fast games) and on the model's rate limit
(DeepSeek needs about 7 s per lane).
**Decision:** a single file, `lane/agents.json`, drives every lane's agent. A web editor
is built against this schema:
- `defaults.profile`: the profile used when nothing else picks one.
- `profiles.<name>`: `model` (an id in `lane/models.json`), `style` (`win` or `explore`),
  `interval_ms` (at least 250). Optional: `max_tokens`, `sampling` (deep-merged over the
  model's `samplingParams`), `notes`.
- `models.<id>`: `min_interval_ms`. Optional: `max_concurrent_lanes`, `notes`.
- `games.<name>` (names from `harness/games.toml`): all optional. `profile`; `prompt` (a
  file under `lane/prompts/`); `keys` (the only keys `send_keys` accepts); `goal` (appended
  to the game prompt as "Goal for this lane: …"); `interval_ms`.

**Resolution per lane:** the lanes file's 5th column, then `games[game].profile`, then
`defaults.profile`. The column takes three forms:
- `profile:<name>`, or a bare name (one with no `/` or `@`).
- A model id, for back-compat. That maps to the game's profile if it uses that model, else
  the first profile that does. If no profile uses it, the game's profile is kept with the
  model swapped in, minus the profile's `sampling` and `max_tokens`.

The interval is `max(games[game].interval_ms or profile.interval_ms, models[model].min_interval_ms)`.
`--play-style` still overrides every lane's style. Without the flag, the profile's style
applies; before this, the default was `explore` unless `--no-faults` was given.

**Keys:** an entry like `<Esc>` allows that named key. Plain text allows each of its
characters (`"hjkl"`). A space and `<Space>` are the same key. `game.ts` rejects a batch
that contains any other key, before pacing and before typing, and lists the allowed keys in
the tool description.

**Plumbing:** `tools/agents_check.py` validates the file with stdlib Python. It catches an
unknown model, profile, game or named key; a bad style; an interval under 250; and a
missing prompt file. Unknown fields only warn. `host-up.sh --agents FILE` (default
`lane/agents.json`) runs it before anything starts, and an invalid file stops the run.
For each lane it writes these to `lane.env`, base64-encoded so spaces and `<keys>` survive
the remote shell: `NEXUS_PROFILE`, `NEXUS_MODEL`, `NEXUS_PLAY_STYLE`,
`NEXUS_ACTION_INTERVAL_MS`, `NEXUS_PROMPT_FILE`, `NEXUS_ALLOWED_KEYS`, `NEXUS_GOAL`,
`NEXUS_MODEL_MAX_TOKENS` and `NEXUS_SAMPLING`. `host-up.sh --print-plan` (or
`agents_check.py --lanes F --plan`) prints the resolved table and starts nothing.

**Rate limits:** `max_concurrent_lanes` is checked at host-up. Too many lanes on one model
only warns, and the run continues: a short burst of 429s beats a refused race. Seed values
are DeepSeek at 7000 ms and 4 lanes (about 34 req/min against its ~36), and GLM-5.3 at
5000 ms and 3 lanes. GLM-5.3-Flash is set to 15000 ms and 2 lanes; it is too slow to play.
**Also:** `scripts/lanes-12-profiles.txt` is the 12-lane set written with profiles. It has
3 DeepSeek lanes.

### D-039 Wall in the browser via zellij web
**Problem:** the wall only existed in a fullscreen terminal on the Mac. A second screen, a
projector or another laptop on the desk could not show it without a terminal tuned to 4 pt.
**Decision:** use zellij 0.45's built-in web client, not a custom viewer.
`scripts/wall-web.sh start` (or `host-up.sh --web-wall`) does three things, all under
`ulimit -n 4096`:
- It creates `nexus-wall` headless with
  `zellij --config wall/config.kdl attach --create-background nexus-wall options --default-layout wall/out/wall.kdl`.
  A running session of that name is reused.
- It starts `zellij web --start --daemonize` on 127.0.0.1:8082. A server that is already
  online is reused.
- It creates a **read-only** token (`zellij web --create-read-only-token`). The token can
  only watch existing sessions. It prints the URL `http://127.0.0.1:8082/nexus-wall` and
  the token once.

**Sharing:** a session is served only if it opts in. `wall/config.kdl` now sets
`web_sharing "on"`. The per-session `options --web-sharing on` on `attach -b` did not take
effect in 0.45.1 (`/session-list` showed `web_clients_allowed: false`). The config line did.
**Secrets:** the token is a credential. It goes to the terminal only. `.build/wall-web/`
(gitignored) holds the token *names* and flags for what `start` created. `wall-web.sh stop`
(also run by `host-down.sh`) revokes those tokens and stops the server and session, but
only the ones it started.
**Verified (2026-10-21, on the Mac, with a 2-pane test layout):** `GET /` and
`GET /<session>` return 200. `/session-list` without a login returns 401. Login with the
read-only token returns 200, and `/session` returns `is_read_only: true`. In headless
Chrome, the xterm buffer showed both panes updating live.
**Gotchas:**
- `zellij web --status` exits 0 even when the server is offline, so the script probes
  with curl.
- `zellij web --stop` refuses `--port`, so the port is fixed at 8082.
- `--create-read-only-token` refuses `--token-name`.
- The browser font defaults to 12 px. For 8x8 pages, zoom the page out (Cmd -) until the
  grid fits.
- `wall-pager` drives the headless session exactly as it drives the terminal one.

### D-038 Race UI: neon anti-grav theme, pilot deaths, generative DnB
**Look:** the race UI is re-themed as futurist anti-grav racing: a dark void and deck with
neon cyan, magenta and yellow signals and a three-step blood ramp (`theme.js`). The
pixel-art rules are unchanged: only `fillRect`, `drawImage` and `putImageData`, no smoothing,
and an integer canvas scale. Crash is `#ff1f3a`, sanitizer `#ff2bd6`.
**Deaths:** a crash or sanitizer report kills the pilot. The craft breaks into shards, the
pilot is ejected and bursts into blood and gibs, and the stains stay on the deck as decals
(capped at 600, cleared at each green flag). A banner reads PILOT TERMINATED, stacking to
DOUBLE KILL and MULTI KILL Xn for deaths within 3 s. Outside the pit, a 2x kill-cam inset
replays the death site, at 0.5x speed for the first 0.6 s. Hang, error exit and visual keep
their effects, restyled in the new palette.
**Music:** `music.js` generates a drum & bass track with WebAudio, seeded by the heat id so
each heat has its own song (key, chord loop, drum pattern, acid riff): 172 BPM, with drums,
sub bass, pad and an acid line. Intensity follows the phase: pad, hats and filtered sub in the
pit, a riser into the full mix at the green flag, and an opened acid filter with extra
percussion in the final minute. A death drops the drums for a bar under a sub boom; a lead
change plays an acid fill.
**Keys:** `M` toggles all sound (effects and music); `B` toggles only the music.
**Originality:** the art, sounds and music are made for this project. There are no
third-party logos, samples or music.
`tools/smoke_ui.mjs` checks the banner, kill-cam, decals and music start/stop.

### D-037 Stronger models play the reasoning games; DeepSeek flash is capped at ~36 req/min
**Memory did not help (discarded).** A `note()` notebook plus `KEEP_TURNS=10` gave sensible
notes but no better scores on qwen3/GLM-4.7, at 2–4x the tokens per call.
**New models** in `lane/models.json` (costs estimated from Workers AI neurons):
`glm-5.3`, `glm-5.3-flash`, `deepseek-v4.1-flash`. All make structured tool calls.
**Rate limits** (`docs/probe-flash-2026-10-21.txt`, scaletest probe, about 2k input tokens):
- `deepseek-v4.1-flash`: no failures at 30 req/min; 70% 429 "inference request per min rate
  reached" at 60. Sustains about 36/min.
- `glm-5.3-flash`: p95 13 s even at 15/min, and it reasons regardless of `enable_thinking`
  (only `reasoning_effort: "low"` stops it). Too slow to play.
- `glm-5.3`: 18–240 s per action across 6 lanes, many stalls.

**Races** (`docs/ab-models-2026-10-21.txt`, 10 min each, `scripts/ab-race.sh`):

| Game | qwen3 | deepseek-v4.1-flash | glm-5.3 |
| --- | --- | --- | --- |
| Zork | 0 after 360–410 moves | 40 (Maze, about 80 moves) | 49 (Kitchen, 79 moves) |
| DCSS | – | Dungeon:2 | Dungeon:2 at 5 min |
| Greed | 185 | 76–134 | 5 |
| Brogue | depth 1 | depth 1 | depth 1 |
| nSnake, Pacman, Bastet | 0–63 | 0–17 | 0–8 |

**Decision:** use DeepSeek (or glm-5.3 when speed doesn't matter) for the reasoning games:
Zork and DCSS. Use qwen3 for the fast or real-time ones (Greed, nSnake, Pacman, Bastet).
`scripts/lanes-12-mixed.txt` puts DeepSeek on 4 lanes. That is still too many for its limit
(4–8 429s per lane, 9–19 s per action); 2–3 lanes or per-model pacing (~7 s) would fit.
Nothing plays Brogue past depth 1 or the real-time games well yet.
**Also:** the host broker died unnoticed, and a whole race failed with `toolfix: upstream
unreachable`. `host-up.sh --live` now checks the broker's `/health` first.

### D-036 The wall blocked every agent key; 1 s pacing; `win` play style
**Cause of the "stalled" lanes:** `lane/ttyview.py` attached with `tmux attach -r`. In each
lane that read-only viewer was the only tmux client, and tmux 3.7 resolves a bare
`send-keys` to it and refuses: `client is read-only`. With the wall open, every agent key
failed. In the 12-lane no-fault run after 26 min: 5–7 actions and 79–273 `send_keys`
errors per lane, about 220 s per action. The 60 s stall aborts were a side effect.
**Fix:** ttyview attaches with `-f ignore-size` and no `-r`. It never writes to the pty,
so the view stays read-only in practice.
**Pacing:** `NEXUS_ACTION_INTERVAL_MS` defaults to 1000 (was 5000), so model latency sets
the pace. 12 lanes come to about 180 req/min per model, half the 360/min tested in D-027.
**Play style:** `NEXUS_PLAY_STYLE=explore` (default, `prompts/system.md`, bug hunting) or
`win` (`prompts/system-win.md`, play for progress). `host-up.sh --no-faults` selects `win`;
`--play-style` overrides it.
**Prompts:** release builds fall back to the base game's prompt (`brogue-rel` → `brogue.md`).
New prompts for nSnake, Pacman and Bastet. Bastet's keys come from its source: Drop is the
keypad Enter, which tmux's `<Enter>` does not send, so the prompt says to use `<Down>`.

### D-035 Live lanes run `host_ip`; the D-030 gateway is dropped
**Measured on min 0.7.1** (VM `default`, test box `gw-test`):

| From | To | Result |
| --- | --- | --- |
| own_ip lane, `--allow-subnets 0.0.0.0/0` | broker `100.64.255.254:8899` | timeout |
| own_ip lane | a host_ip box (`100.64.255.253`, `gw-test.min.internal`) | timeout |
| host_ip box | broker | answered (Cloudflare 405 on GET) |
| host_ip box | own_ip scoreboard `scoreboard.min.internal:8080` | 200 |

So the D-030 relay cannot work: own_ip lanes can't reach a host_ip gateway. A host_ip
lane needs no gateway at all.
**Decision (user sign-off 2026-10-21):** the scoreboard stays own_ip with `--ingress 8080`
and the `localhost:8080` forward. With `--live`, lanes run host_ip with
`--loadout nexus-lane --loadout broker` (`host-up.sh --lane-network`, defaulting to host_ip
under `--live`). Cost: live lanes have no per-box egress isolation and share the VM's
network namespace; lane ports are already random, so they don't collide.
**Long term:** Minimal's Box Egress Proxy (`--credentialed-upstream`, minimal spec 24) is the
isolated path once 0.7.x can configure it.

**First live race in real sessions** (heat h-0002, about 20 min, about 770k input tokens):

| Lane | Model | Result |
| --- | --- | --- |
| l-001 Zork, winch-storm | qwen3 | 184 turns, Living Room, game score 10 after 73 moves, no failures; 49/49 tool calls structured |
| l-002 Brogue-rel, malloc-20pct | GLM | SIGSEGV crash cluster, score 4.5; 337 of 346 tool calls structured, 1 upstream error |
| l-003 Bastet, winch-storm | qwen3 | started late; visual blank-screen |

**Also found:**
- **Loadout hooks run on min 0.7.1.** The `nexus-lane` on_activate hook npm-installed pi on
  every activation and timed out at 240 s on l-003. It now uses `pi` from `pi-next` when on
  PATH, and activation takes about 2 s. The drafted inbox item about inert hooks is stale.
- **`min loadout list | grep -q` panics `min`** (EPIPE) and failed `host-up.sh` under
  pipefail. Fixed by capturing the output first.
- **Wall verified headless:** zellij run in a detached host tmux (240x70). Pages show live
  games through `ttyview`, and `wall-pager` fills the Hot tab. A stale zellij server state
  once made `-n` fall back to the default layout; `zellij kill-all-sessions` +
  `delete-all-sessions` cleared it.

### D-034 First real-session run on the Mac works end to end (2026-10-21)
`scripts/host-up.sh` on the user's MacBook brought up the scoreboard plus 3 lane
sessions in VM `default`.
- Each session is `own_ip` with open egress.
- Jobs launch through `detach.py`; every `exec` returned.
- The scoreboard built (go, open egress) and answered within about 10 s, and the
  forward to `localhost:8080` works.

| Lane | Model | Fault | Result after about 1 min |
| --- | --- | --- | --- |
| Brogue | GLM | malloc-20pct | 4 crashes in 4 clusters, score 12 |
| Zork | qwen3 | winch-storm | running, no failures |
| Bastet | qwen3 | winch-storm | running, no failures |

All three were on the mock model. `wall-gen` produced the pages.

**Not yet verified:** the zellij wall through `ttyview` (zellij was not installed on the
Mac), the race UI in a browser, and live inference. Live needs the D-030 gateway, because
own_ip boxes can't reach the host broker.

### D-033 Open egress by default to get started (`host-up.sh --egress open`)
**Cause of the last "hang":** the scoreboard's `go build` failed to fetch modules under
DNS-host allows. Minimal's go sets `GOPROXY=…|direct`, so Go reaches hosts that can't be
listed up front. host-up then waited silently.
**Decision:** for now, own_ip boxes get `--allow-subnets 0.0.0.0/0`, all internet plus the
in-VM plane. `--egress strict` keeps the per-host lists.
**Long term:** least privilege through egress presets (gominimal/inbox#1154), or a
packaged scoreboard binary that needs no egress.
**Still true:** own_ip boxes can't reach the host broker (#1153), so live lanes need the
D-030 gateway.

### D-032 Launch jobs with `scripts/detach.py`; pi comes from the `pi-next` package
**Hang, part 2.** The D-031 brace form still hung on the Mac. `setsid nohup … &` only
redirects fds 0–2, so the job still inherits any other fd the exec relay passes down, and
that keeps `exec` waiting.
- Reproduced locally with an extra inherited pipe on fd 3: the setsid form held it 8.0 s,
  while `detach.py` (double fork, setsid, closing every fd ≥ 3, then exec) took 0.0 s.
- `host-up.sh` now launches the scoreboard and lanes through it.
- `procps-ng` is added to the session packages for debugging (the box had no `ps`).

**pi.** Upstream `gominimal/pkgs` has pi 1.0.2. A sideload cannot shadow an upstream
package name ("package 'pi' already exists"), so `torment-pkgs` ships `pi-next` 1.3.0. It
builds, and a mock slice played on it.
- It is in `[session].packages`.
- `lane.sh` and `slice.sh` prefer `pi` on PATH; npm stays only as a fallback.
- Upstream bump: gominimal/pkgs#965. Drop `pi-next` once that merges.

### D-031 `host-up.sh` hang: shell precedence, not Minimal
**Cause:** `cmd && setsid nohup job >log 2>&1 </dev/null &` backgrounds the whole `&&`
list in a subshell. The redirects only cover the last command, so the subshell keeps the
caller's stdout open until the job exits, and `min session exec` waits for EOF.
**Ruled out on the Mac:** provisioning (a plain `exec` returns at once) and ssh mux
(`controlmaster false`).
**Measured locally with a captured stdout pipe:** the old form held the pipe 20.0 s; the
fixed form, `cmd && { setsid nohup job … & }`, returned in 0.0 s.
gominimal/inbox#1153 item 11 (ssh mux) can be downgraded to a docs note.

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
