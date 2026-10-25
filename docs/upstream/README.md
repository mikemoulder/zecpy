# Upstream contributions

Problems in the Zcash libraries and tools that building ZecPy uncovered, written up for the
maintainers. Found while building ZecPy, filed during ZECATHON.

| Project | Finding | Filed | Status |
|---|---|---|---|
| librustzcash `zcash_client_backend::sync` | Incremental syncs reveal, via `GetTreeState`, where the previous sync stopped, linking sessions | Issue [#3197](https://github.com/zcash/librustzcash/issues/3197) (design options), PR [#3198](https://github.com/zcash/librustzcash/pull/3198) (absolute batch boundaries) | Open |
| librustzcash `zcash_client_backend::sync` | A reorg onto a branch that is not yet longer goes unnoticed; the wallet reports stale confirmations | PR [#3199](https://github.com/zcash/librustzcash/pull/3199) (hash check at the tip, `reorg_rewind_height` with unit tests) | Open |
| zcash-devtool `pczt sign` | Ironwood spends are left unsigned; the command still exits 0 | Issue [#266](https://github.com/zcash/zcash-devtool/issues/266), PR [#267](https://github.com/zcash/zcash-devtool/pull/267) (verified before and after on regtest) | Open |

Filed 2026-10-25 from xerodahdev. The texts below are the originals they were written from.

Files:

- [`librustzcash-sync-privacy.md`](librustzcash-sync-privacy.md): issue text for both sync
  findings.
- [`0001-zcash_client_backend-Detect-reorgs-onto-a-shorter-br.patch`](0001-zcash_client_backend-Detect-reorgs-onto-a-shorter-br.patch):
  the patch against librustzcash `main` (`146a08f`), with a CHANGELOG entry and a unit test.
  `cargo test -p zcash_client_backend --features sync,orchard,lightwalletd-tonic --lib sync::`
  passes (2 tests).
- [`zcash-devtool-pczt-ironwood.md`](zcash-devtool-pczt-ironwood.md): issue text.
