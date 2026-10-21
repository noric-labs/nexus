#!/bin/sh
# Toy "game" for the local wall demo: redraws a small status screen every second.
lane=$1 game=$2 fault=$3
i=0
while :; do
  i=$((i + 1))
  printf '\033[H\033[2J'
  printf '%s  %s  fault=%s\n' "$lane" "$game" "$fault"
  printf 'turn %d   hp %d/20   depth %d\n\n' "$i" $((20 - i % 20)) $((i / 30 + 1))
  j=0
  while [ $j -lt 8 ]; do
    printf '  %s\n' "$(printf '#.@..#...%*s' $(((i + j) % 40)) 'g')"
    j=$((j + 1))
  done
  sleep 1
done
