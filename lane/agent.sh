#!/bin/sh
# Lane agent entry point: pi (RPC mode, game.ts tools only) playing the game in tmux
# session `game`, paced at one action per ~5 s. Runs in the foreground until stopped,
# capped, or NEXUS_AGENT_SECONDS elapses; the harness owns restarts across heats.
#
# Outputs (all under $NEXUS_OUT/agent/):
#   stats.json      written by game.ts after every model turn and action; the harness
#                   heartbeat reads turn, tokens_in, tokens_out from it. Fields:
#                   v lane_id game model pid started updated turn tokens_in tokens_out
#                   last_tokens_in actions keys_sent tool_errors last_action_ts last_error capped
#   events.ndjson   one line per agent error ({"ts","type":"agent_error","detail",...})
#   pi.stderr.log   pi's diagnostics
set -eu

LANE_DIR=$(cd "$(dirname "$0")" && pwd)

: "${NEXUS_OUT:=/home/out}"
: "${NEXUS_MODEL:=@cf/qwen/qwen3-30b-a3b-fp8}"
: "${NEXUS_GAME:=}"
: "${NEXUS_INFERENCE_URL:=http://100.64.255.254:8899/workers-ai/v1}"
: "${NEXUS_ACTION_INTERVAL_MS:=5000}"
: "${NEXUS_KEEP_TURNS:=3}"
: "${NEXUS_MAX_TURNS:=0}"
: "${NEXUS_MAX_TOKENS:=0}"
: "${NEXUS_THINKING:=low}"
: "${PI_CODING_AGENT_DIR:=${HOME:-/tmp}/.pi/nexus-agent}"
export NEXUS_OUT NEXUS_MODEL NEXUS_GAME NEXUS_INFERENCE_URL NEXUS_ACTION_INTERVAL_MS \
       NEXUS_KEEP_TURNS NEXUS_MAX_TURNS NEXUS_MAX_TOKENS NEXUS_THINKING PI_CODING_AGENT_DIR
export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0

# pi: explicit NEXUS_PI_BIN, else PATH, else the loadout's install prefix.
if [ -z "${NEXUS_PI_BIN:-}" ]; then
    if command -v pi >/dev/null 2>&1; then NEXUS_PI_BIN=$(command -v pi)
    else NEXUS_PI_BIN=${NEXUS_PI_PREFIX:-$HOME/.local/share/nexus-pi}/node_modules/.bin/pi; fi
fi
export NEXUS_PI_BIN
[ -x "$NEXUS_PI_BIN" ] || { echo "agent.sh: pi not found ($NEXUS_PI_BIN)" >&2; exit 127; }

# models.json has no env interpolation for baseUrl, so render it per lane.
mkdir -p "$PI_CODING_AGENT_DIR" "$NEXUS_OUT/agent"
python3 - "$LANE_DIR/models.json" "$PI_CODING_AGENT_DIR/models.json" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1]))
d["providers"]["nexus"]["baseUrl"] = os.environ["NEXUS_INFERENCE_URL"].rstrip("/")
json.dump(d, open(sys.argv[2], "w"), indent=1)
PY

exec python3 "$LANE_DIR/agent.py"
