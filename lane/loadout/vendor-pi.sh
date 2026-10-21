#!/bin/sh
# Build vendor/pi.tgz (pi + deps, ~25 MB) so lanes need no npm registry access.
# Run on any machine with npm; the tree is pure JS/WASM plus per-platform prebuilds.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
ver=${NEXUS_PI_VERSION:-1.3.0}
tmp=$(mktemp -d)
npm install --prefix "$tmp" --no-fund --no-audit --loglevel=error "@earendil-works/pi-coding-agent@$ver"
mkdir -p "$here/vendor"
tar -C "$tmp" -czf "$here/vendor/pi.tgz" node_modules package.json
echo "wrote $here/vendor/pi.tgz ($(du -h "$here/vendor/pi.tgz" | cut -f1)); pi $ver"
