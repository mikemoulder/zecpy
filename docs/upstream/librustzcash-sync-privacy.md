# librustzcash: `sync::run` links sessions through `GetTreeState`, and misses reorgs onto a shorter branch

Prepared for zcash/librustzcash. Not yet filed (to be filed when ZecPy is published).
Against `main` at `146a08f` (2026-10-22); `zcash_client_backend/src/sync.rs` last changed
2026-07-27. A patch for the two contained fixes is in
`0001-zcash_client_backend-Detect-reorgs-onto-a-shorter-br.patch`.

## 1. Incremental syncs tell the server where the previous sync stopped

`running()` fetches the note commitment tree state for the block before every batch it
scans (`download_chain_state(client, scan_range.block_range().start - 1)`, twice in the
file). On an incremental sync the first batch starts right after the wallet's last scanned
block, so the request is `GetTreeState(<height where the last sync stopped>)`.

That height is specific to the wallet. A server that logs requests can link one sync session
to the next ("a wallet that last stopped at 334 is back"), including across IP addresses or
Tor circuits. Batch boundaries inside a session have the same problem, because they are
offsets (`start + batch_size`) from that height.

**Measured.** ZecPy's leak-audit proxy (`tools/leak_audit/`) records and classifies every
request a wallet sends. While ZecPy still synced with `sync::run`, the audit of a merchant
cycle on a regtest chain flagged `GetTreeState` at height 334, exactly where the previous
sync had stopped (ZecPy commit `a4de237` replaced it). zcash-devtool, which has its own sync
loop following the same pattern, shows the same request on every incremental sync; see
`docs/privacy-report.md` in the ZecPy repo.

**Contained fix in the patch:** split batches at absolute multiples of `batch_size`
(`next_batch_boundary`). Requests for later batches then name heights that every wallet
crossing them asks for.

**Remaining, needs a design decision:** the first batch of a session still needs the state
at the last scanned height. Options:

1. Make the session target the tip reported by `GetLatestTreeState` (a request every wallet
   makes), scan exactly to it, and persist that tree state for the next session. This is
   what ZecPy does (`src/sync.rs`): its audit shows no identifying request across create,
   sync, receive, restart and send. In librustzcash it would need somewhere to persist the
   state: a `BlockCache` method, or a `WalletWrite` method storing frontiers.
2. Derive the chain state at the last scanned height from the wallet's own commitment
   trees, which already hold that frontier.

## 2. A reorg onto a branch that is not longer goes unnoticed

`update_chain_tip` passes the server's tip height to `WalletWrite::update_chain_tip`. If the
new branch is not yet longer than what the wallet scanned, no new scan range appears and the
wallet keeps its view of the abandoned branch, reporting confirmations for transactions that
are no longer in the best chain, until the new branch outgrows the old one.

**Reproduced** on a regtest chain with `invalidateblock`: a payment confirmed at height H;
the block at H is invalidated and the payment re-mined at H on a new branch one block long.
Before the fix the wallet reported 4 confirmations; the true count was 1.
(ZecPy test: `tests/test_devnet_reorg.py::test_reorg_to_a_shorter_branch_is_detected`.)

**Fix in the patch:** when the reported tip is at or below `block_max_scanned`, compare the
hash in `GetLatestBlock`'s `BlockID` with `get_block_hash` at that height. A different hash
is a reorg: truncate, clear the block cache above the rewind height, and rescan. The same
hash means the server is behind, and nothing changes. (zcash-devtool carries its own variant
of this check in its sync command; this moves it into the library.)

`BlockID.hash` from lightwalletd is in internal byte order (checked against zebrad's
`getblockhash` on regtest), so it compares directly with `BlockHash`.
