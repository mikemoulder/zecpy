# Privacy model

What a light server (lightwalletd or Zaino) can learn from a ZecPy wallet, how that is
measured, and what is still visible.

## Threat model

The adversary runs the light server the wallet talks to, or can watch traffic to it. It
sees every request and response and the network address they come from. It cannot break
the cryptography, and it does not see the wallet's database or keys.

Out of scope: an attacker with access to the machine running ZecPy, and a global network
observer.

## How it is measured

`tools/leak_audit` is a gRPC proxy that sits between a wallet and the server. It forwards
bytes unchanged, decodes a copy of every request, and labels it:

| Label | Meaning |
|---|---|
| public | Every wallet sends the same request; it says nothing about this one |
| coarse | Reveals something, but only within a deliberately wide window |
| broadcast | Publishes a transaction |
| identifying | Ties the request to this wallet's addresses, transactions or exact history |

Unknown methods are labelled identifying, so the audit fails closed. The proxy runs
outside the wallet, so its record does not depend on the wallet reporting on itself.

The audit inspects request *content*. Tor cannot reach a loopback proxy, so the audit runs
without Tor; the network path is covered separately: `tests/test_network_tor.py` creates and
syncs a public-testnet wallet through Tor, and circuit separation relies on arti's
`isolated_client`, which guarantees that two handles never share a circuit.

`tests/test_devnet_leak_audit.py` runs a merchant's full cycle through the proxy on the
local regtest chain and asserts on the record:

- create a wallet, issue an invoice, receive the payment, confirm it
- send a refund
- restart the process and sync again

Result: **no identifying request.** The record holds GetLatestTreeState, GetSubtreeRoots,
GetBlockRange (public), tree states at aligned heights (coarse) and one SendTransaction.

The same test runs zcash-devtool, the reference wallet built on the same librustzcash
crates, through the same proxy. The audit flags its exact-birthday tree-state request and
its transparent-address queries. That control shows the audit can see a leak when there is
one.

## What ZecPy does differently

| Leak | In a typical light wallet | In ZecPy |
|---|---|---|
| Transparent address queries (`GetAddressUtxos`, `GetTaddressTxids`) on every sync | Sent, unless the wallet is built without transparent support | The wallet-level `transparent-inputs` features are not compiled in (see `cargo tree -e features -i zcash_client_backend`) |
| Exact birthday (`GetTreeState` at birthday - 1) | Sent at wallet creation | Birthday rounded down to a 10,000-block boundary |
| Where the last sync stopped (`GetTreeState` at every batch start) | Sent on every incremental sync by librustzcash `sync::run`; links sessions | Own sync loop: the tip's tree state comes from `GetLatestTreeState` and is saved for the next sync; adjacent ranges merge; batches split only at multiples of 1,000 |
| Memo and transaction-detail fetches (`GetTransaction` by txid) | Sent to read memos and fill in history | Never sent. Invoices match payments by receiving address, so memos are not needed |
| Payments to transparent addresses | Allowed | Refused with `PrivacyError` unless `allow_transparent=True` |
| Network path linking sync and send | One connection, or one Tor circuit, for everything | Every sync session and every broadcast gets its own isolated Tor circuit; a failed circuit is retried on a fresh one, never directly |
| Broadcast timing | Published the moment it is built | Optional `max_broadcast_delay`: published after a random delay from the system CSPRNG |

## What the server still learns

- **That a wallet is syncing, and the chain tip it syncs to.** Every wallet does this.
- **The transaction, when the wallet sends one.** `SendTransaction` publishes it, which is
  unavoidable. The transaction itself is shielded: amounts, sender and recipient are not
  visible.
- **A Tor exit's address, not the wallet's.** Remote servers are reached over Tor by
  default (arti, via `zcash_client_backend`'s `tor` feature), and `.onion` servers are
  supported. With `tor=False` the server sees the caller's IP; that is an explicit opt-out.
  Loopback servers are always direct.
- **One exact-height tree-state request after a reorg.** When the chain reorganises, the
  wallet rewinds and must fetch the tree state at the rewind height. Reorgs are rare and
  the audit reports this request as identifying rather than hiding it.

## Known limitations

- **A received payment orphaned by a reorg stays pending until it is mined again.**
  librustzcash asks the caller to look up such a transaction's status with `GetTransaction`,
  which names the txid. ZecPy does not send that request, so the value shows as pending
  (never spendable, never counted as paid) instead of being cleared at expiry.
- **The invoice address range is a ZecPy convention.** Invoices use diversifier indices from
  2^80 up. Another wallet restored from the same seed finds the payments (all diversifiers of
  a viewing key decrypt), but does not know which invoice each belongs to.
