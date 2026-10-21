# lane/

Everything that runs inside one lane's session for the pi agent side: the pi extension, the
model config, the driver, a mock inference server, and the `nexus-lane` Minimal loadout.

| File | What |
| --- | --- |
| `game.ts` | pi extension: tools `send_keys(keys)` and `screen()`, context trimming, 1 s pacing, turn/token caps, `stats.json` |
| `models.json` | pi provider `nexus` → broker route `/workers-ai/v1`, the 4 Workers AI models |
| `agent.sh` | lane entry point: env defaults, renders `models.json`, execs `agent.py` |
| `agent.py` | RPC driver: starts pi, sends the goal prompt, re-prompts when a run settles, backs off on errors, restarts pi |
| `prompts/` | `system.md` plus one goal prompt per `NEXUS_GAME` (`default.md` otherwise) |
| `mock_openai.py` | OpenAI-compatible mock that answers with `send_keys`/`screen` tool calls |
| `toolfix.py` | local proxy between pi and the broker that turns text-encoded tool calls into `tool_calls` (D-016) |
| `test_toolfix.py`, `test_toolfix_e2e.sh` | toolfix unit/proxy tests and the pi + tmux end-to-end check (mock only) |
| `loadout/` | the `nexus-lane` loadout (`loadout.toml`, `hooks/on-activate.sh`, `vendor-pi.sh`) |
| `ttyview.py` | read-only view of the lane's `game` tmux session through a pty, for the wall (see below) |

## How the agent plays

`agent.sh` runs:

```
pi --mode rpc --offline --no-builtin-tools --no-extensions --no-skills --no-prompt-templates \
   --no-context-files --no-themes -e lane/game.ts --system-prompt lane/prompts/system.md \
   --provider nexus --model "$NEXUS_MODEL" --thinking "$NEXUS_THINKING" --no-session
```

The model only has `send_keys` and `screen`. `send_keys` waits until 1 s after the previous
action (model latency counts toward that), types the keys, waits 400 ms, and returns the
new screen, so a model usually calls only `send_keys`. Key notation: literal text, plus
`<Enter> <Esc> <Tab> <BSpace> <Space> <Up> <Down> <Left> <Right> <Home> <End> <PgUp> <PgDn>
<Del> <Ins> <F1>..<F12> <C-x> <M-x> <lt>`. A newline in the string counts as Enter.

Keys and screens go through `nexus-lane keys <text>` / `nexus-lane screen` when the
harness CLI is on PATH (the raw agent string is passed through, so the harness parses the
notation above), else straight to `tmux send-keys -t game` / `tmux capture-pane -p -t game`.
Set `NEXUS_USE_HARNESS=0` to force tmux.

The `context` hook sends the goal prompt plus the last `NEXUS_KEEP_TURNS` (3) exchanges,
and blanks every screen except the newest, so tokens per call stay flat (about 1k tokens per
call against the mock).

## Stats for the harness heartbeat

`game.ts` rewrites `$NEXUS_OUT/agent/stats.json` atomically after every model turn and every
action (override the path with `NEXUS_AGENT_STATS`):

```json
{"v":1,"lane_id":"l-001","game":"greed","model":"@cf/qwen/qwen3-30b-a3b-fp8","pid":123,
 "started":"…","updated":"…","turn":18,"tokens_in":17505,"tokens_out":220,"last_tokens_in":877,
 "actions":13,"keys_sent":85,"tool_errors":0,"last_action_ts":"…","last_error":"","capped":""}
```

`turn`, `tokens_in` and `tokens_out` map directly onto the heartbeat fields. Agent errors
(provider 429s/5xx after pi's own retries, pi exits) are appended to
`$NEXUS_OUT/agent/events.ndjson` as `{"ts","type":"agent_error","lane_id","model","detail"}`
for the harness to forward. pi's stderr goes to `$NEXUS_OUT/agent/pi.stderr.log`.

## Environment

| Var | Default | Meaning |
| --- | --- | --- |
| `NEXUS_MODEL` | `@cf/qwen/qwen3-30b-a3b-fp8` | one of the 4 ids in `models.json` |
| `NEXUS_GAME` | – | picks `prompts/<game>.md`, else the name without `-rel`, else `default.md` |
| `NEXUS_PLAY_STYLE` | `explore` | `explore`: `prompts/system.md`, odd input welcome (bug hunting); `win`: `prompts/system-win.md`, play for progress (`host-up.sh --no-faults`) |
| `NEXUS_OUT` | `/out` (`/home/out` in the loadout) | artifacts |
| `NEXUS_INFERENCE_URL` | `http://100.64.255.254:8899/workers-ai/v1` | OpenAI-compatible base URL |
| `NEXUS_ACTION_INTERVAL_MS` | `1000` | minimum gap between `send_keys` calls |
| `NEXUS_KEEP_TURNS` | `3` | exchanges kept in context |
| `NEXUS_MAX_TURNS`, `NEXUS_MAX_TOKENS` | `0` (off) | caps; when hit, tools are blocked, pi shuts down, `stats.capped` is set and `agent.sh` exits 0 |
| `NEXUS_THINKING` | `low` | pi thinking level (clamped per model) |
| `NEXUS_AGENT_SECONDS` | `0` (forever) | stop after this long |
| `NEXUS_MAX_KEYS_PER_CALL` | `40` | reject longer batches |
| `NEXUS_TMUX_TARGET`, `NEXUS_TMUX_SOCKET` | `game`, default socket | tmux target / `-L` socket |
| `NEXUS_PI_BIN` | `pi` on PATH, else `$NEXUS_PI_PREFIX/node_modules/.bin/pi` | |
| `NEXUS_RPC_LOG` | off | path to tee pi's RPC stdout |
| `NEXUS_TOOLFIX` | `1` | run `toolfix.py` in front of `NEXUS_INFERENCE_URL` and point pi at it; `0` = pi talks to the URL directly |

## Testing without Cloudflare

```sh
python3 lane/mock_openai.py --port 18899 --log /tmp/mock.ndjson &
tmux -f lane/tmux.conf new-session -d -s game -x 80 -y 24 cat     # or any game
NEXUS_OUT=/tmp/out NEXUS_INFERENCE_URL=http://127.0.0.1:18899/v1 NEXUS_AGENT_SECONDS=60 lane/agent.sh
cat /tmp/out/agent/stats.json
```

`--fail-rate 0.3` makes the mock answer 30% of requests with a 429; `--text-every N` makes
every Nth reply plain text, to exercise the re-prompt path; `--text-tool-shape
qwen|glm|gptoss|cycle` writes each call the way that Workers AI model does (as text, not
`tool_calls`).

## Tool-call recovery (`toolfix.py`)

Only Gemma returns structured `tool_calls` through Workers AI (D-016). With `NEXUS_TOOLFIX=1`
(the default) `agent.sh` starts `toolfix.py` on a free localhost port and renders pi's
`baseUrl` to it. Requests go upstream unchanged. pi always streams, so for requests that declare
tools the proxy buffers the SSE reply; if it has no `tool_calls` but the content or reasoning
holds a call to a declared tool (`<tool_call>{json}`, `<tool_call>send_keys("…")`,
`functions.send_keys {…}`, a `send_keys(…)` line, or a bare args object like `{"keys":"j"}`),
it re-emits the reply with a proper `tool_calls` entry and `finish_reason: tool_calls`.
Anything else is replayed byte-for-byte. Output: `$NEXUS_OUT/agent/toolfix.jsonl` (one line per
recovery or miss), `toolfix.stats.json` (counts by shape and model; also `GET /toolfix/stats`).
The proxy exits when the agent does.

```sh
python3 lane/test_toolfix.py -v          # exact D-016 shapes, JSON and SSE, plus negatives
lane/test_toolfix_e2e.sh 15              # pi + game.ts + tmux `cat`, each shape, toolfix on/off
```

## Installing pi

pi 1.3.0 (`@earendil-works/pi-coding-agent`, Node >= 22.19) goes into
`/home/.local/share/nexus-pi`. `scripts/lane.sh` installs it on first start:

1. from `$HOME/.cache/nexus-lane/pi.tgz`, which the loadout patches in when it ships
   `vendor/pi.tgz` (run `lane/loadout/vendor-pi.sh` once on the host: ~25 MB, extracts in under a
   second, no registry egress needed), else
2. `npm install --prefix … @earendil-works/pi-coding-agent@$NEXUS_PI_VERSION` (~10 s, needs
   egress to `registry.npmjs.org`; `scripts/host-up.sh` allows it when no tarball is vendored).

The loadout's `on_activate` hook does the same, but the Minimal docs say loadout hooks are not run
in the current release ([concepts/sessions](https://minimal.dev/docs/concepts/sessions): "hooks
declared in loadouts are excluded from composition entirely"), so `lane.sh` does not rely on it.

## Running lanes as Minimal sessions

`scripts/host-up.sh` does all of this from the Mac; the steps and prerequisites are in
[`scripts/README.md`](../scripts/README.md#running-on-minimal-sessions). Per lane it runs:

```sh
min session activate --name l-001 --no-prompt --network own_ip --loadout nexus-lane \
    --allow-dns-hosts scoreboard.min.internal [--allow-dns-hosts registry.npmjs.org] ~/src/nexus
min session exec l-001 'mkdir -p $HOME/.nexus && printf %s\\n NEXUS_LANE_ID=l-001 … > $HOME/.nexus/lane.env'
min session exec l-001 'cd /workbench && setsid nohup bash scripts/lane.sh >$HOME/.nexus/lane.log 2>&1 </dev/null &'
```

- The checkout is the session path, so the repo is at `/workbench` and the lane code at
  `/workbench/lane`.
- **Lane config** is `$HOME/.nexus/lane.env` (`NEXUS_LANE_ID`, `NEXUS_GAME`, `NEXUS_FAULT`,
  `NEXUS_SEED`, `NEXUS_MODEL`, `NEXUS_SCOREBOARD_URL`, `NEXUS_MOCK`), not activation-time env:
  `min session run` forwards no client environment
  ([reference/tasks](https://minimal.dev/docs/reference/tasks#env_vars)), and a file survives a
  restart of the lane inside the session.
- `scripts/lane.sh` is the `lane` task's entry point too. It starts the harness, waits for tmux
  session `game`, then runs `lane/agent.sh`. It ends when the harness ends, and SIGTERM stops
  both, so `lane_end` is sent. `NEXUS_MOCK=1` runs `mock_openai.py` in the box instead of using the
  broker; `--live` on `host-up.sh` adds `--loadout broker` and runs the lanes `host_ip` (D-035).
- **Why `setsid nohup … &`:** `min session exec` returns when its command exits, and it ends the
  command's whole process group if the client goes away first. `setsid` moves the lane into its
  own group, so it outlives the exec ([cli-min, "Backgrounding a
  command"](https://minimal.dev/docs/reference/cli-min#session-exec)).
- **Why not `min session run l-001 lane`:** a task in an `own_ip` session runs in a second box
  beside the session's box, and on a Mac that box has no outbound reach yet
  ([reference/tasks#networking](https://minimal.dev/docs/reference/tasks#networking)). Use
  `host-up.sh --via run --network host_ip` for that path.
- **tmux socket:** `lane.sh` sets `TMUX_TMPDIR=$HOME/.nexus/tmux`, so the harness, the agent and
  `ttyview.py` find the same server in any exec or task of the session.

Useful commands:

```sh
min session exec l-001 'tail -n 40 $HOME/.nexus/lane.log $HOME/out/harness.log $HOME/out/agent/agent.log'
min session exec l-001 'cat $HOME/out/agent/stats.json'
min session exec l-001 'kill $(cat $HOME/.nexus/lane.pid)'          # stop the lane (lane_end is sent)
min session exec l-001 'cd /workbench && setsid nohup bash scripts/lane.sh >$HOME/.nexus/lane.log 2>&1 </dev/null &'
min session destroy -f l-001
```

## Watching a lane (`ttyview.py`)

`min session exec` is non-interactive: it relays stdout and stderr but gives the command no
terminal, and `tmux attach` then exits with `open terminal failed: not a terminal`.
`ttyview.py` opens a pty in the box, sized to the game window, runs `tmux attach -r -t game` on
it, and streams the pty's output to stdout. Your terminal renders it as a read-only attach:

```sh
min session exec l-001 'exec python3 /workbench/lane/ttyview.py'
```

The wall uses this as its pane command (`wall-gen --cmd`). Keystrokes are not forwarded.
