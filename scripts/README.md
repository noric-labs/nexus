# scripts

## `slice.sh`: the M1 local slice

Runs, in one box:

- the scoreboard and the mock model on free local ports;
- three lanes, each a `nexus-lane run` harness plus a pi agent (`lane/agent.sh`).

Lanes are isolated by tmux socket. In production each lane is its own Minimal session.

**Prerequisites.** Build these into `.build/` (git-ignored):

```sh
(cd scoreboard && CGO_ENABLED=0 go build -o ../.build/scoreboard .)
npm install --prefix .build/pi @earendil-works/pi-coding-agent
```

**Running it.** Run it as a Minimal task, so the sideloaded games (`frotz`, `brogue-ce`,
`greed`, `libfiu`, …) are on PATH. Declare the task in the workspace `minimal.toml`:

```toml
[tasks.slice]
packages = ["base", "bash", "coreutils", "curl", "python", "node", "tmux", "frotz", "zork-stories", "brogue-ce", "greed", "libfiu", "torment-seed"]
exec = "bash nexus/scripts/slice.sh"
```

Then run `min task run slice`.

**Knobs:**

- `SLICE_SECONDS` (run length, default 240)
- `SLICE_HEAT` (default 180s) and `SLICE_PIT` (default 15s)
- `SLICE_LANES="id game fault seed;..."` (the lanes to run)

**Output.** Everything goes to `.build/slice-<timestamp>/`: the scoreboard DB and log,
`state.json`, and per-lane harness and agent logs, stats, failure cards and last screens.
