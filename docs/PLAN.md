# Plan

The research, sources and diagrams are in the
[living doc](https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a).
This file records the decisions and milestones that the code is built against.

## Decisions

| Area | Decision |
| --- | --- |
| Substrate | Minimal sessions, one per lane; a lane is the racer |
| Lane | game × fault config × pi agent × seed |
| Agent | pi in RPC mode, `--no-builtin-tools`, tools `send_keys(keys)` and `screen()` |
| Inference | gominimal/broker → Cloudflare Workers AI, **one account**; 4 models × ~25 lanes |
| Cadence | one action every 5 s, several keys per call |
| Lane count | ~100 agent lanes (set by the scale test); fuzz/bot lanes use no inference |
| Packages | `torment-pkgs` sideload via `[[upstream.sideload]]`; libfiu, libfaketime, seed shim, games |
| First games | dfrotz + Zork, Brogue CE, DCSS (built-in seeds); greed as smoke test |
| Fault layers | libc/syscall, resource limits, terminal chaos, fuzzing; kernel/eBPF deferred |
| Failures | crash, sanitizer, hang, visual corruption; dedup by top-5 in-project frames |
| Wall | zellij on the MacBook Pro 16", 8x8 pages of 80x24 read-only attaches at 5x10 px cells, Hot tab |
| Race | second display, retro pixel arcade, details below |

## Browser race

- **Scoring:** `score = Σ weight(type) × 0.5^(n−1)`, where n is the hit number of that
  stack-hash cluster in the lane this heat.
- **Weights (configurable):** sanitizer 5, crash 3, hang 2, visual 1, agent error 0.
- **Heats:** 15 min plus a 1-min pit. The scoreboard emits `pit` over SSE, and the launcher
  recycles lanes with new seeds. Events carry `heat_id`.
- **Standings:** lanes; constructors (by fault config); by game; season table (25-18-15-12-10-8-6-4-2-1, to confirm).
- **Track:** one pixel oval with every car on it; distance is heat score, and a lap is 20
  points. The top 10 are enlarged with number plates, and a timing tower shows the rest.
  Targets 1920x1080 with integer scaling.
- **Clicking a car opens:** the live 80x24 screen, the failure log with a replay bundle, the
  config card, and a "focus on wall" button.
- **Flair:** a crash effect per failure type, WebAudio chiptune (muted by default), a
  commentary ticker, and attract mode with the podium during the pit.

## Milestones

1. **M0: repos and packages.** Done when `min package build` passes for libfiu,
   libfaketime, the seed shim, dfrotz and Brogue CE (ASan+UBSan), and dfrotz runs under
   `fiu-run` in a session. Scale test phase A runs here.
2. **M1: 3-lane slice.** Done when 3 lanes (dfrotz, Brogue, greed) appear on the leaderboard
   and the wall, at least one failure is counted with a stack hash, and destroying a lane
   brings it back. Scale test phases B and C run here.
3. **M2: faults, sanitizers, fuzzing.** Done when every fault config emits events and the
   board shows unique clusters per lane.
4. **M3: race UI and wall.** Done when the full race spec above runs on the second display,
   the wall shows 64 lanes per page, and the Hot tab follows failures within 5 s.
5. **M4: scale to the measured ceiling.** Done when that lane count runs 24 h unattended
   within rate limits, with a replayable record for every failure.

## Scale test

| Phase | Ramp | Stop when | Output |
| --- | --- | --- | --- |
| A. Inference probe | 60 → 120 → 240 → 300 → 360 req/min per model, 5 min per step; one model, then all 4 | 429s > 1% or p95 > 5 s | sustainable req/min per model; whether the limit is per model or account-wide; keys per call |
| B. Host density | 10 → 25 → 50 → 100 → 150 lanes with a random-key stub, 15 min per step | CPU > 85%, memory > 80%, activate p95 > 30 s, or event loss > 0.1% | lanes per host; activate/destroy latency |
| C. Combined soak | 80% of min(A, B) with real pi lanes, for 2 h | 429s > 1% or over budget | the M4 lane count, $/hour, failures per lane-hour |

## Open questions

- gominimal/broker: confirm that it injects the Workers AI credential.
- Minimal team: per-session cgroup limits, FUSE/BPF, sideload build caching, own_ip
  box-to-box traffic, churn latency, and multi-host support.
- Kernel/eBPF layer scope.
- Season points scheme, and the second display's resolution.
