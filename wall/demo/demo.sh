#!/bin/sh
# Local wall demo: one tmux server per fake lane (socket "lane-N", session "game",
# like a real lane), a lanes file, and layouts generated with a local attach template.
#   demo.sh up [N]   start N fake lanes (default 70: 64 at 80x24 + 6 brogue at 100x34)
#   demo.sh down     kill them
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${WALL_DEMO_OUT:-$here/out}
n=${2:-70}
games="greed frotz-zork1 dcss"
faults="none malloc-1pct io-save-5pct winch-storm time-warp keystorm"
case ${1:-up} in
up)
  mkdir -p "$out"
  printf '{"lanes":[\n' > "$out/lanes.json"
  i=1
  while [ $i -le "$n" ]; do
    id=$(printf 'l-%03d' $i)
    if [ $i -gt $((n - 6)) ]; then game=brogue; w=100; h=34
    else game=$(echo $games | cut -d' ' -f$((i % 3 + 1))); w=80; h=24; fi
    fault=$(echo $faults | cut -d' ' -f$((i % 6 + 1)))
    tmux -L "lane-$i" kill-server 2>/dev/null || true
    tmux -L "lane-$i" -f /dev/null new-session -d -s game -x $w -y $h "$here/toy.sh $id $game $fault"
    tmux -L "lane-$i" set -g window-size manual >/dev/null
    lf=""
    [ $((i % 5)) -eq 0 ] && lf=$(printf ',"last_failure":{"type":"crash","ts":"2026-10-21T12:%02d:00Z"}' $((i % 60)))
    sep=","; [ $i -eq "$n" ] && sep=""
    printf '{"lane_id":"%s","game":"%s","fault":"%s"%s}%s\n' "$id" "$game" "$fault" "$lf" "$sep" >> "$out/lanes.json"
    i=$((i + 1))
  done
  printf ']}\n' >> "$out/lanes.json"
  cd "$here/.." && uv run wall-gen --lanes-file "$out/lanes.json" --out "$out" \
    --cmd 'tmux -L {session} attach -r -t game'
  ;;
down)
  i=1
  while [ $i -le "$n" ]; do tmux -L "lane-$i" kill-server 2>/dev/null || true; i=$((i + 1)); done
  ;;
esac
