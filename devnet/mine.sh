#!/usr/bin/env bash
# Mine N blocks on the regtest chain (default 1). Coinbase matures after 100 blocks.
# zebrad takes about 2.5 s per block on regtest, so blocks are requested one at a time:
# a single large `generate` call outlives any sane HTTP timeout.
set -euo pipefail
source "$(dirname "$0")/env.sh"

n="${1:-1}"
for i in $(seq 1 "$n"); do
  rpc generate '[1]' >/dev/null || { echo "generate failed; is devnet/up.sh running?" >&2; exit 1; }
  if [ $((i % 25)) -eq 0 ] && [ "$i" -lt "$n" ]; then echo "  mined $i/$n"; fi
done
rpc getblockcount | python3 -c 'import sys,json; print("height", json.load(sys.stdin)["result"])'
