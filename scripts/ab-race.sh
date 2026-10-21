#!/bin/bash
# A/B race: bring up 12 live no-fault lanes from LANES with a one-page wall attached
# headlessly, measure every lane at 90 s (abort if no model answered), 5 and 10 min, append
# to RESULT, then tear everything down. Spends Workers AI credit for about 12 minutes.
#   scripts/ab-race.sh scripts/lanes-12-mixed.txt .build/ab-mixed.txt
cd "$(dirname "$0")/.."; L=$1 R=$2
scripts/host-up.sh --live --no-faults --one-page -n 12 --lanes "$L" > .build/host-up-ab.log 2>&1 || { echo "host-up failed" >> $R; tail -20 .build/host-up-ab.log >> $R; exit 1; }
(cd wall; tmux -L walltest kill-server 2>/dev/null; zellij delete-session wall-t --force >/dev/null 2>&1; tmux -L walltest new-session -d -x 240 -y 70 "ulimit -n 4096; cd $PWD && zellij --config config.kdl -s wall-t -n out/wall.kdl")
teardown() { zellij kill-session wall-t >/dev/null 2>&1; zellij delete-session wall-t --force >/dev/null 2>&1; tmux -L walltest kill-server 2>/dev/null; scripts/host-down.sh > .build/host-down.log 2>&1; echo "torn down: $(command min session list 2>&1 | grep -cE ' (l-0[0-9]+|scoreboard) ') sessions left" >> $R; }
measure() { echo "===== $1"; for n in $(seq -w 1 12); do l=l-0$n; command min session exec $l 'export TMUX_TMPDIR=$HOME/.nexus/tmux; python3 -c "
import json,time,datetime as d
s=json.load(open(\"/home/out/agent/stats.json\"));st=d.datetime.fromisoformat(s[\"started\"].replace(\"Z\",\"+00:00\")).timestamp()
el=time.time()-st;log=open(\"/home/out/agent/agent.log\").read()
print(\"%-11s %-8s el=%3ds act=%3d s/act=%5.1f tok_in=%7d stalls=%d 429s=%d\"%(s[\"game\"],s[\"model\"].split(\"/\")[-1][:8],el,s[\"actions\"],el/max(1,s[\"actions\"]),s[\"tokens_in\"],log.count(\"stalled\"),log.count(\"429\")))"; tmux capture-pane -p -t game | grep -E "Score|Moves|Depth|Level|XL|score" | tail -1 | cut -c1-100' 2>&1 | sed "s/^/$l /"; done; }
sleep 90; measure "90 s" > .build/ab-early.txt; if ! grep -q "tok_in= *[1-9]" .build/ab-early.txt; then { echo "ABORT: no lane got a model response in 90 s"; cat .build/ab-early.txt; } >> $R; teardown; exit 1; fi
sleep 210; measure "5 min" >> $R; sleep 300; measure "10 min" >> $R
teardown
