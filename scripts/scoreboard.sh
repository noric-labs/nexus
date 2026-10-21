#!/bin/bash
# Build and run the scoreboard. Entry point of the `scoreboard` task (minimal.toml) and of
# scripts/host-up.sh, which starts it in the scoreboard session.
#
#   scripts/scoreboard.sh [--heat 15m] [--pit 1m] [--listen 0.0.0.0:8080] [--db PATH]
#
# The binary goes to $HOME/.nexus/bin (CGO_ENABLED=0; modernc sqlite is pure Go). The
# first build downloads modules, so the box needs egress to proxy.golang.org. If
# scoreboard/scoreboard.toml exists it is passed as -config; the flags override it.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${HOME:=/home}"
heat=${NEXUS_HEAT:-15m} pit=${NEXUS_PIT:-1m}
listen=${NEXUS_SB_LISTEN:-0.0.0.0:8080} db=${NEXUS_SB_DB:-$HOME/.nexus/scoreboard.db}
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || { echo "scoreboard.sh: $1 needs a value" >&2; exit 2; }
    case $1 in
        --heat) heat=$2 ;; --pit) pit=$2 ;; --listen) listen=$2 ;; --db) db=$2 ;;
        *) echo "scoreboard.sh: unknown flag $1" >&2; exit 2 ;;
    esac
    shift 2
done

mkdir -p "$HOME/.nexus/bin" "$(dirname "$db")"
bin=$HOME/.nexus/bin/scoreboard
(cd "$ROOT/scoreboard" && CGO_ENABLED=0 go build -o "$bin" .)

args=(-listen "$listen" -db "$db" -heat "$heat" -pit "$pit")
[ -f "$ROOT/scoreboard/scoreboard.toml" ] && args+=(-config "$ROOT/scoreboard/scoreboard.toml")
echo "scoreboard.sh: $bin ${args[*]}"
exec "$bin" "${args[@]}"
