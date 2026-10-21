#!/bin/bash
# End-to-end check of the tool-call recovery shim without live inference:
#   mock_openai.py --text-tool-shape S  <-  toolfix.py  <-  pi + game.ts  ->  tmux toy program
# For each shape (qwen, glm, gptoss) the lane must make real send_keys actions with toolfix
# on (the default), and none with NEXUS_TOOLFIX=0 (control).
#
#   lane/test_toolfix_e2e.sh [seconds-per-run]      (needs node, tmux, pi; see lane/README.md)
set -euo pipefail

LANE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$LANE")
SECS=${1:-20}
: "${NEXUS_PI_BIN:=$ROOT/.build/pi/node_modules/.bin/pi}"
export NEXUS_PI_BIN
WORK=$(mktemp -d "${TMPDIR:-/tmp}/toolfix-e2e.XXXXXX")
free_port() { python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])"; }

pids=()
cleanup() {
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
    for s in "$WORK"/sock-*; do [ -e "$s" ] && tmux -L "$(basename "$s")" kill-server 2>/dev/null || true; done
}
trap cleanup EXIT

fail=0
run() {  # shape toolfix(0|1)
    local shape=$1 tf=$2 out=$WORK/$1-tf$2 sock=tfe2e-$$-$1-$2 port
    mkdir -p "$out"; touch "$WORK/sock-$sock"
    port=$(free_port)
    python3 "$LANE/mock_openai.py" --port "$port" --latency 0.05 --text-tool-shape "$shape" \
        --log "$out/mock.ndjson" 2>"$out/mock.log" &
    pids+=($!)
    tmux -L "$sock" -f "$LANE/tmux.conf" new-session -d -s game -x 80 -y 24 cat
    NEXUS_OUT=$out NEXUS_TOOLFIX=$tf NEXUS_INFERENCE_URL=http://127.0.0.1:$port/v1 \
        NEXUS_AGENT_SECONDS=$SECS NEXUS_ACTION_INTERVAL_MS=300 NEXUS_USE_HARNESS=0 \
        NEXUS_TMUX_SOCKET=$sock NEXUS_MODEL=@cf/qwen/qwen3-30b-a3b-fp8 NEXUS_THINKING=off \
        PI_CODING_AGENT_DIR=$out/pi "$LANE/agent.sh" >"$out/agent.log" 2>&1 || true
    tmux -L "$sock" capture-pane -p -t game >"$out/screen.txt" 2>/dev/null || true
    tmux -L "$sock" kill-server 2>/dev/null || true
    python3 - "$out" "$shape" "$tf" <<'EOF' || fail=1
import json, os, sys
out, shape, tf = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
s = json.load(open(os.path.join(out, "agent", "stats.json")))
screen = open(os.path.join(out, "screen.txt")).read().strip()
tfs_p = os.path.join(out, "agent", "toolfix.stats.json")
tfs = json.load(open(tfs_p)) if os.path.exists(tfs_p) else {}
model = json.load(open(os.path.join(out, "pi", "models.json")))["providers"]["nexus"]["baseUrl"]
msg = (f"{shape:7s} toolfix={int(tf)} baseUrl={model} turns={s['turn']} actions={s['actions']} "
       f"keys={s['keys_sent']} tool_errors={s['tool_errors']} "
       f"recovered={tfs.get('recovered')} none={tfs.get('none')} by_shape={tfs.get('by_shape')} "
       f"screen_chars={len(screen)}")
if tf:
    ok = (s["actions"] >= 3 and s["tool_errors"] == 0 and tfs.get("recovered", 0) >= s["actions"]
          and tfs.get("none") == 0 and len(screen) > 0 and "127.0.0.1" in model)
else:
    ok = s["actions"] == 0 and not tfs
print(("PASS " if ok else "FAIL ") + msg)
sys.exit(0 if ok else 1)
EOF
}

for shape in qwen glm gptoss; do
    run "$shape" 1
    run "$shape" 0
done
# The proxy must not outlive its lane.
sleep 2
if grep -lsa "toolfix.py" /proc/[0-9]*/cmdline | xargs -r grep -lsa "$WORK" >/dev/null; then
    echo "FAIL toolfix still running"; fail=1
else
    echo "PASS toolfix exited with its lanes"
fi
echo "work dir: $WORK"
exit $fail
