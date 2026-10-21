# Handover: Torment Nexus (2026-10-21)

**Where things stand:** the 3-lane slice runs end to end in one Minimal box, with live
Workers AI and real faults, and it is finding real bugs. The next milestone is running
lanes as real Minimal sessions across several microVMs from the Mac. That is blocked on
two things:
1. the `host-up.sh` hang;
2. a topology decision (D-030), because `own_ip` boxes can't reach the host broker.

The authoritative history is [docs/DECISIONS.md](docs/DECISIONS.md) (D-001..D-030). The
interfaces are in [docs/CONTRACTS.md](docs/CONTRACTS.md), and the plan and milestone status
in [docs/PLAN.md](docs/PLAN.md). The shared research doc is at
https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a and has a Status
section.

## Repos

| Repo | What | Notes |
| --- | --- | --- |
| [noric-labs/nexus](https://github.com/noric-labs/nexus) (private) | harness, lane runtime, scoreboard and race UI, wall, scaletest, scripts | `minimal.toml` declares the sideload and every task |
| [noric-labs/torment-pkgs](https://github.com/noric-labs/torment-pkgs) (public) | Minimal sideload: 8 games, libfiu, libfaketime, torment-seed, torment-crashtrace | Must stay public, because the host daemon clones it. Recipes use `upstream "<name>"`, not `../pkg` imports (D-007). |

## What works (verified)

- **Packages:** all 12 build with `min package build`:
  - games: frotz, zork-stories, brogue-ce, dcss, greed, bastet, nsnake, pacman4console;
  - fault and support libraries: libfiu, libfaketime, torment-seed, torment-crashtrace.
- **Harness:** `harness/`, 85 tests.
  - Fault kinds: libfiu (activated after a delay through `fiu-ctrl`), strace, rlimit,
    faketime, winch-storm, keystorm.
  - Detectors: crash with a stack hash, sanitizer, hang, error_exit, visual.
  - DCSS ambient UBSan reports are baselined.
- **Lane runtime:** `lane/`, the pi RPC driver.
  - A 60 s stall watchdog.
  - The `toolfix.py` proxy (18 tests) normalises requests for Workers AI and recovers
    tool calls written as text.
  - All four models play live; qwen3 and GLM are the defaults (D-028, D-029).
- **Scoreboard and race UI:** `scoreboard/`. Go tests pass; the UI passed a headless
  smoke test (screenshots in `docs/img/`).
- **Inference:** phase A probe cost $15.20. No 429s up to 360 req/min per model, so lane
  count is bound by host capacity, not Workers AI (D-027).
- **Bugs found live:**
  - Bastet: ASan SEGV in ncurses redraw after a resize.
  - nSnake: ncurses aborts under resize.
  - Brogue: stable malloc-fault crash clusters.
  - DCSS: a UBSan shift in `chardump.cc` reached through melee.

## How to run

- **Local slice, several lanes in one box:** `min task run slice`. The scenario tasks are
  `slice-faults`, `slice-new`, `slice-dcss`, and the LIVE ones `slice-toolfix` and
  `slice-mixed`, all defined in `minimal.toml`. Output goes to `.build/slice-*/`.
- **Race UI demo with no games:** run `scoreboard -heat 60s -pit 10s`, then
  `python3 scoreboard/tools/fake_lanes.py`.
- **Probe:** `cd scaletest && uv run python -m probe.cli --help`. Dry runs are the default;
  `--live` spends real credit.
- **Host sessions:** `scripts/host-up.sh --dry-run`, then `scripts/host-up.sh`.
  **Currently hangs, see below.**

## Blockers and open decisions

1. **`host-up.sh` hangs** at the first `min session exec … setsid nohup … &`.
   - From the source: `exec` returns when its child exits, and the Mac `min` client
     waits for ssh stdout EOF.
   - Suspect: an ssh ControlMaster/ControlPersist setting holding that open. Check with
     `ssh -G local-minvmd0 | grep -i control` and `~/.ssh/config`.
   - Also possible: the first `exec` waiting on session provisioning. `[session].packages`
     is now large (it includes dcss and llvm).
2. **Multi-VM topology (D-030, proposed, needs sign-off).** The constraints:
   - sessions share one VM, and more VMs need `minvmd --vm NAME` / `min --vm NAME`;
   - `exec` needs `--vm`;
   - box names must be unique machine-wide;
   - `own_ip` boxes cannot reach the host alias (broker);
   - there is no cross-VM traffic.

   The proposal:
   - a `host_ip` gateway box per VM, relaying to the broker and the scoreboard;
   - `own_ip` lanes with egress only to their gateway's /32;
   - pi vendored (no npm at runtime).

   Next step: implement `host-up.sh --vms K` with a gateway relay.
3. **AFL++ fuzz lanes (D-026):** deferred. A delegation attempt was cut off by a safety
   classifier; the user to scope it.
4. **Not yet verified on real hardware:**
   - the race UI in a real browser on the second display (WebAudio, SSE reconnect);
   - the zellij wall attaching through `min session exec` + `lane/ttyview.py`;
   - scale test phase B (host density).

## Gotchas

- **Tools disappear on session restart.** Packages recorded in the workbench
  `minimal.toml` vanished after restarts and had to be re-added with `min add`.
- **Sideload packages only load inside tasks.** Use a task or `[session].packages`;
  `min add` and `min materialize` can't see them.
- **Minimal shows no build logs.** To debug a failed `min package build`, have `build.sh`
  copy its log into `$OUTPUT_DIR` as an output (D-007; gominimal/inbox#1151).
- **Lanes sharing one box need separate tmux sockets.** `slice.sh` handles this with
  `NEXUS_TMUX_SOCKET`. Test scoreboards must use random ports.
- **libfiu malloc faults never fire under ASan builds.** Pair malloc profiles with the
  `*-rel` release builds.
- **`fiu-run` overwrites `LD_PRELOAD`.** The harness sets up libfiu itself (D-014).

## Issues filed

- gominimal/inbox#1151: sideload `upstream "<name>"`, build logs, error output.
- gominimal/inbox#1153: named VMs (`--vm`), multi-VM networking limits, `session exec`
  lifetime and the ssh mux pitfall.
