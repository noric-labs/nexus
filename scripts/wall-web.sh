#!/bin/bash
# Run ON THE MAC: show the zellij wall in a browser through zellij's built-in web client
# (zellij >= 0.43; checked with 0.45.1). See D-039.
#
#   scripts/wall-web.sh start [--layout FILE] [--no-token]
#   scripts/wall-web.sh token      # one more read-only login token
#   scripts/wall-web.sh status
#   scripts/wall-web.sh stop       # undo what start did (only what it started)
#
# start:
#   1. creates the wall session ($NEXUS_WALL_SESSION, default nexus-wall) headless with
#      `zellij attach --create-background`, from --layout (default wall/out/wall.kdl), unless a
#      session of that name is already running (then it is reused as is);
#   2. starts `zellij web` daemonized on 127.0.0.1:8082 (zellij's default; `zellij web --stop`
#      takes no --port, so the port is fixed), unless it is already online;
#   3. creates a READ-ONLY login token and prints it once, with the URL.
# Steps 1 and 2 run under `ulimit -n 4096`: the wall has ~164 panes, each a pty, and the macOS
# default of 256 fds makes zellij panic.
#
# The session is shareable because wall/config.kdl sets `web_sharing "on"`; a wall started
# from a terminal with that config is shareable too while the web server runs.
# The token is a credential: it is printed to this terminal only and never written to disk.
# .build/wall-web/ (gitignored) records only what start created: the session flag, the server
# flag and the token NAMES, so stop can revoke them and leave everything else alone.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=$ROOT/.build/wall-web
CONFIG=$ROOT/wall/config.kdl
SESSION=${NEXUS_WALL_SESSION:-nexus-wall}
PORT=8082
URL=http://127.0.0.1:$PORT/$SESSION

die() { echo "wall-web: $*" >&2; exit 1; }
say() { echo "wall-web: $*"; }
alive() { zellij list-sessions -n -s 2>/dev/null | grep -qx "$SESSION"; }
# Not `zellij web --status`: in 0.45.1 it exits 0 when the server is offline.
online() { curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/"; }

new_token() {
    local out name tok
    out=$(zellij web --create-read-only-token) || die "creating a token failed"
    # Output: "token_N: <token> (read-only)". Keep the name, show the token once.
    name=$(sed -n 's/^\([^: ]*\): [^ ]* (read-only)$/\1/p' <<< "$out" | head -n1)
    tok=$(sed -n 's/^[^: ]*: \([^ ]*\) (read-only)$/\1/p' <<< "$out" | head -n1)
    [ -n "$tok" ] || { echo "$out"; die "could not parse the token output above"; }
    mkdir -p "$STATE"
    echo "$name" >> "$STATE/tokens"
    cat <<EOF

  Wall in the browser: $URL
  Read-only token ($name, shown once; paste it at the login prompt):

    $tok

  Revoke: zellij web --revoke-token $name   (scripts/wall-web.sh stop revokes it too)
EOF
}

cmd=${1:-}; [ $# -gt 0 ] && shift
case $cmd in
start)
    LAYOUT=$ROOT/wall/out/wall.kdl TOKEN=1
    while [ $# -gt 0 ]; do
        case $1 in
            --layout) LAYOUT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2"); shift ;;
            --no-token) TOKEN=0 ;;
            *) die "unknown argument $1" ;;
        esac
        shift
    done
    command -v zellij >/dev/null || die "zellij not found (brew install zellij)"
    mkdir -p "$STATE"
    ulimit -n 4096
    if alive; then
        say "session $SESSION is already running; reusing it (it must have been started with
wall/config.kdl, which sets web_sharing \"on\", or the browser cannot attach)"
    else
        [ -f "$LAYOUT" ] || die "no layout $LAYOUT (run wall-gen, or scripts/host-up.sh, first)"
        # A dead session of that name would be resurrected instead of using the layout.
        zellij delete-session "$SESSION" --force >/dev/null 2>&1 || true
        (cd "$ROOT/wall" && env -u ZELLIJ zellij --config "$CONFIG" attach --create-background "$SESSION" \
            options --default-layout "$LAYOUT")
        alive || die "session $SESSION did not start"
        touch "$STATE/session"
        say "started session $SESSION (headless) from $LAYOUT"
    fi
    if online; then
        say "zellij web is already online on 127.0.0.1:$PORT; reusing it"
    else
        zellij --config "$CONFIG" web --start --daemonize --ip 127.0.0.1 --port "$PORT" >/dev/null
        online || die "zellij web did not come up on 127.0.0.1:$PORT"
        touch "$STATE/server"
        say "started zellij web on 127.0.0.1:$PORT"
    fi
    if [ "$TOKEN" = 1 ]; then new_token; else say "open $URL"; fi
    ;;
token)
    online || die "zellij web is offline; run: scripts/wall-web.sh start"
    new_token
    ;;
status)
    zellij web --status || true
    if alive; then say "session $SESSION: running"; else say "session $SESSION: not running"; fi
    zellij web --list-tokens 2>/dev/null || true
    ;;
stop)
    if [ -f "$STATE/tokens" ]; then
        while read -r name; do
            [ -n "$name" ] && zellij web --revoke-token "$name" >/dev/null 2>&1 && say "revoked token $name"
        done < "$STATE/tokens"
        rm -f "$STATE/tokens"
    fi
    if [ -f "$STATE/server" ]; then
        zellij web --stop >/dev/null 2>&1 || true
        rm -f "$STATE/server"
        say "stopped zellij web on 127.0.0.1:$PORT"
    fi
    if [ -f "$STATE/session" ]; then
        zellij kill-session "$SESSION" >/dev/null 2>&1 || true
        zellij delete-session "$SESSION" --force >/dev/null 2>&1 || true
        rm -f "$STATE/session"
        say "stopped session $SESSION"
    fi
    ;;
*)
    sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'
    [ "$cmd" = -h ] || [ "$cmd" = --help ] || [ -z "$cmd" ] || exit 2
    ;;
esac
