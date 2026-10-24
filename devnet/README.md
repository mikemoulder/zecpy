# devnet: a private Zcash chain for development and tests

A local regtest chain (zebrad + lightwalletd) bound to loopback. It gives unlimited test
funds, blocks on demand, and a light server we control, so tests can reproduce reorgs and
record exactly what the server sees.

Consensus mirrors Mainnet: every upgrade through NU6.3 (Ironwood) is active from block 1,
NU7 is not. `zebrad.toml.in` and `activation-heights.toml` must agree.

## Requirements

Binaries in `~/.zecpy-dev/bin` (override with `ZECPY_DEV`):

| Binary | Version | Source |
|---|---|---|
| `zebrad` | 7.0.0 | ZcashFoundation/zebra release tarball, SHA-256 verified |
| `lightwalletd` | 0.6.0 | zcash/lightwalletd tag `v0.6.0`, built with Go 1.27.3 |
| `zcash-devtool` | `0b3436b` | zcash/zcash-devtool, `--features regtest_support` |

## Use

```bash
devnet/up.sh <uregtest1... shielded address for block rewards>
devnet/mine.sh 101          # coinbase matures after 100 blocks; ~2.5 s per block
devnet/down.sh              # stop, keep state
devnet/down.sh --wipe       # stop and start over next time
```

Endpoints: zebrad JSON-RPC `http://127.0.0.1:18232`, lightwalletd gRPC
`http://127.0.0.1:19067` (plaintext, loopback only).

## Gotchas

- lightwalletd reads block height 0 as "no height given", so a wallet whose birthday is
  block 1 fails on `GetTreeState(0)`. Mine a few blocks first and use a birthday of 2 or
  more.
- A wallet created on this chain only works on this chain. `down.sh --wipe` invalidates it.
