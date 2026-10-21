#!/bin/bash
# Run ON THE MAC (or any Minimal host) from the nexus checkout: bring up the Torment Nexus as
# real Minimal sessions, one per lane plus one scoreboard session, forward the scoreboard to
# localhost, and generate the zellij wall. scripts/host-down.sh undoes it.
#
#   scripts/host-up.sh [--dry-run] [-n N] [--lanes FILE] [--live] [--heat 15m] [--pit 1m]
#                      [--port 8080] [--network own_ip|host_ip] [--via exec|run]
#
#   --dry-run   print every command (min, uv, ...) and run none of them
#   -n N        start the first N lanes of the lanes file (default 3)
#   --lanes F   lanes file, "id game fault seed [model]" per line (default scripts/lanes.txt)
#   --live      pi talks to Workers AI through the `broker` loadout. Without it every lane runs
#               lane/mock_openai.py in its own box: no inference egress, no spend.
#   --port P    laptop port for the scoreboard forward (default 8080)
#   --network   own_ip (default): every box has its own address, lanes may reach only the
#               scoreboard (+ the broker with --live). host_ip: boxes share the host network,
#               no isolation, egress flags are not passed (a host_ip box does not enforce them).
#   --via       exec (default): start scripts/scoreboard.sh and scripts/lane.sh in the session's
#               own box with `min session exec <s> 'python3 scripts/detach.py ...'`.
#               run: `min session run <s> <task>`, kept in the background on this machine.
#               Needs --network host_ip (see "Why --via exec" below).
#
# Session names: the scoreboard is $NEXUS_SB_SESSION (default `scoreboard`), each lane is
# $NEXUS_LANE_PREFIX<id> (default prefix empty, so `l-001`). Local state, logs and the pids
# of background `min` clients go to .build/host/.
#
# Command forms follow https://minimal.dev/docs/reference/cli-min (checked 2026-10-21):
#   min session activate [OPTIONS] [PATH]      --name --network --ingress --allow-* --loadout
#                                              --no-loadouts --no-prompt
#   min session exec <SESSION> <COMMAND>...    one argument = a shell command in the session
#   min session run <SESSION> <TASK>           no task args, no client env forwarded
#   min net forward <SESSION> <LOCAL>:<PORT>   foreground; dials 127.0.0.1:<PORT> in the box
#   min session list --json, min loadout list, min session destroy -f <SESSION>
#
# Why --via exec: per reference/tasks#networking, a task in an own_ip session runs in "a second
# box beside the session's box", and on a VM-backed host (every Mac) it "has no outbound reach
# yet". A scoreboard started as a task would therefore not be behind the session's --ingress or
# `min net forward`, and lane tasks could not reach it. `session exec` runs in the session's
# own box, under the session's network policy.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=$ROOT/.build/host
DRY=0 N=3 LANES_FILE=$ROOT/scripts/lanes.txt LIVE=0 HEAT=15m PIT=1m PORT=8080
NET=own_ip VIA=exec
SB=${NEXUS_SB_SESSION:-scoreboard}
PREFIX=${NEXUS_LANE_PREFIX:-}
MODEL=${NEXUS_MODEL:-@cf/qwen/qwen3-30b-a3b-fp8}
BROKER_SUBNET=100.64.255.254/32
# Hosts the scoreboard box may reach for `go build` (module proxy + checksum DB). Add more
# with NEXUS_SB_ALLOW_HOSTS="a b" if the build log shows another host.
SB_HOSTS="proxy.golang.org sum.golang.org ${NEXUS_SB_ALLOW_HOSTS:-}"

while [ $# -gt 0 ]; do
    case $1 in
        --dry-run) DRY=1 ;;
        -n) N=$2; shift ;;
        --lanes) LANES_FILE=$2; shift ;;
        --live) LIVE=1 ;;
        --heat) HEAT=$2; shift ;;
        --pit) PIT=$2; shift ;;
        --port) PORT=$2; shift ;;
        --network) NET=$2; shift ;;
        --via) VIA=$2; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "host-up: unknown argument $1" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "host-up: $*" >&2; exit 1; }
say() { echo "host-up: $*"; }
# Print a command, then run it (unless --dry-run).
run() { printf '+'; printf ' %q' "$@"; echo; [ "$DRY" = 1 ] || "$@"; }
# min session exec <session> '<shell command>': one argument, run by the session's shell.
# Printed as the user would type it: the remote strings below contain no single quotes.
rexec() { printf "+ min session exec %q '%s'\n" "$1" "$2"; [ "$DRY" = 1 ] || min session exec "$1" "$2"; }
# Quiet probe for wait loops: never runs under --dry-run.
probe() { [ "$DRY" = 1 ] && return 0; min session exec "$1" "$2" >/dev/null 2>&1; }
# A `min` client kept running on this machine (forward, session run): nohup, log + pid file.
bg() {
    local name=$1; shift
    printf '+ nohup'; printf ' %q' "$@"; printf ' > %q 2>&1 &\n' "$STATE/$name.log"
    [ "$DRY" = 1 ] && return 0
    nohup "$@" > "$STATE/$name.log" 2>&1 &
    echo $! > "$STATE/$name.pid"
}
wait_for() {  # wait_for <seconds> <description> <command...>
    local secs=$1 what=$2; shift 2
    [ "$DRY" = 1 ] && { say "(dry run) would wait up to ${secs}s for $what"; return 0; }
    local end=$((SECONDS + secs))
    until "$@"; do
        [ $SECONDS -lt $end ] || return 1
        sleep 2
    done
}

case $NET in own_ip|host_ip) ;; *) die "--network must be own_ip or host_ip" ;; esac
case $VIA in exec|run) ;; *) die "--via must be exec or run" ;; esac
[[ $N =~ ^[0-9]+$ ]] && [ "$N" -ge 1 ] || die "-n needs a positive number"
if [ "$VIA" = run ] && [ "$NET" = own_ip ]; then
    die "--via run needs --network host_ip: a task in an own_ip session runs in a second box that
the session's ingress and 'min net forward' do not reach, and on a Mac it has no outbound reach
(https://minimal.dev/docs/reference/tasks#networking)."
fi
if [ "$VIA" = run ] && { [ "$HEAT" != 15m ] || [ "$PIT" != 1m ]; }; then
    die "--heat/--pit cannot be passed with --via run: 'min session run <SESSION> <TASK>' takes no
task args, so the scoreboard task uses its defaults (15m/1m). Use --via exec."
fi

# ---- preflight --------------------------------------------------------------------------
if [ "$DRY" = 0 ]; then
    command -v min >/dev/null || die "min not found (https://minimal.dev/docs/start/1-getting-started)"
    command -v uv >/dev/null || die "uv not found (needed for wall-gen): brew install uv"
    command -v curl >/dev/null || die "curl not found"
    command -v zellij >/dev/null || say "warning: zellij not found (brew install zellij); needed only to show the wall"
fi
[ "$(uname -s)" = Darwin ] || say "note: not macOS; the flow is the same on a Linux host"

valid() { [[ $1 =~ ^[A-Za-z0-9@/._:+-]+$ ]]; }
LANES=()
while read -r id game fault seed model _; do
    case $id in ''|'#'*) continue ;; esac
    [ -n "${seed:-}" ] || die "$LANES_FILE: need 'id game fault seed [model]', got: $id $game $fault"
    for v in "$id" "$game" "$fault" "$seed" "${model:-$MODEL}"; do
        valid "$v" || die "$LANES_FILE: unsafe value '$v'"
    done
    [[ $id =~ ^[a-z0-9-]+$ ]] || die "$LANES_FILE: lane id '$id' must be [a-z0-9-] (it names a session)"
    LANES+=("$id $game $fault $seed ${model:-$MODEL}")
done < "$LANES_FILE"
[ "${#LANES[@]}" -ge "$N" ] || die "$LANES_FILE has ${#LANES[@]} lanes, asked for $N"
LANES=("${LANES[@]:0:$N}")

SESSIONS=("$SB")
for spec in "${LANES[@]}"; do read -r id _ <<< "$spec"; SESSIONS+=("$PREFIX$id"); done

if [ "$DRY" = 0 ]; then
    mkdir -p "$STATE"
    existing=$(min session list --json) || die "'min session list --json' failed; is the daemon up? (min version)"
    for s in "${SESSIONS[@]}"; do
        # The JSON layout is not documented; match the name as a JSON string value.
        if grep -Eq "\"name\"[[:space:]]*:[[:space:]]*\"$s\"" <<< "$existing"; then
            die "a session named '$s' already exists; run scripts/host-down.sh (or min session destroy -f $s)"
        fi
    done
    curl -s -o /dev/null -m 2 "http://localhost:$PORT/" \
        && die "localhost:$PORT is already in use (an old forward?); run scripts/host-down.sh or pass --port"
    printf '%s\n' "${SESSIONS[@]}" > "$STATE/sessions.txt"
    echo "$PORT" > "$STATE/port"
fi

# The nexus-lane loadout: copied, not symlinked (Minimal rejects hook scripts reached through
# symlinks). Its vendor/pi.tgz patch lands in each lane's $HOME/.cache/nexus-lane/pi.tgz, which
# scripts/lane.sh installs; without it lane.sh npm-installs pi, so allow the registry.
LOADOUTS=${XDG_CONFIG_HOME:-$HOME/.config}/minimal/loadouts
run mkdir -p "$LOADOUTS"
run rm -rf "$LOADOUTS/nexus-lane"
run cp -R "$ROOT/lane/loadout" "$LOADOUTS/nexus-lane"
NPM=0
# pi comes from the pi-next sideload package in [session].packages; npm is only a fallback.
if ! grep -q '"pi-next"' "$ROOT/minimal.toml" && [ ! -s "$ROOT/lane/loadout/vendor/pi.tgz" ]; then
    NPM=1
    say "pi-next not in minimal.toml [session].packages and no vendor/pi.tgz: lanes will npm-install pi"
fi
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then
    min loadout list | grep -qw broker || die "--live needs the 'broker' loadout (min loadout list)"
fi

# ---- scoreboard session -----------------------------------------------------------------
sb_flags=(--name "$SB" --no-loadouts --no-prompt --network "$NET")
if [ "$NET" = own_ip ]; then
    # Static ingress so lane boxes can connect to :8080; egress only for `go build`.
    sb_flags+=(--ingress 8080:8080)
    for h in $SB_HOSTS; do sb_flags+=(--allow-dns-hosts "$h"); done
fi
printf '+ min session activate'; printf ' %q' "${sb_flags[@]}" "$ROOT"; echo
if [ "$DRY" = 0 ]; then
    min session activate "${sb_flags[@]}" "$ROOT" 2>&1 | tee "$STATE/activate-$SB.log"
    [ "${PIPESTATUS[0]}" = 0 ] || die "activating $SB failed (log: $STATE/activate-$SB.log)"
fi

# How lanes address the scoreboard. own_ip: by box name. Activate prints
# "names: <box>.min.internal ..." (cli-min#finalize-install); use that rather than assuming the
# box is named after the session. host_ip: every box shares the host's network namespace.
if [ "$NET" = own_ip ]; then
    SB_HOST=$SB.min.internal
    if [ "$DRY" = 0 ]; then
        h=$(sed -n 's/.*names: \([A-Za-z0-9._-]*\.min\.internal\).*/\1/p' "$STATE/activate-$SB.log" | head -n1)
        if [ -n "$h" ]; then SB_HOST=$h
        else say "warning: activate printed no 'names: <box>.min.internal' line; assuming $SB_HOST"; fi
    fi
    SB_URL=http://$SB_HOST:8080
else
    SB_HOST=127.0.0.1 SB_URL=http://127.0.0.1:8080
fi
say "lanes will report to $SB_URL"

if [ "$VIA" = exec ]; then
    # Prep first, then background ONLY the job. A trailing & after an && chain backgrounds
    # the whole chain in a subshell that keeps exec's stdout open, so exec never returns.
    # detach.py double-forks and closes every inherited fd: setsid+nohup still leaks fd 3+
    # from the exec relay, which keeps exec waiting (D-032).
    rexec "$SB" "cd /workbench && python3 scripts/detach.py --log \$HOME/.nexus/scoreboard.log -- bash scripts/scoreboard.sh --heat $HEAT --pit $PIT"
else
    bg "run-$SB" min session run "$SB" scoreboard
fi
# The first start builds Go modules from proxy.golang.org; give it a few minutes.
if ! wait_for 300 "the scoreboard on :8080 in $SB" probe "$SB" 'curl -sf -m 3 http://127.0.0.1:8080/api/state'; then
    min session exec "$SB" 'tail -n 30 $HOME/.nexus/scoreboard.log' >&2 || cat "$STATE/run-$SB.log" >&2 || true
    die "scoreboard did not answer on 127.0.0.1:8080 inside $SB within 300s"
fi

# ---- forward to the laptop --------------------------------------------------------------
bg forward min net forward "$SB" "$PORT:8080"
if ! wait_for 30 "http://localhost:$PORT" curl -sf -m 3 -o /dev/null "http://localhost:$PORT/api/state"; then
    cat "$STATE/forward.log" >&2 || true
    die "the forward to localhost:$PORT does not answer. The docs note that the own_ip leg of
'min net forward' is not yet proven end to end (cli-min#net-forward); try --network host_ip."
fi
say "race UI: http://localhost:$PORT"

# ---- lane sessions ----------------------------------------------------------------------
lane_flags=(--no-prompt --network "$NET" --loadout nexus-lane)
[ "$LIVE" = 1 ] && lane_flags+=(--loadout broker)
if [ "$NET" = own_ip ]; then
    lane_flags+=(--allow-dns-hosts "$SB_HOST")
    [ "$LIVE" = 1 ] && lane_flags+=(--allow-subnets "$BROKER_SUBNET")
    [ "$NPM" = 1 ] && lane_flags+=(--allow-dns-hosts registry.npmjs.org)
fi
MOCK=$((1 - LIVE))
first=1
for spec in "${LANES[@]}"; do
    read -r id game fault seed model <<< "$spec"
    s=$PREFIX$id
    printf '+ min session activate --name %q' "$s"; printf ' %q' "${lane_flags[@]}" "$ROOT"; echo
    if [ "$DRY" = 0 ]; then
        min session activate --name "$s" "${lane_flags[@]}" "$ROOT" > "$STATE/activate-$s.log" 2>&1 \
            || { cat "$STATE/activate-$s.log" >&2; die "activating $s failed"; }
    fi
    # Lane config for scripts/lane.sh. Written into the box because `min session run` forwards
    # no client env, and so a restart inside the session needs no flags.
    rexec "$s" "mkdir -p \$HOME/.nexus && printf %s\\\\n NEXUS_LANE_ID=$id NEXUS_GAME=$game NEXUS_FAULT=$fault NEXUS_SEED=$seed NEXUS_MODEL=$model NEXUS_SCOREBOARD_URL=$SB_URL NEXUS_MOCK=$MOCK > \$HOME/.nexus/lane.env"
    if [ "$first" = 1 ] && [ "$VIA" = exec ]; then
        # The session's packages come from [session] in minimal.toml, sideload games included.
        # Sideload packages are verified in task `packages`, not yet in [session]: check once.
        if [ "$DRY" = 0 ] && ! min session exec "$s" 'for t in tmux python3 node curl dfrotz greed brogue-ce fiu-run; do command -v $t >/dev/null || { echo "missing: $t"; exit 1; }; done'; then
            die "$s lacks lane tools. If only the games are missing, the sideload packages did not
compose into the session; use --via run --network host_ip (task packages do carry them)."
        fi
        first=0
    fi
    if ! wait_for 20 "$s to reach $SB_URL" probe "$s" "curl -sf -m 3 -o /dev/null $SB_URL/api/state"; then
        min session policy "$s" >&2 || true
        die "$s cannot reach $SB_URL (egress: --allow-dns-hosts $SB_HOST; see the policy above)"
    fi
    if [ "$VIA" = exec ]; then
        rexec "$s" "cd /workbench && python3 scripts/detach.py --log \$HOME/.nexus/lane.log -- bash scripts/lane.sh"
    else
        bg "run-$s" min session run "$s" lane
    fi
done

# ---- wait for lane_start ----------------------------------------------------------------
for spec in "${LANES[@]}"; do
    read -r id _ <<< "$spec"
    wait_for 120 "$id on the scoreboard" sh -c "curl -sf -m 3 http://localhost:$PORT/api/state | grep -q '\"$id\"'" \
        || say "warning: $id has not reported yet; check: min session exec $PREFIX$id 'tail -n 40 \$HOME/.nexus/lane.log \$HOME/out/harness.log'"
done

# ---- wall -------------------------------------------------------------------------------
[ "$DRY" = 1 ] || {
    echo "["
    sep=""
    for spec in "${LANES[@]}"; do
        read -r id game fault _ <<< "$spec"
        printf '%s {"lane_id": "%s", "game": "%s", "fault": "%s"}' "$sep" "$id" "$game" "$fault"
        sep=$',\n'
    done
    printf '\n]\n'
} > "$STATE/lanes.json"
# `min session exec` gives the command no terminal, so `tmux attach` cannot run there directly;
# lane/ttyview.py makes a pty in the box and streams the read-only attach (see wall/README.md).
VIEW_CMD="min session exec {session} 'exec python3 /workbench/lane/ttyview.py'"
run uv run --project "$ROOT/wall" wall-gen --lanes-file "$STATE/lanes.json" --out "$ROOT/wall/out" \
    --session-fmt "$PREFIX{lane_id}" --cmd "$VIEW_CMD"

cat <<EOF

Up: scoreboard session '$SB' + ${#LANES[@]} lane session(s) ($NET, via $VIA, $([ "$LIVE" = 1 ] && echo "live via broker" || echo "mock model")).
  race UI      http://localhost:$PORT   (forward pid in $STATE/forward.pid)
  one lane     ${VIEW_CMD//\{session\}/${SESSIONS[1]}}
  lane logs    min session exec ${SESSIONS[1]} 'tail -n 40 \$HOME/.nexus/lane.log \$HOME/out/harness.log \$HOME/out/agent/agent.log'

Wall (wall/terminals/ has Ghostty, kitty and WezTerm configs for the 4pt grid):
  # in the fullscreen wall terminal
  cd $ROOT/wall && zellij delete-session nexus-wall 2>/dev/null; zellij --config config.kdl -s nexus-wall -n out/wall.kdl
  # in any other terminal
  cd $ROOT/wall && uv run wall-pager --session nexus-wall --scoreboard http://localhost:$PORT --out out

Tear down: scripts/host-down.sh
EOF
