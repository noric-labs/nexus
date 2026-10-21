# on_activate for nexus-lane. POSIX sh. Installs pi (vendored tarball if the loadout
# shipped one, else npm from the registry), renders models.json for the broker, and
# creates $NEXUS_OUT. Fails activation only if pi cannot be installed: a lane without
# its agent is useless.
set -u
say() { echo "nexus-lane: $*"; }

PREFIX=${NEXUS_PI_PREFIX:-$HOME/.local/share/nexus-pi}
VER=${NEXUS_PI_VERSION:-1.3.0}
TGZ=$HOME/.cache/nexus-lane/pi.tgz
PI=$PREFIX/node_modules/.bin/pi

mkdir -p "$PREFIX" "${NEXUS_OUT:-$HOME/out}/agent" "${PI_CODING_AGENT_DIR:-$HOME/.pi/nexus-agent}" \
    || say "could not create output dirs"

# The pi-next session package puts pi on PATH (D-032); lane.sh prefers it, so skip npm.
if command -v pi >/dev/null 2>&1; then
    PI=$(command -v pi)
    say "pi from the session packages: $PI"
elif [ -x "$PI" ]; then
    say "pi already installed: $("$PI" --version 2>/dev/null)"
elif [ -s "$TGZ" ]; then
    tar -xzf "$TGZ" -C "$PREFIX" && say "pi installed from vendored tarball"
else
    npm install --prefix "$PREFIX" --no-fund --no-audit --loglevel=error \
        "@earendil-works/pi-coding-agent@$VER" >/dev/null 2>&1 \
        && say "pi $VER installed from npm" \
        || say "npm install of pi failed (needs egress to registry.npmjs.org, or ship vendor/pi.tgz)"
fi
[ -x "$PI" ] || { say "pi missing at $PI"; exit 1; }

# models.json: the provider `nexus` at the broker route. agent.sh re-renders this at
# start too, so a changed NEXUS_INFERENCE_URL takes effect without reactivating.
LANE=${NEXUS_LANE_DIR:-/workbench/lane}
if [ -f "$LANE/models.json" ]; then
    python3 - "$LANE/models.json" "$PI_CODING_AGENT_DIR/models.json" <<'PY' || say "could not render models.json"
import json, os, sys
d = json.load(open(sys.argv[1]))
d["providers"]["nexus"]["baseUrl"] = os.environ.get("NEXUS_INFERENCE_URL", d["providers"]["nexus"]["baseUrl"]).rstrip("/")
json.dump(d, open(sys.argv[2], "w"), indent=1)
PY
else
    say "no $LANE/models.json (activate with the nexus checkout as the session path)"
fi

say "lane ${NEXUS_LANE_ID:-?} game=${NEXUS_GAME:-?} model=${NEXUS_MODEL:-?} out=${NEXUS_OUT:-?}"
exit 0
