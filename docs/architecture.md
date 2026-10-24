# Architecture

ZecPy is a Python package with a native Rust core. Python owns the developer-facing API
and the invoice logic; Rust owns everything cryptographic and everything that talks to the
chain, by calling librustzcash. Nothing cryptographic is reimplemented.

## Summary

```text
Style:      one Python package, one native extension (PyO3), no services
Stack:      Rust 1.99 + PyO3 0.30 + pyo3-async-runtimes 0.30 (tokio), built by maturin 1.15
            librustzcash crates.io releases of 2026-10-21 (zcash_client_backend 0.25.0,
            zcash_client_sqlite 0.23.0, zcash_keys 0.17.0, zcash_primitives 0.31.0)
Storage:    zcash_client_sqlite wallet database; one small sqlite file for invoices
Network:    lightwalletd gRPC, over Tor by default
Ships as:   a wheel (`pip install zecpy`), examples, docs
```

## The demo path

The specification. Anything not on this table waits until the table works end to end.

| # | Developer does | ZecPy does | Server learns | Persists |
|---|---|---|---|---|
| 1 | `Wallet.create(...)` | Generates a seed, derives keys, sets a coarsened birthday | One tree-state request for a 10,000-block bucket | wallet.db |
| 2 | `wallet.invoice(amount, ref)` | Derives a fresh shielded address at a new diversifier index | Nothing (offline) | invoices.db |
| 3 | Customer pays | n/a | n/a | n/a |
| 4 | `await wallet.sync()` | Downloads compact blocks, trial-decrypts locally, rewinds on reorg | Only that someone downloaded these blocks | wallet.db |
| 5 | **`async for p in wallet.payments()`** | Maps the received note to its invoice by address, waits for N confirmations | Nothing new | invoices.db |
| 6 | `await wallet.send(to, amount, memo)` | Proposes, proves, signs locally; broadcasts | One transaction, from a Tor exit | wallet.db |

**Aha step: 5.** A payment arrives and is matched to its invoice, while the leak-audit panel
shows the server learned nothing that links it to the merchant. This step gets the careful
error handling: confirmation depth, reorg rollback and exactly-once delivery.

## Diagram

Every box is a folder in this repo or a running process.

```text
   examples/  (payment bot, payroll)
        │
        ▼
   python/zecpy/       Wallet, Invoice, Payment; async API; invoice store
        │
        ▼
   src/  (native core, PyO3)    keys, sync, send, received-output queries
        │         │
        │         └──► wallet.db  (zcash_client_sqlite)
        ▼
   tools/leak_audit/   recording gRPC proxy (tests and demo only)
        │
        ▼
   light server        devnet/ locally; testnet.zec.rocks or any lightwalletd
```

## Components

| Component | Path | Responsibility |
|---|---|---|
| Python API | `python/zecpy/` | `Wallet`, `Invoice`, `Payment`, typed exceptions, `.pyi` stubs. Invoice store and payment watcher (stdlib `sqlite3`) |
| Native core | `src/` | PyO3 classes over librustzcash. Owns the wallet database behind a mutex, runs blocking work off the GIL |
| `src/net.rs` | | Light-server connection: Tor (arti, via `zcash_client_backend/tor`) or direct for loopback only |
| `src/sync.rs` | | Compact-block sync and reorg rewind, ported from zcash-devtool |
| `src/send.rs` | | Proposal, proving (`LocalTxProver::bundled`), signing, broadcast |
| `src/received.rs` | | Read-only queries over `v_received_outputs` joined to `addresses` for diversifier indices |
| Leak audit | `tools/leak_audit/` | gRPC proxy that decodes and classifies every request a wallet makes |
| Devnet | `devnet/` | Local regtest chain (zebrad + lightwalletd), Mainnet-like rules with Ironwood |

## Privacy by construction

| Guarantee | How it is enforced |
|---|---|
| No transparent address queries | ZecPy's own sync loop (`src/sync.rs`) never issues `GetAddressUtxos` or `GetTaddressTxids`; the call that did lived in librustzcash's `sync::run`, which ZecPy does not use. `tests/test_devnet_leak_audit.py` asserts on every run that no such request is made. (Cold signing needs the `pczt` feature, which compiles librustzcash's transparent wallet code in; ZecPy never calls it.) |
| No transaction lookups in the clear | Memo and transaction-detail fetches only run over Tor; otherwise they are skipped and reported as unavailable |
| Server cannot pin the wallet's birthday | Birthday rounded down to a 10,000-block boundary before `GetTreeState` |
| IP not linked to the wallet | Tor is the default for any non-loopback server. If Tor fails, ZecPy raises; it never falls back to a direct connection silently |
| Payments matched without memos | Each invoice gets its own diversified address; matching happens during local trial decryption |
| No secrets in logs or reprs | Seeds, keys and memos are wrapped in redacting types on both sides of the boundary |

Each guarantee has a test in `tests/` that runs the wallet through the leak-audit proxy on
devnet and asserts on what the proxy recorded.

## Concurrency model

- One tokio runtime per process (pyo3-async-runtimes). Async Python methods return
  awaitables backed by tokio futures.
- `WalletDb` is not `Sync`, so each `Wallet` holds it in a `Mutex`; database work and
  proving run in `spawn_blocking`, with the GIL released.
- Sync is cancellable: cancelling the Python task stops after the current batch, leaving
  the database consistent.

## Data owned by ZecPy

Everything else lives in zcash_client_sqlite's schema.

```text
invoice   id, diversifier_index, address, amount_zat, reference, status, created_at
payment   id, invoice_id, txid, pool, output_index, value_zat, height, status
          UNIQUE (txid, pool, output_index)   exactly-once delivery
```

Files beside the wallet database: `chain_state.bin` (tree state at the last synced tip,
so the next sync does not ask where it stopped), `memo_decoys.txt` (decoy candidates and
pending memo fetches; as sensitive as the database) and `tor/` (arti state).

`status` moves `detected → confirmed`, and back to `detected` (or removed) if a reorg drops
the block.

## Python API

As built (`python/zecpy/wallet.py`; types in `python/zecpy/_native.pyi`). Amounts are
zatoshis. Methods marked local send nothing to the server.

```python
class Wallet:
    # Lifecycle
    @staticmethod
    async def create(path, *, network, server, tor=True) -> tuple[Wallet, Mnemonic]: ...
    @staticmethod
    async def restore(path, mnemonic, *, network, server, birthday=None, tor=True) -> Wallet: ...
    @staticmethod
    async def watch(path, bundle, *, server, birthday=None, tor=True) -> Wallet: ...  # viewing key only
    @staticmethod
    def open(path, *, server, mnemonic=None, tor=True) -> Wallet: ...                 # no mnemonic: watch-only

    # Chain and state
    async def sync(self) -> int: ...                       # height synced to
    def balance(self) -> Balance: ...                      # local; per pool, spendable vs pending
    def address(self) -> str: ...                          # local; fresh shielded UA
    def transactions(self, since=0) -> list[Transaction]: ...   # local
    def transaction(self, txid) -> Transaction | None: ...      # local
    async def fetch_memos(self, *, decoys=7, max_delay=10.0) -> MemoReport: ...  # opt-in, Tor

    # Paying
    def propose(self, to, amount_zat, memo=None, **flags) -> Proposal: ...   # local
    def propose_many(self, payments, **flags) -> Proposal: ...               # local, up to 50
    def propose_sweep(self, to, memo=None, *, mode="spendable", **flags) -> Proposal: ...  # local
    async def send_proposal(self, proposal, *, max_broadcast_delay=0.0) -> str: ...
    async def send(self, to, amount_zat, memo=None, **flags) -> str: ...
    async def send_many(self, payments, **flags) -> str: ...
    def parse_request(self, uri) -> PaymentRequest: ...                       # local, ZIP-321
    async def pay(self, uri, *, max_zat, **flags) -> str: ...

    # Cold signing
    def propose_cold(self, to, amount_zat, memo=None, **flags) -> bytes: ...  # unsigned PCZT
    def propose_cold_many(self, payments, **flags) -> bytes: ...
    async def broadcast_signed(self, signed_pczt, *, max_broadcast_delay=0.0) -> str: ...

    invoices: Invoices    # create, get, by_reference, status, paid_zat, poll, watch

# flags: allow_transparent=False, allow_pool_crossing=False (both refusals raise PrivacyError)
```

## Risks

| Risk | Detected by | Fallback |
|---|---|---|
| PyO3 0.30 + tokio + non-`Sync` WalletDb do not fit together | Binding spike (first task) | Synchronous API with a background thread |
| Sync port is larger than it looks (devtool's is 846 lines) | Sync works on devnet by Sunday midday | Port without defrag; keep reorg handling |
| Tor bootstrap is slow or fails during the demo | Timed run on this VPS | Bootstrap once before recording; cache state in the wallet dir |
| Public testnet stays unfunded | Faucet checks | Demo on devnet with the explorer panel replaced by zebrad's own view |

## Cut list

In cut order, decided now:

```text
1. Payroll / batch payouts (payment bot stays)
2. Mempool (0-conf) payment detection
3. Memo reading (needs GetTransaction by txid, which names the transaction)
4. Prebuilt macOS / Windows wheels
```

In scope since Sat night: Tor with isolated circuits, PCZT cold signing with a watch-only
hot wallet, the leak audit as a standalone tool with a cross-wallet report, and the
upstream librustzcash fix.
