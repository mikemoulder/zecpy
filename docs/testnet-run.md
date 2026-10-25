# A recorded run on public testnet

On 2026-10-25 ZecPy ran a full merchant story on Zcash testnet against
`testnet.zec.rocks`, with real proofs, real blocks and real third-party transactions as
memo decoys. Every request the shop sent to the light server was recorded by the leak-audit
proxy, and that proxy reached the server only over Tor. Script:
[`tools/testnet_run.py`](../tools/testnet_run.py). Raw audit log:
[`testnet-run/shop-audit.jsonl`](testnet-run/shop-audit.jsonl).

## Setup

```text
shop (ZecPy, watch-only) -> audit proxy (loopback, records) -> tunnel -> arti (Tor) -> testnet.zec.rocks
agent (ZecPy, hot)       -> ZecPy's built-in Tor                                  -> testnet.zec.rocks
signer (ZecPy)           -> no network
payer (zcash-devtool)    -> testnet.zec.rocks     (a customer's wallet, another codebase)
```

The shop holds a viewing key only (`can_send=False`); its spending key was made offline by
the signer. The payer was funded with 1 TAZ from a public faucet
(`7cf7c141cde4844868d79c5c83a6189a435c8990fe16c1374c2d5775ac781bf7`, block 4,540,684).

## What happened

| Time (UTC) | Step | Evidence |
|---|---|---|
| 16:34:58 | Signer generates a seed offline | no network |
| 16:35:28 | Shop imports the watch bundle, syncs from a coarsened birthday | 2 coarse tree states at 4,539,999 |
| 16:35:46 | Shop issues invoice 1, 0.25 TAZ, its own diversified address | no request |
| 16:36:18 | Payer pays it with a memo, from zcash-devtool | `fe0bdcd309662487095fc870dd9a3f95fdf5280005311d12365745ac03ac3f32` |
| 16:37:27 | Shop **detects** it, matched by address during trial decryption | block 4,540,817, Ironwood |
| 16:38:32 | Shop **confirms** it, invoice settled | 3 confirmations |
| 16:38:44 | Shop reads the memo with `fetch_memos(decoys=7)` | 8 `GetTransaction`, 1 real (below) |
| 16:42:45 | Shop refunds 0.1 TAZ to the agent: proposes and proves a PCZT, the offline signer finds the change with its own key and signs, the shop broadcasts | `a0c65fe0160fa97bec30fdf9b8d7274df9d97e8b9959b08944f9b5073d23785d`, block 4,540,830, fee 10,000 zat |
| 16:42:45 | Shop issues a payment link for 0.0125 TAZ and hands it to the agent | ZIP-321 URI |
| 16:46:31 | Agent calls `pay(link, max_zat=1_000_000)`: **refused**, nothing built | `PrivacyError`: "the request asks for 1250000 zatoshis, more than max_zat=1000000; nothing was built" |
| 16:46:31 | Agent calls `pay(link, max_zat=2_000_000)`: paid over its own Tor | `9b07a288ffe2c9892ee8cf218b9a4d2c95d44f7865cba18a5a56db0dc583d0aa` |
| 16:49:14 | Shop sees the agent's payment at the link's address, confirmed | block 4,540,842, settled |

Fourteen minutes, end to end.

## What the server learned from the shop

108 requests over the whole run:

| Label | Count | Requests |
|---|---|---|
| public | 97 | `GetSubtreeRoots` 63, `GetLatestTreeState` 21, `GetBlockRange` 12, `GetLatestBlock` 1 |
| coarse | 2 | `GetTreeState` at 4,539,999: the birthday rounded down to a 10,000-block boundary |
| broadcast | 1 | `SendTransaction`: the refund |
| identifying | 8 | `GetTransaction`: the opt-in memo fetch |

**No request outside the memo fetch tied anything to the shop.** That covers sync,
detection, confirmation, the invoices and the cold-signed refund. There was no transparent
address query, no exact birthday, and no tree state at the height where a previous sync
stopped. The shop never looked a transaction up by its id, apart from the memo fetch below.

## The memo fetch, unmasked

The audit labels every `GetTransaction` identifying, because each one names a transaction.
`fetch_memos` sends the real one only inside a shuffled set of decoys. Here is what the
server received, in order, with the real one marked from the wallet's own records:

```text
1  09b379847b46ab62afb34378490ae89317b54cfb88262b8d7b63eeb5aba757bd
2  b3148a07f52c176b62472e8e8345314ab49fb39203e90a6555d93e5c9b47a8a0
3  331e3ab821a59c5581482a57dbc648b056e8360ea9315a762fd916656959e827
4  8daaf062a2cfe539894f64891eb7f97fe7af67d723af32993b49a7f6ddf28032
5  b3b5968afbc5391c5ecefc46e0a096db7d570a2eaf3d875222dd56567638d159
6  fe0bdcd309662487095fc870dd9a3f95fdf5280005311d12365745ac03ac3f32   the customer's payment
7  32ebc13d348f782a773822c21a173d0beb835bd19825ac92bc851e0c9399a53c
8  148985a5722ac3bb2f434c52938a95957b1cff6919202f7823638d0259a18787
```

The seven decoys are other people's shielded testnet transactions from nearby blocks, taken
from compact blocks the shop had already downloaded. None of them belongs to this run.
Publishing which one was real gives that up for this transaction, which is the point of a
demonstration on testnet. On a real shop, nobody but the wallet knows.

## What this run does not show

- **The agent's requests were not audited.** It used ZecPy's built-in Tor directly, to show
  that path with a real send. The audit proxy can only record a wallet that talks to it on
  loopback.
- **Tor hides the network address, not the request content.** The proxy sees content, then
  forwards it over Tor. The server saw the shop's requests arrive from Tor exits.
- **No reorg happened during the run,** and no pool crossing was attempted: testnet funds
  arrive in Ironwood, as on mainnet since NU6.3.
