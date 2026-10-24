# Shared paths and ports for the local regtest chain. Sourced by the other scripts.
# Binaries live outside the repo (see memory.md, Dev tooling); override ZECPY_DEV to move them.

DEVNET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZECPY_DEV="${ZECPY_DEV:-$HOME/.zecpy-dev}"
BIN="$ZECPY_DEV/bin"
RUN="${ZECPY_DEVNET_RUN:-$ZECPY_DEV/regtest}"

# Loopback only: nothing on this chain is reachable from outside the machine.
ZEBRA_RPC="127.0.0.1:18232"
ZEBRA_P2P="127.0.0.1:18344"
LWD_GRPC="127.0.0.1:19067"
LWD_HTTP="127.0.0.1:19068"

rpc() { # rpc <method> [json params]
  curl -sf --max-time 30 -H 'content-type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":${2:-[]}}" "http://$ZEBRA_RPC"
}
