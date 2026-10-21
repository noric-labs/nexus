#!/bin/bash
# One lane: the harness (game in tmux session `game`) plus the pi agent. This is one
# iteration of slice.sh's lane loop, for a box that holds exactly one lane.
#
# Entry point of the `lane` task (minimal.toml) and of scripts/host-up.sh, which starts
# it in each lane session with `min session exec <lane> 'setsid nohup bash scripts/lane.sh ...'`.
#
# Lane config, later sources win:
#   1. the environment (NEXUS_* as in docs/CONTRACTS.md);
#   2. $NEXUS_ENV_FILE (default $HOME/.nexus/lane.env), KEY=VALUE lines. host-up.sh writes it,
#      because `min session run` forwards no client env (docs: reference/tasks#env_vars);
#   3. flags: --id --game --fault --seed --model --scoreboard --mock 0|1 --seconds N
#      (the `lane` task maps its args onto these; an empty value means "not given").
#
# NEXUS_MOCK=1 runs lane/mock_openai.py inside the box and points pi at it instead of
# the broker, so no Workers AI spend. NEXUS_LANE_SECONDS>0 stops the lane after that long.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${HOME:=/home}"
ENV_FILE=${NEXUS_ENV_FILE:-$HOME/.nexus/lane.env}
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || { echo "lane.sh: $1 needs a value" >&2; exit 2; }
    v=$2
    if [ -n "$v" ]; then
        case $1 in
            --id) NEXUS_LANE_ID=$v ;;        --game) NEXUS_GAME=$v ;;
            --fault) NEXUS_FAULT=$v ;;       --seed) NEXUS_SEED=$v ;;
            --model) NEXUS_MODEL=$v ;;       --scoreboard) NEXUS_SCOREBOARD_URL=$v ;;
            --mock) NEXUS_MOCK=$v ;;         --seconds) NEXUS_LANE_SECONDS=$v ;;
            *) echo "lane.sh: unknown flag $1" >&2; exit 2 ;;
        esac
    fi
    shift 2
done

: "${NEXUS_LANE_ID:?set NEXUS_LANE_ID (env, $ENV_FILE or --id)}"
: "${NEXUS_GAME:?set NEXUS_GAME (env, $ENV_FILE or --game)}"
: "${NEXUS_FAULT:=none}" "${NEXUS_SEED:=1}" "${NEXUS_MODEL:=@cf/qwen/qwen3-30b-a3b-fp8}"
: "${NEXUS_SCOREBOARD_URL:=http://scoreboard.min.internal:8080}"
: "${NEXUS_OUT:=$HOME/out}" "${NEXUS_MOCK:=0}" "${NEXUS_LANE_SECONDS:=0}"
: "${NEXUS_INFERENCE_URL:=http://100.64.255.254:8899/workers-ai/v1}"
# One tmux socket directory for the harness, the agent and lane/ttyview.py (the wall).
# It lives in the session home, which every exec and task in the session shares.
: "${TMUX_TMPDIR:=$HOME/.nexus/tmux}"
: "${NEXUS_PI_PREFIX:=$HOME/.local/share/nexus-pi}"
: "${PI_CODING_AGENT_DIR:=$HOME/.pi/nexus-agent}"
export NEXUS_LANE_ID NEXUS_GAME NEXUS_FAULT NEXUS_SEED NEXUS_MODEL NEXUS_SCOREBOARD_URL \
       NEXUS_OUT NEXUS_INFERENCE_URL TMUX_TMPDIR NEXUS_PI_PREFIX PI_CODING_AGENT_DIR
export TERM=${TERM:-xterm-256color} NEXUS_LANE_DIR=$ROOT/lane

STATE=$HOME/.nexus
mkdir -p "$STATE/bin" "$NEXUS_OUT/agent" "$PI_CODING_AGENT_DIR"
mkdir -p -m 700 "$TMUX_TMPDIR"
log() { echo "lane.sh[$NEXUS_LANE_ID]: $*"; }

# One lane per box: the tmux session name `game` is fixed.
if [ -f "$STATE/lane.pid" ] && kill -0 "$(cat "$STATE/lane.pid")" 2>/dev/null; then
    log "already running (pid $(cat "$STATE/lane.pid"))"; exit 1
fi
echo $$ > "$STATE/lane.pid"

cat > "$STATE/bin/nexus-lane" <<EOF
#!/bin/sh
PYTHONPATH=$ROOT/harness/src exec python3 -m nexus_harness.cli "\$@"
EOF
chmod +x "$STATE/bin/nexus-lane"
export PATH=$STATE/bin:$PATH

# pi: NEXUS_PI_BIN, else the install prefix. Loadout hooks do not run in the current
# Minimal release (docs: concepts/sessions, "Hooks are inert"), so install it here:
# from a vendored tarball if one is in the box, else from npm (needs registry egress).
# Preferred: the pi-next sideload package (session packages) puts pi on PATH.
PI=${NEXUS_PI_BIN:-$(command -v pi || echo "$NEXUS_PI_PREFIX/node_modules/.bin/pi")}
if [ ! -x "$PI" ]; then
    mkdir -p "$NEXUS_PI_PREFIX"
    for tgz in "$HOME/.cache/nexus-lane/pi.tgz" "$ROOT/lane/loadout/vendor/pi.tgz"; do
        if [ -s "$tgz" ]; then tar -xzf "$tgz" -C "$NEXUS_PI_PREFIX" && log "pi from $tgz" && break; fi
    done
fi
if [ ! -x "$PI" ]; then
    log "installing pi ${NEXUS_PI_VERSION:-1.3.0} from npm"
    npm install --prefix "$NEXUS_PI_PREFIX" --no-fund --no-audit --loglevel=error \
        "@earendil-works/pi-coding-agent@${NEXUS_PI_VERSION:-1.3.0}"
fi
[ -x "$PI" ] || { log "pi missing at $PI"; exit 127; }
export NEXUS_PI_BIN=$PI

pids=()
cleanup() {
    # Agent first, then the harness, so its lane_end goes out while the box is up.
    for ((i=${#pids[@]}-1; i>=0; i--)); do kill "${pids[$i]}" 2>/dev/null || true; done
    wait 2>/dev/null || true
    rm -f "$STATE/lane.pid"
}
trap cleanup EXIT
trap 'exit 143' TERM INT

if [ "$NEXUS_MOCK" = 1 ]; then
    port=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])")
    python3 "$ROOT/lane/mock_openai.py" --port "$port" --log "$NEXUS_OUT/mock.ndjson" \
        > "$NEXUS_OUT/mock.log" 2>&1 &
    pids+=($!)
    NEXUS_INFERENCE_URL=http://127.0.0.1:$port/v1
    log "mock model at $NEXUS_INFERENCE_URL"
fi

log "game=$NEXUS_GAME fault=$NEXUS_FAULT seed=$NEXUS_SEED scoreboard=$NEXUS_SCOREBOARD_URL out=$NEXUS_OUT"
nexus-lane run > "$NEXUS_OUT/harness.log" 2>&1 &
harness=$!
pids+=($harness)
for _ in $(seq 40); do tmux has-session -t game 2>/dev/null && break; sleep 0.5; done
tmux has-session -t game 2>/dev/null || log "warning: tmux session game not up yet (see $NEXUS_OUT/harness.log)"

"$ROOT/lane/agent.sh" > "$NEXUS_OUT/agent/agent.log" 2>&1 &
pids+=($!)

if [ "$NEXUS_LANE_SECONDS" -gt 0 ]; then
    ( sleep "$NEXUS_LANE_SECONDS"; kill -TERM $$ ) &
    pids+=($!)
fi
# The harness owns the lane: when it ends (stopped, NEXUS_MAX_RUNS, bad fault), so does the lane.
set +e
wait "$harness"
rc=$?
log "harness exited $rc"
exit "$rc"
