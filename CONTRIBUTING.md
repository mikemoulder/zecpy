# Contributing to ZecPy

Thank you for helping. ZecPy is small on purpose: a few objects, done carefully, each one
checked against what a light server can learn from it. This guide gets you from a fresh
clone to a merged pull request, and explains the one rule that decides most reviews.

## The rule: the leak budget is zero

A change that lets the light server learn something new about the wallet is a bug, however
useful the feature it adds. "Learn something" means any request whose content ties it to the
wallet's addresses, transactions or history: a transparent address, a transaction id, an exact
height only this wallet would ask for, a timing that links two sessions.

That rule is checked, not trusted. The leak-audit proxy records every request a wallet makes,
and the test suite asserts on the record. If your change touches the network, storage or
logging, your pull request shows what the audit saw.

The full set of invariants every change is held to:

1. **Sends spend only shielded funds.** Transparent recipients are refused unless the caller
   passes an explicit opt-in.
2. **Nothing that identifies the wallet's transactions goes to the server in the clear.**
   Transaction lookups, memo fetches and transparent address queries go over Tor or do not
   happen.
3. **Seeds, spending keys, viewing keys and memo contents are never logged,** printed in a
   repr, or included in an exception message.
4. **Payments are matched by per-invoice diversified addresses, not by memos.**
5. **Every privacy claim in the docs is backed by a test that asserts it.**

## Ways to help

| Area | Good for | Where to start |
|---|---|---|
| **More wallets in the privacy report** | Anyone who uses a light wallet | Run it through `tools/leak_audit` with the five scenarios in `tools/leak_audit/report.py` |
| **A Windows wheel** | Rust on Windows, MSVC, arti | `.github/workflows/wheels.yml`, then `tests/smoke.py` on Windows |
| **An MCP server for agents** | Agent framework users | `parse_request` as a read-only tool, `pay` with a budget set by code (see the [agent guide](https://zecpy.pages.dev/docs/agents)) |
| **Removing ZecPy's own sync loop** | Rust, librustzcash | Help land [librustzcash #3197](https://github.com/zcash/librustzcash/issues/3197), [#3198](https://github.com/zcash/librustzcash/pull/3198) and [#3199](https://github.com/zcash/librustzcash/pull/3199) upstream |
| **Docs and examples** | Anyone | The website sources live in [zecpy-site](https://github.com/mikemoulder/zecpy-site); examples in `examples/` |
| **Review** | Zcash protocol people | Read [docs/privacy-model.md](docs/privacy-model.md) and tell us what it gets wrong |

Issues labelled `good first issue` are small and well described. If you want to work on
something larger, open an issue first so we can agree on the approach before you write it.

## Set up

You need Linux or macOS, Python 3.10 or newer, and a Rust toolchain
([rustup](https://rustup.rs); tested with 1.99).

```bash
git clone https://github.com/mikemoulder/zecpy && cd zecpy
uv venv .venv && . .venv/bin/activate        # or: python -m venv .venv
uv pip install -e '.[dev,examples]'           # builds the Rust extension in place
```

The first build compiles librustzcash and takes 11 to 19 minutes on a 4-core machine. On a
machine with 8 GB of RAM or less, `export CARGO_BUILD_JOBS=2` so linking does not swap. After
changing Rust code, rebuild with `maturin develop`.

### The local chain

Most tests run against **devnet**, a private regtest chain on your machine: zebrad and
lightwalletd, every upgrade through NU6.3 (Ironwood) active from block 1, blocks on demand.
[devnet/README.md](devnet/README.md) lists the binaries and where to get them.

```bash
devnet/up.sh <a uregtest1... address for block rewards>
devnet/mine.sh 101            # coinbase matures after 100 blocks
devnet/down.sh                # stop, keep state; --wipe to start over
```

## Test

| Command | What it runs | Time |
|---|---|---|
| `cargo check` | Rust type check, the cheapest useful check | seconds |
| `pytest -q tests/test_*_unit.py` | Python unit tests, no chain needed | seconds |
| `pytest -q` | The full suite on devnet: real blocks, proofs, reorgs, the leak audit | about an hour |
| `pytest -m network` | Create and sync a wallet on public testnet over Tor | minutes |
| `python tests/smoke.py --network` | Against an installed wheel, outside the repo | a minute |
| `cargo test` | Rust unit tests (see below if linking fails) | minutes |

If `cargo test` cannot find `libpython3.x.so`, symlink `libpython3.x.so.1` under that name in
a scratch directory and run with `LIBRARY_PATH=<that dir> cargo test`.

## Prove a change leaks nothing

Any change to sync, send, memos, Tor or the server connection needs evidence from the audit.

**Watch it live.** Start the proxy in front of devnet and point a wallet at it:

```bash
python -m tools.leak_audit --listen 127.0.0.1:19167 --upstream 127.0.0.1:19067
```

```python
wallet, _ = await Wallet.create("./w", network="regtest", server="http://127.0.0.1:19167",
                                regtest_heights=heights)
```

Every request prints as it arrives, labelled `public`, `coarse`, `broadcast` or
`identifying`. Ctrl-C prints a summary. Unknown request types count as identifying.

**Assert it in a test.** Put an `AuditProxy` in front of devnet, run your scenario through
it, and assert on what it recorded, the way `tests/test_devnet_leak_audit.py` does:

```python
import pytest
from tools.leak_audit import AuditProxy
from zecpy import Wallet

pytestmark = pytest.mark.devnet

@pytest.fixture
async def proxy(tmp_path):
    audit = AuditProxy("127.0.0.1:0", "127.0.0.1:19067", log_path=tmp_path / "audit.jsonl")
    await audit.start()
    yield audit
    await audit.stop()

async def test_my_feature_reveals_nothing(tmp_path, devnet, proxy):
    wallet, _ = await Wallet.create(tmp_path / "w", network="regtest",
                                    server=proxy.url, regtest_heights=devnet.heights)
    await wallet.sync()                    # ... then exercise your feature
    assert proxy.summary()["identifying"] == 0, [
        (r.method, r.reason) for r in proxy.records]
```

The `devnet` fixture comes from `tests/conftest.py`.

**Paste the summary** into your pull request. A feature that cannot pass this needs a design
discussion, an explicit opt-in, and a line in the privacy model saying what it costs.

To audit on public testnet, the proxy can reach the server over Tor through a local tunnel:
see `tools/leak_audit/tunnel.py` and [docs/testnet-run.md](docs/testnet-run.md).

## Review checklist

Your pull request template carries this list. Reviewers check it too.

- [ ] Every request the change adds or alters is labelled `public`, `coarse` or `broadcast`
      by the audit, or is opt-in, documented, and goes over Tor.
- [ ] No seed, key, memo or address of the user appears in a log line, a repr, an exception
      message or a test failure message.
- [ ] Refusals raise `PrivacyError` with the reason and the flag that overrides it.
- [ ] Amounts are integer zatoshis; no floats near money.
- [ ] New behaviour has a test; new privacy behaviour has a test that asserts on the audit.
- [ ] Cryptography stays in librustzcash. ZecPy calls it; it does not reimplement it.
- [ ] Docs updated: the API reference, and the privacy model if a guarantee changed.

## Code style

- **Rust:** `cargo fmt` and `cargo clippy`. Blocking work and proving run off the event loop
  with the GIL released. Map errors to `PrivacyError`, `WalletError`, `NetworkError` or
  `ValueError`, with messages that help without revealing anything.
- **Python:** typed, async where it touches the network, small public surface. Keep `_native.pyi`
  in step with the Rust bindings.
- **Comments** explain why, not what. Privacy-relevant decisions say what the server would
  learn otherwise.

## Commits and pull requests

- Conventional commit messages: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `build:`,
  `chore:`. One logical change per commit.
- Keep pull requests focused. A refactor and a feature are two pull requests.
- Describe what the light server sees before and after, when it changes.
- By contributing you agree that your work is licensed under the project's terms: MIT or
  Apache-2.0, at the user's option.

## Report a leak or a vulnerability

Do not open a public issue for a privacy leak or a security problem. Follow
[SECURITY.md](SECURITY.md). For everything else, issues are welcome.

## Community

Be kind, assume good faith, and keep the discussion on the work. The
[code of conduct](CODE_OF_CONDUCT.md) applies everywhere the project is discussed.
