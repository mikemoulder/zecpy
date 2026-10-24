# Upstream contributions

Problems in the Zcash libraries and tools that building ZecPy uncovered, written up for the
maintainers. Prepared during ZECATHON and filed when ZecPy is published.

| Project | Finding | Evidence | Fix |
|---|---|---|---|
| librustzcash `zcash_client_backend::sync` | Incremental syncs reveal, via `GetTreeState`, where the previous sync stopped, linking sessions | ZecPy leak audit; `docs/privacy-report.md` | Patch aligns batch boundaries; issue proposes persisting the tip tree state, as `src/sync.rs` does |
| librustzcash `zcash_client_backend::sync` | A reorg onto a branch that is not yet longer goes unnoticed; the wallet reports stale confirmations | Reproduced with `invalidateblock` on regtest | Patch compares the block hash at the reported tip |
| zcash-devtool `pczt sign` | Ironwood spends are left unsigned; the command still exits 0 | Reproduced on regtest: `MissingSpendAuthSig` at send | Issue suggests signing with `sign_ironwood` |

Files:

- [`librustzcash-sync-privacy.md`](librustzcash-sync-privacy.md): issue text for both sync
  findings.
- [`0001-zcash_client_backend-Detect-reorgs-onto-a-shorter-br.patch`](0001-zcash_client_backend-Detect-reorgs-onto-a-shorter-br.patch):
  the patch against librustzcash `main` (`146a08f`), with a CHANGELOG entry and a unit test.
  `cargo test -p zcash_client_backend --features sync,orchard,lightwalletd-tonic --lib sync::`
  passes (2 tests).
- [`zcash-devtool-pczt-ironwood.md`](zcash-devtool-pczt-ironwood.md): issue text.
