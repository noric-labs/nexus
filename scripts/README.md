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
`greed`, `libfiu`, …) are on PATH. The repo's `minimal.toml` declares it (the sideload packages
must be listed in a task's `packages` by hand; `min add` cannot add them). From the checkout:

```sh
min task run slice
```

The task builds `.build/scoreboard` and installs `.build/pi` first if needed.

**Knobs:**

- `SLICE_SECONDS` (run length, default 240)
- `SLICE_HEAT` (default 180s) and `SLICE_PIT` (default 15s)
- `SLICE_LANES="id game fault seed;..."` (the lanes to run)

**Output.** Everything goes to `.build/slice-<timestamp>/`: the scoreboard DB and log,
`state.json`, and per-lane harness and agent logs, stats, failure cards and last screens.

## Running on Minimal sessions

`host-up.sh` runs the same pieces as real Minimal sessions, from the Mac: one session for the
scoreboard, one per lane, the scoreboard forwarded to `localhost:8080`, and the wall in zellij.
`host-down.sh` tears it down.

| File | What |
| --- | --- |
| `host-up.sh`, `host-down.sh` | run on the Mac, from the nexus checkout |
| `lanes.txt` | the lanes: `id game fault seed [model]`; the session is named after the id |
| `scoreboard.sh` | builds and runs the scoreboard on `0.0.0.0:8080` (the `scoreboard` task) |
| `lane.sh` | one lane: harness + pi agent (the `lane` task) |

### Prerequisites on the Mac

- `min`, installed and set up (`min version` answers; run `min finalize-install` if `min
  session activate` asks for it, so `<box>.min.internal` names resolve).
- `uv` (`brew install uv`) for `wall-gen` and `wall-pager`, and `zellij` (`brew install zellij`).
- A wall terminal configured from `wall/terminals/` (`ghostty.conf`, `kitty.conf` or
  `wezterm.lua`): about 4 pt font, zero padding, non-native fullscreen. See `wall/README.md`.
- Optional: `lane/loadout/vendor-pi.sh` (needs `npm`) to ship pi with the loadout. Without it,
  every lane npm-installs pi on first start and is allowed to reach `registry.npmjs.org`.
- For `--live` only: the `broker` loadout (`min loadout list`). Live runs spend Workers AI
  credit, so the default is the in-box mock model.
- If activation stops on a policy prompt (`--no-prompt` makes it fail and print a snippet), add
  the snippet to `~/.config/minimal/user_policy.toml`. The `nexus-lane` loadout inherits
  `NEXUS_*` names, so `[vars] allow = ["NEXUS_*"]` is the usual one.

### Steps

```sh
cd ~/src/nexus
scripts/host-up.sh --dry-run          # print every command, run nothing
scripts/host-up.sh                    # 3 lanes from scripts/lanes.txt, mock model
#   -n 8 --lanes my-lanes.txt         more lanes;  --heat 3m --pit 15s   short heats
#   --live                            pi -> broker -> Workers AI (spends credit)
```

`host-up.sh` does the following, and stops with a message at the first check that fails:

1. Copies `lane/loadout` to `~/.config/minimal/loadouts/nexus-lane`.
2. `min session activate --name scoreboard --no-loadouts --no-prompt --network own_ip
   --ingress 8080:8080 --allow-dns-hosts proxy.golang.org --allow-dns-hosts sum.golang.org .`
3. `min session exec scoreboard 'cd /workbench && setsid nohup bash scripts/scoreboard.sh
   --heat 15m --pit 1m >$HOME/.nexus/scoreboard.log 2>&1 </dev/null &'`, then waits until
   `curl 127.0.0.1:8080/api/state` answers inside the box. The first build downloads Go modules.
4. `min net forward scoreboard 8080:8080` in the background (pid in `.build/host/forward.pid`),
   then checks `http://localhost:8080`.
5. For each lane: `min session activate --name l-001 --no-prompt --network own_ip --loadout
   nexus-lane --allow-dns-hosts <scoreboard box>.min.internal .`, writes
   `$HOME/.nexus/lane.env` in the box, checks that the box has the tools and games (first lane)
   and can reach the scoreboard, then starts `scripts/lane.sh` with `setsid nohup`.
6. Waits for each lane's `lane_start` on the scoreboard, then generates the wall:
   `uv run --project wall wall-gen --lanes-file .build/host/lanes.json --out wall/out
   --session-fmt '{lane_id}' --cmd "min session exec {session} 'exec python3 /workbench/lane/ttyview.py'"`.

Then show the wall and start the pager:

```sh
# in the fullscreen wall terminal
cd ~/src/nexus/wall && zellij delete-session nexus-wall 2>/dev/null; zellij --config config.kdl -s nexus-wall -n out/wall.kdl
# in any other terminal
cd ~/src/nexus/wall && uv run wall-pager --session nexus-wall --scoreboard http://localhost:8080 --out out
```

Open the race UI at <http://localhost:8080>. To look at one lane or its logs:

```sh
min session exec l-001 'exec python3 /workbench/lane/ttyview.py'
min session exec l-001 'tail -n 40 $HOME/.nexus/lane.log $HOME/out/harness.log $HOME/out/agent/agent.log'
```

Tear down. This saves `/api/state` to `.build/host/state-final.json`, stops the lanes (they send
`lane_end`), stops the forward, and destroys the sessions. `--keep-scoreboard` keeps the
scoreboard and its forward:

```sh
scripts/host-down.sh
```

### How it maps onto Minimal

These follow the [min CLI reference](https://minimal.dev/docs/reference/cli-min) and
[tasks reference](https://minimal.dev/docs/reference/tasks):

- **Sessions and naming.** `min session activate [OPTIONS] [PATH]` with `--name`. Each session
  is activated from the checkout, so the repo is at `/workbench` and `[session].packages` from
  `minimal.toml` (the sideloaded games included) is on PATH.
- **Networking.** `--network own_ip` gives each box its own address. A box with no egress flags
  reaches nothing. Lanes get `--allow-dns-hosts <scoreboard>.min.internal`, plus
  `--allow-subnets 100.64.255.254/32` (the broker) with `--live`. The scoreboard gets
  `--ingress 8080:8080` and egress only to the Go module proxy. Lanes find it by box name:
  `host-up.sh` reads `names: <box>.min.internal` from the activate output.
- **Long-running processes.** `min session exec <s> '<cmd>'` with one argument runs a shell
  command and returns when it exits. A job it backgrounds keeps running, but if the client goes
  away first, the job's process group is killed. Hence `setsid nohup … >log 2>&1 </dev/null &`.
- **Tasks in sessions.** `min session run <session> <task>` runs a declared task against an
  existing session. It takes no task args, forwards no client env, and relays output until the
  task ends. In an `own_ip` session the task runs in a second box: it is not behind the
  session's ingress or `min net forward`, and on a Mac it has no outbound reach. So `host-up.sh`
  defaults to `--via exec`. `--via run --network host_ip` uses the tasks instead: the
  scoreboard defaults to 15m/1m, the lane config comes from `lane.env`, and the boxes share the
  host network with no isolation.
- **Forward.** `min net forward <SESSION> <LOCAL>:<PORT>` stays in the foreground and dials
  `127.0.0.1:<PORT>` inside the box. The docs note that the `own_ip` leg is not yet proven end to
  end. If `localhost:8080` does not answer, `host-up.sh` says so; try `--network host_ip`.
- **The wall.** `min session exec` gives the command no terminal, so a bare
  `tmux attach -r` fails (`open terminal failed: not a terminal`). `lane/ttyview.py` makes the
  pty inside the box and streams a read-only attach (see `lane/README.md`).
- **Teardown.** `min session destroy -f <s>`. `-f` is needed without a terminal.

**Checking `minimal.toml`.** The in-sandbox `min check` reads only its own sandbox's project.
The host `min` documents no `check` command, and `mip check` is Linux-only. On the Mac, the first
`min session activate` or `min task run` from the checkout is what parses the file. The tasks also run directly, for example
`min task run lane --id l-009 --game greed --mock 1 --seconds 120 --scoreboard http://<host>:8080`
or `min task run scoreboard --heat 3m --pit 15s`.
