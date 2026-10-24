# ZecPy

A Python SDK that lets backends, bots and AI agents send and receive shielded Zcash in a
few lines, private by default, built on librustzcash via PyO3.

Hackathon: ZECATHON (zkSNARKs), track Core & Tooling. Deadline Oct 28, 2026 (confirm the
exact cutoff on thezecathon.com). Feature freeze: Tue Oct 27, 18:00 UTC.

**We are building to win, not to submit.** Every feature must survive a question from a
Zcash core engineer. Fewer things, done deeply, with the depth visible in the demo.

## Before starting work

Read `handoff.md` first, then verify it against the repo (`git log --oneline -5`,
`git status --porcelain`). The repository is right when they disagree. `memory.md` has
the durable project knowledge; do not rediscover what is written there.

## Commands

```bash
uv venv && source .venv/bin/activate      # once
uv pip install maturin pytest             # once
export CARGO_BUILD_JOBS=2                 # 7.8GB RAM box: more jobs swap during linking
cargo check                               # cheapest useful check
maturin develop                           # build the extension into the venv
pytest -q                                 # Python tests (unit + testnet-marked)
cargo test                                # Rust tests
```

No deployed URL: the deliverable is a library, a wheel, examples and docs.

## Privacy invariants (a leak is disqualifying, not a deduction)

Any change that touches network, storage or logging is checked against these:

1. Sends spend only shielded funds. Transparent recipients are refused unless the caller
   passes an explicit opt-in.
2. Nothing that identifies the wallet's transactions (txid lookups, memo fetches,
   transparent address queries) goes to the light server in the clear. It goes via Tor or
   it does not happen.
3. Seeds, spending keys, viewing keys and memo contents are never logged, printed in
   reprs, or included in exception messages.
4. Payment matching uses per-invoice diversified addresses, not memos.
5. Every privacy claim in the README is backed by a leak-audit test that asserts it.

## Working stance

- **Inspect before modifying.** Find the file, understand it, check callers, then make
  the smallest change that works.
- **Reuse, do not reinvent.** All cryptography, key handling, proving and signing stay in
  librustzcash. zcash-devtool is the reference for how the crates are wired together.
- **Verify before reporting.** `cargo check`, then the targeted test, then
  `maturin develop` and the example. Never say it works without having run it.
- **Testnet is the truth.** Mocks are for unit tests; claims in docs come from real
  testnet runs with txids recorded.
- **Stop after two failed attempts** on the same problem. Record it in `handoff.md` under
  Do Not Repeat and change approach.
- **Protect the scope.** New ideas get MUST / SHOULD / COULD / NOT NOW before any code.

## Communication

Plain language. Found / Changed / Verified / Next, proportional to the change.

## Before ending a session

Verify, commit, then update `handoff.md` with the exact stopping point and the single
next step. Update `memory.md` only when something durable changed.

## Never

- Commit a secret, a seed, a viewing key, a wallet database, or a `.env` file.
- Use a mainnet seed anywhere in this repo. Testnet only.
- Add AI attribution to a commit: no `Co-Authored-By` naming a tool, no generated-by
  footer, no robot emoji.
- Use em dashes in the README, docs, commit messages or comments.
- Commit or push as anyone but `xerodahdev <xero.dev.ai@gmail.com>`, over an HTTPS remote.
- Touch `/home/coder/zecathon`. It is a separate project.
- Refactor after Tue Oct 27 midday.
