#!/usr/bin/env bash
# Stop the regtest chain. --wipe also deletes its state, for a fresh chain next time.
set -euo pipefail
source "$(dirname "$0")/env.sh"

for p in lwd zebrad; do
  if [ -f "$RUN/$p.pid" ]; then
    kill "$(cat "$RUN/$p.pid")" 2>/dev/null || true
    rm -f "$RUN/$p.pid"
  fi
done

if [ "${1:-}" = "--wipe" ]; then
  rm -rf "$RUN"
  echo "regtest stopped and wiped"
else
  echo "regtest stopped (state kept in $RUN)"
fi
