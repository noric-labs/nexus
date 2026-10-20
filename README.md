# nexus

The Torment Nexus: a parallel software torment rig. About 100 Minimal sandboxes ("lanes")
each run one terminal game, played by a [pi](https://github.com/earendil-works/pi) agent,
under one fault-injection configuration. A scoreboard sandbox counts failures per lane and
runs them as a retro pixel-arcade race in the browser, while a zellij wall on the host shows
every lane's live terminal.

- **Research & build plan (living doc):** https://claude.ai/code/artifact/b10bce23-195d-4e31-8c3d-0b65f2d6ed4a
- **Decisions and milestones:** [docs/PLAN.md](docs/PLAN.md)

## Layout (planned)

| Path | What |
| --- | --- |
| `harness/` | Lane launcher, PTY driver, failure detectors, pi `game.ts` extension |
| `scoreboard/` | Go service: `POST /events`, SQLite, SSE, embedded race UI |
| `wall/` | `wall-gen` (zellij KDL layouts) and `wall-pager` |
| `loadouts/lane/` | Minimal loadout for a lane session |
| `scaletest/` | Inference probe and host-density ramp |

Packages (games, fault tools, seed shim) live in a separate sideload repo, `torment-pkgs`.
