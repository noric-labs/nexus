# lane/

Everything that runs inside one lane's session for the pi agent side: the pi extension, the
model config, the driver, a mock inference server, and the `nexus-lane` Minimal loadout.

| File | What |
| --- | --- |
| `game.ts` | pi extension: tools `send_keys(keys)` and `screen()`, context trimming, 5 s pacing, turn/token caps, `stats.json` |
| `models.json` | pi provider `nexus` → broker route `/workers-ai/v1`, the 4 Workers AI models |
| `agent.sh` | lane entry point: env defaults, renders `models.json`, execs `agent.py` |
| `agent.py` | RPC driver: starts pi, sends the goal prompt, re-prompts when a run settles, backs off on errors, restarts pi |
| `prompts/` | `system.md` plus one goal prompt per `NEXUS_GAME` (`default.md` otherwise) |
| `mock_openai.py` | OpenAI-compatible mock that answers with `send_keys`/`screen` tool calls |
| `loadout/` | the `nexus-lane` loadout (`loadout.toml`, `hooks/on-activate.sh`, `vendor-pi.sh`) |

## How the agent plays

`agent.sh` runs:

```
pi --mode rpc --offline --no-builtin-tools --no-extensions --no-skills --no-prompt-templates \
   --no-context-files --no-themes -e lane/game.ts --system-prompt lane/prompts/system.md \
   --provider nexus --model "$NEXUS_MODEL" --thinking "$NEXUS_THINKING" --no-session
```

The model only has `send_keys` and `screen`. `send_keys` waits until 5 s after the previous
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
| `NEXUS_GAME` | – | picks `prompts/<game>.md` |
| `NEXUS_OUT` | `/out` (`/home/out` in the loadout) | artifacts |
| `NEXUS_INFERENCE_URL` | `http://100.64.255.254:8899/workers-ai/v1` | OpenAI-compatible base URL |
| `NEXUS_ACTION_INTERVAL_MS` | `5000` | minimum gap between `send_keys` calls |
| `NEXUS_KEEP_TURNS` | `3` | exchanges kept in context |
| `NEXUS_MAX_TURNS`, `NEXUS_MAX_TOKENS` | `0` (off) | caps; when hit, tools are blocked, pi shuts down, `stats.capped` is set and `agent.sh` exits 0 |
| `NEXUS_THINKING` | `low` | pi thinking level (clamped per model) |
| `NEXUS_AGENT_SECONDS` | `0` (forever) | stop after this long |
| `NEXUS_MAX_KEYS_PER_CALL` | `40` | reject longer batches |
| `NEXUS_TMUX_TARGET`, `NEXUS_TMUX_SOCKET` | `game`, default socket | tmux target / `-L` socket |
| `NEXUS_PI_BIN` | `pi` on PATH, else `$NEXUS_PI_PREFIX/node_modules/.bin/pi` | |
| `NEXUS_RPC_LOG` | off | path to tee pi's RPC stdout |

## Testing without Cloudflare

```sh
python3 lane/mock_openai.py --port 18899 --log /tmp/mock.ndjson &
tmux -f lane/tmux.conf new-session -d -s game -x 80 -y 24 cat     # or any game
NEXUS_OUT=/tmp/out NEXUS_INFERENCE_URL=http://127.0.0.1:18899/v1 NEXUS_AGENT_SECONDS=60 lane/agent.sh
cat /tmp/out/agent/stats.json
```

`--fail-rate 0.3` makes the mock answer 30% of requests with a 429; `--text-every N` makes
every Nth reply plain text, to exercise the re-prompt path.

## Installing pi

The loadout's `on_activate` installs pi 1.3.0 (`@earendil-works/pi-coding-agent`, Node >= 22.19)
into `/home/.local/share/nexus-pi`:

1. from `vendor/pi.tgz` if the loadout ships one (run `lane/loadout/vendor-pi.sh` once on the
   host: ~25 MB, extracts in under a second, no registry egress needed), else
2. `npm install --prefix … @earendil-works/pi-coding-agent@$NEXUS_PI_VERSION` (~10 s, needs
   egress to `registry.npmjs.org`).

## Host commands (M1: scoreboard + 3 lanes)

Install the loadout. Copy it rather than symlinking it, because Minimal rejects hook scripts
reached through symlinks:

```sh
cd ~/src/nexus
lane/loadout/vendor-pi.sh                              # optional, recommended
mkdir -p ~/.config/minimal/loadouts
rm -rf ~/.config/minimal/loadouts/nexus-lane
cp -R lane/loadout ~/.config/minimal/loadouts/nexus-lane
```

Scoreboard session. It gets its own IP and a static ingress for port 8080, and the host
reaches it through a forward:

```sh
min session activate scoreboard --name scoreboard --network own_ip --ingress 8080:8080
min session exec scoreboard 'cd /workbench && ./run.sh'   # however scoreboard/ starts; listens on :8080
min net forward scoreboard 8080:8080                      # race UI at http://localhost:8080
```

Three lanes. Each lane gets its own IP and may reach only the scoreboard by DNS and the broker.
Add `--allow-dns-hosts registry.npmjs.org` if `vendor/pi.tgz` is not shipped:

```sh
lane() {  # lane <id> <game> <model>
  NEXUS_LANE_ID=$1 NEXUS_GAME=$2 NEXUS_MODEL=$3 NEXUS_FAULT=none NEXUS_SEED=$RANDOM \
  min session activate . --name "$1" \
      --loadout broker --loadout nexus-lane \
      --network own_ip \
      --allow-dns-hosts scoreboard.min.internal \
      --allow-subnets 100.64.255.254/32
}
lane l-001 frotz-zork1 @cf/qwen/qwen3-30b-a3b-fp8
lane l-002 brogue      @cf/zai-org/glm-4.7-flash
lane l-003 greed       @cf/openai/gpt-oss-20b
```

Run the commands from the nexus checkout, so that the repo is synced to `/workbench` and
`NEXUS_LANE_DIR=/workbench/lane`. The harness launcher (`harness/`) starts the game in tmux
session `game` and then `lane/agent.sh`. To start the agent by hand in a lane whose game is
already running:

```sh
min session exec l-001 'cd /workbench && nohup lane/agent.sh >/home/out/agent/agent.log 2>&1 &'
min session exec l-001 'cat /home/out/agent/stats.json'
```

Destroy a lane (the launcher brings it back) with `min session destroy -f l-002`.
