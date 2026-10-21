# Handover: Torment Nexus (updated 2026-10-21, after D-035)

**Where things stand:** the Nexus runs as real Minimal sessions on the Mac. `scripts/host-up.sh`
brings up the scoreboard and 3 lane sessions in VM `default`.
- Every session is `own_ip` with open egress. The lanes play on the mock model.
- Failures score: Brogue under `malloc-20pct` produced 4 crash clusters within a minute
  (D-034).
- The race UI is forwarded to `localhost:8080`.
- The zellij wall shows live games through `ttyview`, and the pager fills the Hot tab
  (verified headless in a host tmux, D-035).
- **Live inference works in real sessions (D-035):** `host-up.sh --live` runs the lanes
  `host_ip` with the broker loadout. qwen3 and GLM played Zork and Brogue for 20 minutes.
  The D-030 gateway is dropped: own_ip boxes can't reach host_ip boxes.

Separately, the multi-lane slice runs in one box with live Workers AI and is finding real
bugs.

**Next milestones:**
1. The race UI in a real browser (WebAudio, SSE reconnect), and the wall on the real display.
2. Several VMs (`host-up.sh --vms K`).

The authoritative history is [docs/DECISIONS.md](docs/DECISIONS.md) (D-001..D-035). The
interfaces are in [docs/CONTRACTS.md](docs/CONTRACTS.md), and the plan and milestone status
in [docs/PLAN.md](docs/PLAN.md). The research doc is at
https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a.

## Repos

| Repo | What | Notes |
| --- | --- | --- |
| [noric-labs/nexus](https://github.com/noric-labs/nexus) (private) | harness, lane runtime, scoreboard and race UI, wall, scaletest, scripts | `minimal.toml` declares the sideload, the session packages and every task |
| [noric-labs/torment-pkgs](https://github.com/noric-labs/torment-pkgs) (public) | Minimal sideload: 8 games, libfiu, libfaketime, torment-seed, torment-crashtrace, pi-next | Must stay public, because the host daemon clones it. Recipes use `upstream "<name>"` (D-007). A sideload can't shadow an upstream package name (D-032). |

## What works (verified)

- **Real sessions on the Mac (D-034):**
  - `host-up.sh` activates the scoreboard and the lanes.
  - Jobs launch through `scripts/detach.py`, and every `exec` returns.
  - The scoreboard builds with Go and answers.
  - The `:8080` forward works, and lanes report heartbeats and failures.
- **Packages:** all 13 build with `min package build`.
  - Games: frotz, zork-stories, brogue-ce, dcss, greed, bastet, nsnake, pacman4console.
  - Fault and support libraries: libfiu, libfaketime, torment-seed, torment-crashtrace.
  - Agent: pi-next 1.3.0. The upstream bump is gominimal/pkgs#965.
- **Harness:** `harness/`, 85 tests.
  - Faults: libfiu (activated after a delay through `fiu-ctrl`), strace, rlimit, faketime,
    winch-storm, keystorm.
  - Detectors: crash with a stack hash, sanitizer, hang, error_exit, visual.
  - DCSS ambient UBSan reports are baselined.
- **Lane runtime:** `lane/`, the pi RPC driver.
  - A 60 s stall watchdog.
  - The `toolfix.py` proxy (18 tests) normalises requests for Workers AI.
  - All four models play live; qwen3 and GLM are the defaults (D-028, D-029).
- **Scoreboard and race UI:** Go tests pass; headless smoke test 40/40 (screenshots in
  `docs/img/`).
- **Inference:** phase A probe cost $15.20. No 429s up to 360 req/min per model (D-027).
- **Bugs found live:**
  - Bastet: ASan SEGV in ncurses redraw after a resize.
  - nSnake: ncurses aborts under resize.
  - Brogue: stable malloc-fault crash clusters.
  - DCSS: a UBSan shift in `chardump.cc` reached through melee.

## How to run (on the Mac, from the nexus checkout)

```sh
# lanes
min session destroy scoreboard 2>/dev/null; for s in l-001 l-002 l-003; do min session destroy $s 2>/dev/null; done
git pull && scripts/host-up.sh             # --dry-run, -n N, --egress open|strict, --live (host_ip lanes, D-035)
curl -s localhost:8080/api/state | head -c 400; open http://localhost:8080

# wall: a fullscreen terminal that is NOT inside zellij
ulimit -n 4096   # ~164 panes, each a pty; the macOS default of 256 fds makes zellij panic
cd wall && zellij kill-session nexus-wall 2>/dev/null; zellij delete-session nexus-wall --force 2>/dev/null
zellij --config config.kdl -s nexus-wall -n out/wall.kdl
# pager, in a second terminal
cd wall && uv run wall-pager --session nexus-wall --scoreboard http://localhost:8080 --out out

# lane logs / one lane's view
min session exec l-001 'tail -n 40 $HOME/.nexus/lane.log $HOME/out/harness.log $HOME/out/agent/agent.log'
min session exec l-001 'exec python3 /workbench/lane/ttyview.py'

scripts/host-down.sh                       # tear down
```

**In one box (from a Minimal session):**
- `min task run slice` runs the local slice.
- The scenario tasks are `slice-faults`, `slice-new` and `slice-dcss`, plus the LIVE ones
  `slice-toolfix` and `slice-mixed`. Output goes to `.build/slice-*/`.
- Race UI demo: `scoreboard -heat 60s -pit 10s` plus `python3 scoreboard/tools/fake_lanes.py`.
- Probe: `cd scaletest && uv run python -m probe.cli --help`. `--live` spends real credit.

## Open items, in order

1. **Race UI** on the second display, in a real browser: WebAudio and SSE reconnect. Not
   done: the Chrome extension was not connected.
2. **Wall on the real display.** Panes and pager are verified headless; check pane sizing
   on the 16" fullscreen terminal.
3. **Multiple VMs:** `host-up.sh --vms K`.
   - Use `minvmd --vm NAME` and `min --vm NAME …`, and pass `--vm` to `exec`.
   - Box names must be unique machine-wide.
   - There is no cross-VM traffic, so lanes in other VMs can't reach the scoreboard box.
     Needs a design: a scoreboard per VM, or relaying through the host.
4. **Live-lane isolation.** Live lanes are host_ip, with no egress enforcement (D-035).
   Move them to own_ip once Minimal's Box Egress Proxy (`--credentialed-upstream`) can
   front the Workers AI route.
5. **Least-privilege egress.** `--egress open` is the default for now (D-033). Return to
   strict once egress presets (#1154) or an audit mode (#1155) exist, or once the
   scoreboard is a packaged binary that needs no Go build.
6. **Scale test phase B** (host density) on the Mac.
7. **AFL++ fuzz lanes (D-026):** for the user to scope.

## Gotchas

- **The "exec hang" was not exec (D-031..D-034).**
  - `host-up` was silently waiting on a scoreboard whose `go build` had died for lack of
    egress. It now shows progress and fails fast with the log tail.
  - Background jobs with `scripts/detach.py`: `a && setsid … &` backgrounds the whole
    chain, and setsid leaks the relay's extra fds.
- **zellij 0.45:**
  - `-n` (`--new-session-with-layout`) starts the wall. `--layout` targets an existing
    session and exits with "There is no active session!".
  - Launching inside zellij, or into a stale `nexus-wall`, gives a plain shell.
- **Minimal's go uses `GOPROXY=…|direct`,** so DNS-host egress lists can't cover module
  fetches.
- **Session boxes lack basic tools:** `procps-ng` is in the session packages for
  `ps`/`pgrep`.
- **Packages vanish on restart:** packages recorded in the workbench `minimal.toml` vanished
  after restarts and had to be re-added.
- **Sideload packages only load inside tasks or `[session].packages`;** `min add` and
  `min materialize` can't see them.
- **Minimal shows no build logs:** copy `build.sh`'s log into `$OUTPUT_DIR` to debug a build
  (gominimal/inbox#1151).
- **Several lanes in one box need separate tmux sockets** (`NEXUS_TMUX_SOCKET`). Test
  scoreboards must use random ports.
- **libfiu malloc faults never fire under ASan builds:** use the `*-rel` builds.
- **`fiu-run` overwrites `LD_PRELOAD`:** the harness sets libfiu up itself.

## Issues and PRs filed

- gominimal/inbox#1151: sideload `upstream "<name>"`, build logs, error output.
- gominimal/inbox#1153: named VMs (`--vm`), multi-VM networking limits, `session exec`
  lifetime. It has a correction comment: the hang was our shell, not ssh.
- gominimal/inbox#1154: egress presets, modelled on gh-aw network permissions.
- gominimal/inbox#1155: an egress audit mode that suggests a least-privilege allow list.
- gominimal/pkgs#965: bump `pi` to 1.3.0. Drop `pi-next` once it merges.

### Drafted for the inbox, not yet filed

1. **Comment on #1151, sideload docs:**
   - **Updating an upstream package from a sideload isn't documented.** Reusing the name
     fails with `package 'pi' already exists`, so we shipped `pi-next`.
   - **`min check` inside a sandbox ignores the cwd's `minimal.toml`.** It passed a
     deliberately broken copy.
   - **Where sideload packages apply isn't documented.** They work in
     `[tasks.*].packages` and `[session].packages`, but `min add` and `min materialize`
     report "no such package".
2. **New bug: `min session exec` leaks daemon sockets into user commands.**
   - Evidence: a command run via `exec` holds `socket:` fds 16, 39, 48 and 58, from
     `ls -l /proc/<pid>/fd` in the scoreboard session on 2026-10-21. Looks like CLOEXEC
     is missing.
   - Background jobs inherit them. That's a hygiene and potential security issue, and it
     can keep the exec channel open.
   - Workaround: `scripts/detach.py` closes fds 3 and up.
3. **No `ps`/`pgrep` in session boxes.** `base` has no procps. Document it, or add
   `procps-ng` to the default session tools.
4. **Docs inconsistency on lifecycle hooks.** *Stale as of min 0.7.1: hooks run
   (D-035). Re-check the docs before filing.*
   - `concepts/sessions` says hooks are inert in the current release: project
     `[session]` hooks are composed but never executed, and loadout hooks are excluded.
   - `reference/loadouts` describes `on_activate`, `on_destroy`, `on_attach` and
     `on_detach` as running, with `on_activate` failures failing activation and 60–300 s
     timeouts.
   - Our `lane/loadout` hook never ran, so `lane.sh` installs pi itself.
   - Ask: mark the loadout reference as not yet active, or ship hooks.
