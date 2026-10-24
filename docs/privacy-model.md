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
| Transparent address queries (`GetAddressUtxos`, `GetTaddressTxids`) on every sync | Sent by librustzcash's `sync::run` when transparent support is built in | Never sent: ZecPy's own sync loop has no transparent queries, and the leak audit asserts their absence on every test run |
| Exact birthday (`GetTreeState` at birthday - 1) | Sent at wallet creation | Birthday rounded down to a 10,000-block boundary |
| Where the last sync stopped (`GetTreeState` at every batch start) | Sent on every incremental sync by librustzcash `sync::run`; links sessions | Own sync loop: the tip's tree state comes from `GetLatestTreeState` and is saved for the next sync; adjacent ranges merge; batches split only at multiples of 1,000 |
| Memo and transaction-detail fetches (`GetTransaction` by txid) | Sent for every received transaction, alone, to read memos and fill in history | Off by default: invoices match by address and history is local. Opt-in `fetch_memos` sends each real txid only among decoys, over Tor (see below) |
| Pool crossings (Orchard notes paying an Ironwood receiver) | Built silently; the crossing amount is public in the bundles' value balances | `propose` reports `crossing_value`; sending refuses with `PrivacyError` unless `allow_pool_crossing=True`. ZIP 318 canonical migrations pass |
| Payments to transparent addresses | Allowed | Refused with `PrivacyError` unless `allow_transparent=True` |
| Network path linking sync and send | One connection, or one Tor circuit, for everything | Every sync session and every broadcast gets its own isolated Tor circuit; a failed circuit is retried on a fresh one, never directly |
| Broadcast timing | Published the moment it is built | Optional `max_broadcast_delay`: published after a random delay from the system CSPRNG |

## Memo fetching

Compact blocks carry only the first 52 bytes of each note ciphertext, so a memo needs the
full transaction, requested by txid. A server that sees one txid fetched soon after it was
mined learns that a light wallet received in it. Tor hides which wallet, not which
transaction. `fetch_memos` is therefore opt-in, and when used:

1. **Only what is needed.** librustzcash queues every wallet transaction for retrieval, and
   after decrypting one also queues the funding transactions of its transparent inputs.
   ZecPy fetches only transactions holding a received shielded note whose memo is unknown;
   fetching the others would tell the server about the sender's coins.
2. **Never alone.** Each real txid is fetched with `k` decoys (default 7): other
   non-coinbase transactions with shielded outputs, within 100 blocks, taken from compact
   blocks the wallet already downloaded. A coinbase is excluded because a server would rule
   it out as a payment at once.
3. **No rank tell.** Decoys are a window of consecutive candidates in height order, offset
   so the real transaction's rank inside the window is uniform. Choosing "the nearest k"
   would put the real one at the median. A unit test checks the rank distribution over
   5,000 draws.
4. **Wait rather than go out bare.** A payment without `k` candidates yet stays pending
   until later blocks supply them.
5. **Separate circuit, random time.** Each batch uses its own isolated Tor circuit, after a
   random delay of up to `max_delay` seconds (default 10), in shuffled order. Without Tor,
   to a remote server, the call raises `PrivacyError`.

What it does not hide: that some client fetched these `k + 1` transactions, and the rough
height they share. The anonymity set is the decoys plus every other client of that server.
`tests/test_devnet_memos.py` asserts through the audit proxy that sync sends no
`GetTransaction` at all, that a fetch sends exactly `k + 1` distinct txids including the
real one, and that no decoy is a coinbase.

## Pool crossings

Each shielded bundle's net value balance is public. When Orchard notes pay an Ironwood
receiver, the Orchard bundle shows value leaving and the Ironwood bundle shows it arriving:
the amount is on chain. `propose` computes the value entering each pool beyond what was
spent from it and reports it as `crossing_value`. Sending refuses it unless the caller
passes `allow_pool_crossing=True`, except for ZIP 318 canonical migration transfers (one
canonical denomination, fixed shape and fee), which are built to be indistinguishable from
each other.

## Cold signing

A hot server can run with only a viewing key: `Wallet.watch` imports the account from a
`ColdSigner`'s watch bundle, `propose_cold` builds and proves an unsigned transaction (PCZT),
and the offline signer signs it. The hot server never holds a key that can spend.

The signer decides which outputs are change by checking each recipient against its own
viewing key, not by trusting the address labels the hot side attached. A compromised hot
server cannot disguise a payment to itself as change.

The watch bundle contains the Unified Full Viewing Key, which reveals the account's full
history to whoever holds it. It cannot spend, but it is confidential.

## What the server still learns

- **That a wallet is syncing, and the chain tip it syncs to.** Every wallet does this.
- **The transaction, when the wallet sends one.** `SendTransaction` publishes it, which is
  unavoidable. The transaction itself is shielded: amounts, sender and recipient are not
  visible.
- **A Tor exit's address, not the wallet's.** Remote servers are reached over Tor by
  default (arti, via `zcash_client_backend`'s `tor` feature), and `.onion` servers are
  supported. With `tor=False` the server sees the caller's IP; that is an explicit opt-out.
  Loopback servers are always direct.
- **With `fetch_memos`: `k + 1` transactions near one height,** from a fresh Tor circuit.
  See Memo fetching above.
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
