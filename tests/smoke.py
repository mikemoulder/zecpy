"""Smoke test for an installed ZecPy wheel: does it work on this machine?

    python smoke.py              # offline checks, seconds
    python smoke.py --network    # also creates a testnet wallet and syncs it over Tor

Runs against the installed package, never against a source checkout, so CI can use it to
prove that a wheel built for a platform works there. No pytest needed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTNET = "https://testnet.zec.rocks:443"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        sys.exit(1)


def offline() -> None:
    import zecpy
    from zecpy import ColdSigner, Mnemonic, PrivacyError, WatchBundle, ZecPyError

    location = Path(zecpy.__file__).resolve()
    check("imports the installed package", "site-packages" in location.parts or "dist-packages" in location.parts,
          str(location.parent))
    check("version", True, importlib.metadata.version("zecpy"))
    check("error hierarchy", issubclass(PrivacyError, ZecPyError))

    signer, mnemonic = ColdSigner.generate(network="test")
    phrase = mnemonic.reveal()
    check("seed generated offline", len(phrase.split()) == 24)
    check("mnemonic hidden from repr and str", phrase.split()[0] not in repr(mnemonic) + str(mnemonic)
          and isinstance(mnemonic, Mnemonic))
    check("signer repr reveals no words", not any(w in repr(signer).split() for w in phrase.split()))

    bundle = signer.watch_bundle()
    check("watch bundle round trip", WatchBundle.from_json(bundle.to_json()) == bundle,
          bundle.ufvk[:12] + "...")
    again = ColdSigner(phrase, network="test").watch_bundle()
    check("same seed, same viewing key", again.ufvk == bundle.ufvk)

    cli = shutil.which("zecpy-cold")
    check("zecpy-cold on PATH", cli is not None, cli or "")
    done = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=60)
    check("zecpy-cold --help", done.returncode == 0 and "sign" in done.stdout)


async def network() -> None:
    from zecpy import Wallet

    with tempfile.TemporaryDirectory() as tmp:
        start = time.monotonic()
        wallet, _ = await Wallet.create(Path(tmp) / "w", network="test", server=TESTNET)
        check("testnet wallet created over Tor", True, f"{time.monotonic() - start:.0f} s")
        height = await wallet.sync()
        check("synced to the testnet tip over Tor", height > 4_500_000, f"height {height}")
        check("shielded address", wallet.address().startswith("utest1"))
        invoice = wallet.invoices.create(150_000, "smoke")
        check("invoice with a ZIP-321 URI", invoice.uri.startswith("zcash:utest1"))
        check("empty balance", wallet.balance().total == 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="also sync a testnet wallet over Tor")
    args = parser.parse_args()
    print(f"ZecPy smoke test on {platform.system()} {platform.machine()}, Python {platform.python_version()}")
    offline()
    if args.network:
        asyncio.run(network())
    print("all checks passed")


if __name__ == "__main__":
    main()
