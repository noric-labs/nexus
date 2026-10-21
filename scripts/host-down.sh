#!/bin/bash
# Run ON THE MAC from the nexus checkout: tear down what scripts/host-up.sh started.
#
#   scripts/host-down.sh [--dry-run] [--keep-scoreboard]
#
# Also stops the browser wall if host-up.sh --web-wall started it (scripts/wall-web.sh stop).
#
# 1. saves the final /api/state to .build/host/state-final.json (through the forward);
# 2. stops each lane's scripts/lane.sh with SIGTERM, so the harness sends lane_end;
# 3. stops the local `min` clients host-up left running (net forward, session run);
# 4. destroys the sessions listed in .build/host/sessions.txt (else the scoreboard and every
#    lane in scripts/lanes.txt) with `min session destroy -f`
#    (-f because there is no one to answer the prompt for uncommitted changes in a script;
#    reference/cli-min#session-destroy).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=$ROOT/.build/host
DRY=0 KEEP_SB=0
for a in "$@"; do
    case $a in
        --dry-run) DRY=1 ;;
        --keep-scoreboard) KEEP_SB=1 ;;
        *) echo "host-down: unknown argument $a" >&2; exit 2 ;;
    esac
done
run() { printf '+'; printf ' %q' "$@"; echo; [ "$DRY" = 1 ] || "$@"; }

if [ -f "$STATE/sessions.txt" ]; then
    SESSIONS=()   # no mapfile: macOS /bin/bash is 3.2
    while read -r s; do [ -n "$s" ] && SESSIONS+=("$s"); done < "$STATE/sessions.txt"
else
    # No record (state wiped, or host-up died early): fall back to every lane in the lanes file.
    SESSIONS=("${NEXUS_SB_SESSION:-scoreboard}")
    while read -r id _; do
        case $id in ''|'#'*) continue ;; esac
        SESSIONS+=("${NEXUS_LANE_PREFIX:-}$id")
    done < "${NEXUS_LANES_FILE:-$ROOT/scripts/lanes.txt}"
    echo "host-down: no $STATE/sessions.txt; trying ${SESSIONS[*]}"
fi
SB=${SESSIONS[0]}
PORT=$(cat "$STATE/port" 2>/dev/null || echo 8080)

if [ "$DRY" = 0 ]; then
    curl -sf -m 5 "http://localhost:$PORT/api/state" > "$STATE/state-final.json" \
        && echo "host-down: final state in $STATE/state-final.json" || true
fi

for s in "${SESSIONS[@]:1}"; do
    run min session exec "$s" 'p=$(cat $HOME/.nexus/lane.pid 2>/dev/null) && kill -TERM "$p" 2>/dev/null; true' || true
done
[ "$DRY" = 1 ] || sleep 3   # let lane_end reach the scoreboard

for pidf in "$STATE"/*.pid; do
    [ -e "$pidf" ] || continue
    case $KEEP_SB:$(basename "$pidf") in 1:forward.pid|"1:run-$SB.pid") continue ;; esac
    run kill "$(cat "$pidf")" || true
    [ "$DRY" = 1 ] || rm -f "$pidf"
done

for s in "${SESSIONS[@]:1}"; do run min session destroy -f "$s" || true; done
if [ "$KEEP_SB" = 1 ]; then
    echo "host-down: kept $SB and its forward on localhost:$PORT"
    printf '%s\n' "$SB" > "$STATE/sessions.txt"
else
    run min session destroy -f "$SB" || true
    [ "$DRY" = 1 ] || rm -f "$STATE/sessions.txt"
fi

# Browser wall (host-up.sh --web-wall, D-039): stops only what scripts/wall-web.sh started.
[ -d "$ROOT/.build/wall-web" ] && run "$ROOT/scripts/wall-web.sh" stop || true
