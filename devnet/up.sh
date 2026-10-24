#!/usr/bin/env bash
# Start a private Zcash regtest chain (zebrad + lightwalletd) on loopback.
#
#   devnet/up.sh <regtest address for block rewards>
#
# State persists in $RUN between runs; devnet/down.sh --wipe starts over.
set -euo pipefail
source "$(dirname "$0")/env.sh"

miner="${1:-${ZECPY_DEVNET_MINER:-}}"
if [ -z "$miner" ]; then
  echo "usage: devnet/up.sh <regtest address for block rewards>" >&2
  exit 1
fi
for b in zebrad lightwalletd; do
  [ -x "$BIN/$b" ] || { echo "missing $BIN/$b (see memory.md, Dev tooling)" >&2; exit 1; }
done
if rpc getblockcount >/dev/null 2>&1; then
  echo "zebrad already running on $ZEBRA_RPC" >&2
  exit 1
fi

mkdir -p "$RUN/zebra" "$RUN/lwd"
sed -e "s|@MINER@|$miner|" -e "s|@STATE@|$RUN/zebra|" \
    -e "s|@RPC@|$ZEBRA_RPC|" -e "s|@P2P@|$ZEBRA_P2P|" \
    "$DEVNET_DIR/zebrad.toml.in" > "$RUN/zebrad.toml"

"$BIN/zebrad" -c "$RUN/zebrad.toml" start >"$RUN/zebrad.log" 2>&1 &
echo $! >"$RUN/zebrad.pid"

for _ in $(seq 1 60); do
  rpc getblockcount >/dev/null 2>&1 && break
  sleep 1
done
rpc getblockcount >/dev/null 2>&1 || { echo "zebrad did not start, see $RUN/zebrad.log" >&2; exit 1; }

# lightwalletd needs at least one block past genesis to serve.
height=$(rpc getblockcount | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"])')
[ "$height" -ge 1 ] || rpc generate '[1]' >/dev/null

# zebrad has cookie auth off on loopback, but lightwalletd insists on credentials.
"$BIN/lightwalletd" --rpchost 127.0.0.1 --rpcport "${ZEBRA_RPC##*:}" \
  --rpcuser devnet --rpcpassword devnet \
  --grpc-bind-addr "$LWD_GRPC" --http-bind-addr "$LWD_HTTP" \
  --no-tls-very-insecure --data-dir "$RUN/lwd" --log-file "$RUN/lwd.log" \
  >>"$RUN/lwd.log" 2>&1 &
echo $! >"$RUN/lwd.pid"

for _ in $(seq 1 30); do
  (exec 3<>"/dev/tcp/${LWD_GRPC%:*}/${LWD_GRPC##*:}") 2>/dev/null && break
  sleep 1
done

echo "regtest up: zebrad rpc http://$ZEBRA_RPC, lightwalletd grpc http://$LWD_GRPC"
echo "height $(rpc getblockcount | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"])')"
