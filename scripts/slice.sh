#!/bin/bash
# M1 local slice: scoreboard + 3 lanes (harness + pi against the mock model) in one box.
# Lanes are isolated by tmux socket (NEXUS_TMUX_SOCKET) instead of separate sessions.
#
# Needs on PATH: python3, node, tmux, curl, and the games (dfrotz, brogue-ce-asan,
# greed, fiu-run). Expects .build/scoreboard and .build/pi (see scripts/README).
#
#   SLICE_SECONDS=240 SLICE_HEAT=180s SLICE_PIT=15s scripts/slice.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
B=$ROOT/.build
OUT=${SLICE_OUT:-$B/slice-$(date +%Y%m%d-%H%M%S)}
SECONDS_TO_RUN=${SLICE_SECONDS:-240}
free_port() { python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])"; }
PORT=${SLICE_PORT:-$(free_port)}
MOCK_PORT=${SLICE_MOCK_PORT:-$(free_port)}
SB=http://127.0.0.1:$PORT
MODEL=${SLICE_MODEL:-@cf/qwen/qwen3-30b-a3b-fp8}
# SLICE_LIVE=1 sends real inference through the broker (spends Workers AI credit).
if [ "${SLICE_LIVE:-0}" = 1 ]; then
    INFERENCE_URL=${SLICE_INFERENCE_URL:-http://100.64.255.254:8899/workers-ai/v1}
else
    INFERENCE_URL=http://127.0.0.1:$MOCK_PORT/v1
fi

mkdir -p "$OUT/bin"
cat > "$OUT/bin/nexus-lane" <<EOF
#!/bin/sh
PYTHONPATH=$ROOT/harness/src exec python3 -m nexus_harness.cli "\$@"
EOF
chmod +x "$OUT/bin/nexus-lane"
export PATH=$OUT/bin:$PATH HOME=${HOME:-$OUT/home}

pids=()
cleanup() {
    # Lanes first (newest first), so their lane_end reaches the scoreboard before it stops.
    for ((i=${#pids[@]}-1; i>=0; i--)); do kill "${pids[$i]}" 2>/dev/null || true; sleep 0.2; done
    wait 2>/dev/null || true
}
trap cleanup EXIT

"$B/scoreboard" -listen 127.0.0.1:$PORT -db "$OUT/scoreboard.db" \
    -heat "${SLICE_HEAT:-180s}" -pit "${SLICE_PIT:-15s}" > "$OUT/scoreboard.log" 2>&1 &
pids+=($!)
python3 "$ROOT/lane/mock_openai.py" --port "$MOCK_PORT" --log "$OUT/mock.ndjson" \
    > "$OUT/mock.log" 2>&1 &
pids+=($!)
until curl -sf "$SB/api/state" > /dev/null; do sleep 0.5; done

# lane_id game fault seed [model]; override with SLICE_LANES="id game fault seed [model];..."
if [ -n "${SLICE_LANES:-}" ]; then
    IFS=';' read -r -a LANES <<< "$SLICE_LANES"
else
    LANES=(
        "l-001 frotz-zork1 winch-storm 101"
        "l-002 brogue-rel malloc-1pct 202"
        "l-003 greed none 303"
    )
fi
for spec in "${LANES[@]}"; do
    read -r id game fault seed lane_model <<< "$spec"
    lane_model=${lane_model:-$MODEL}
    mkdir -p "$OUT/$id"
    common=(NEXUS_LANE_ID=$id NEXUS_GAME=$game NEXUS_FAULT=$fault NEXUS_SEED=$seed
            NEXUS_MODEL=$lane_model NEXUS_SCOREBOARD_URL=$SB
            NEXUS_OUT=$OUT/$id NEXUS_TMUX_SOCKET=$id TERM=xterm-256color)
    env "${common[@]}" nexus-lane run > "$OUT/$id/harness.log" 2>&1 &
    pids+=($!)
    # Give the harness a moment to create the tmux session before the agent attaches.
    for _ in $(seq 20); do tmux -L "$id" has-session -t game 2>/dev/null && break; sleep 0.5; done
    env "${common[@]}" NEXUS_INFERENCE_URL=$INFERENCE_URL \
        NEXUS_PI_BIN="$B/pi/node_modules/.bin/pi" PI_CODING_AGENT_DIR="$OUT/$id/pi" \
        "$ROOT/lane/agent.sh" > "$OUT/$id/agent.log" 2>&1 &
    pids+=($!)
done

echo "slice running for ${SECONDS_TO_RUN}s (model $MODEL via $INFERENCE_URL); output in $OUT"
end=$((SECONDS + SECONDS_TO_RUN))
while [ $SECONDS -lt $end ]; do sleep 5; done

curl -sf "$SB/api/state" > "$OUT/state.json"
for spec in "${LANES[@]}"; do
    read -r id _ <<< "$spec"
    curl -sf "$SB/api/lanes/$id" > "$OUT/$id/card.json" || true
    tmux -L "$id" capture-pane -p -t game > "$OUT/$id/last-screen.txt" 2>/dev/null || true
done
python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
st = json.load(open(os.path.join(out, "state.json")))
print("heat:", st["heat"]["id"], st["heat"]["phase"])
print("scores:", st.get("scores"))
for lane in sorted(st.get("lanes", {}) if isinstance(st.get("lanes"), dict) else []):
    print(" ", lane)
for d in sorted(os.listdir(out)):
    stats = os.path.join(out, d, "agent", "stats.json")
    if os.path.exists(stats):
        s = json.load(open(stats))
        print(f"{d}: turns={s.get('turn')} actions={s.get('actions')} keys={s.get('keys_sent')} "
              f"tok_in={s.get('tokens_in')} capped={s.get('capped')}")
    card = os.path.join(out, d, "card.json")
    if os.path.exists(card):
        c = json.load(open(card))
        fails = c.get("failures") or []
        kinds = {}
        for f in fails:
            kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
        print(f"{d}: failures={kinds}")
PY
