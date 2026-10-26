# Security policy

ZecPy handles money and promises privacy. Both kinds of failure are security bugs here:

- **Vulnerabilities:** anything that could lose funds, sign something the user did not
  approve, expose a seed, spending key, viewing key or memo, or let a malicious light server
  or network attacker change what the wallet does.
- **Privacy leaks:** any request, timing pattern or log line that lets a light server or a
  network observer learn which addresses or transactions belong to the wallet, link two
  sessions, or learn the user's network address when Tor is on. A leak is not a
  "privacy enhancement" request: it is treated with the same urgency as a vulnerability.

## Report privately

Please do **not** open a public issue. Use GitHub's private reporting instead:
**Security** tab of this repository, then **Report a vulnerability**
([direct link](https://github.com/xerodahdev/zecpy/security/advisories/new)).

A useful report includes:

- what an attacker or a server learns or can do, and from what position;
- steps to reproduce, ideally on testnet or the local devnet;
- for a leak, the leak-audit record (`python -m tools.leak_audit --log audit.jsonl`), with
  any real addresses or transaction ids replaced;
- the ZecPy version (`pip show zecpy`), Python version and platform.

Never send a mainnet seed, spending key or viewing key, in a report or anywhere else.

## What to expect

- Acknowledgement within 3 days.
- An assessment and a plan within 10 days: confirmed or not, severity, and a fix timeline.
- A fix released as a new version on PyPI, with a GitHub security advisory crediting you
  unless you prefer otherwise.
- Problems in librustzcash, arti or zcash-devtool are reported upstream with you, following
  their own policies.

## Supported versions

ZecPy is alpha. Fixes go into the latest release; upgrade with `pip install -U zecpy`.

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Scope notes

- **Known, documented limitations** are not vulnerabilities: they are listed in
  [docs/privacy-model.md](docs/privacy-model.md#known-limitations) (for example, one
  identifying tree-state request after a reorg). Reports that show one is worse than
  documented are welcome.
- **Opt-outs are the caller's choice:** `tor=False`, `allow_transparent=True` and
  `allow_pool_crossing=True` reveal what their documentation says they reveal.
- **The local machine is trusted:** an attacker with access to the wallet directory or the
  running process is out of scope.
