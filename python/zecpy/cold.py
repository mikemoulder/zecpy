"""Cold signing: the spending key stays on an offline machine.

    offline machine                         online server (viewing key only)
    ColdSigner(mnemonic).watch_bundle() ->  Wallet.watch(path, bundle)
                                            pczt = wallet.propose_cold(to, amount)
    signer.inspect(pczt); signer.sign() <-  (proved on the server: no secret needed)
                                     ->     await wallet.broadcast_signed(signed)

The server can be fully compromised without losing funds: it never holds a key that can
spend. The signer decides which outputs are change with its own viewing key, so it shows
what will really leave the wallet.

Run ``python -m zecpy.cold --help`` for the offline command-line signer.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from ._native import ColdSigner as _NativeSigner
from ._native import Summary
from .wallet import Mnemonic


@dataclass(frozen=True)
class WatchBundle:
    """What a hot wallet needs to watch an account and build transactions for it. Cannot
    spend, but reveals the account's history: keep it confidential."""

    network: str
    ufvk: str
    seed_fingerprint: str
    account: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> WatchBundle:
        data = json.loads(text)
        return cls(data["network"], data["ufvk"], data["seed_fingerprint"], int(data["account"]))


class ColdSigner:
    """Holds the spending key for one account. Never touches the network."""

    def __init__(
        self,
        mnemonic: Mnemonic | str,
        *,
        network: str,
        account: int = 0,
        regtest_heights: Mapping[str, int] | None = None,
    ) -> None:
        phrase = mnemonic.reveal() if isinstance(mnemonic, Mnemonic) else mnemonic
        heights = dict(regtest_heights) if regtest_heights is not None else None
        self._native = _NativeSigner(phrase, network, heights, account)

    @classmethod
    def generate(
        cls, *, network: str, account: int = 0, regtest_heights: Mapping[str, int] | None = None
    ) -> tuple[ColdSigner, Mnemonic]:
        """Creates a signer with a fresh 24-word seed, entirely offline. Write the mnemonic
        down: it is the only way to recover the funds."""
        mnemonic = Mnemonic(_NativeSigner.generate_mnemonic())
        signer = cls(mnemonic, network=network, account=account, regtest_heights=regtest_heights)
        return signer, mnemonic

    def watch_bundle(self) -> WatchBundle:
        raw = self._native.watch_bundle()
        return WatchBundle(raw["network"], raw["ufvk"], raw["seed_fingerprint"], int(raw["account"]))

    def inspect(self, pczt: bytes) -> Summary:
        """What signing would do: value spent, each output, change detected by key, fee."""
        return self._native.inspect(pczt)

    def sign(self, pczt: bytes) -> bytes:
        """Signs this account's spends. Refuses a PCZT that spends nothing of ours."""
        return bytes(self._native.sign(pczt))

    def __repr__(self) -> str:
        return repr(self._native)


def describe(summary: Summary) -> str:
    """A human-readable account of a PCZT, for the person about to sign it."""
    zec = lambda zat: f"{zat / 100_000_000:.8f} ZEC"  # noqa: E731
    lines = [f"Spends   {zec(summary.spending)} from this wallet"]
    for out in summary.outputs:
        if out.is_change:
            lines.append(f"  change   {zec(out.value):>18}  back to this wallet ({out.pool})")
        else:
            label = out.label or "(no address label: inspect carefully)"
            lines.append(f"  PAY      {zec(out.value):>18}  to {label} ({out.pool})")
    lines.append(f"Leaving  {zec(summary.paying_out)}  plus fee {zec(summary.fee)}")
    if summary.foreign_spends:
        lines.append(f"WARNING  {summary.foreign_spends} spend(s) belong to another key")
    return "\n".join(lines)


def _read_mnemonic() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Mnemonic (input hidden): ")
    return sys.stdin.readline().strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zecpy.cold", description="Offline ZecPy signer.")
    parser.add_argument("--network", required=True, choices=["main", "test", "regtest"])
    parser.add_argument("--account", type=int, default=0)
    parser.add_argument("--regtest-heights", help="JSON file of activation heights (regtest only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new", help="generate a seed offline; print its mnemonic and watch bundle")
    sub.add_parser("bundle", help="print the watch bundle for the hot wallet")
    p_inspect = sub.add_parser("inspect", help="describe an unsigned PCZT")
    p_inspect.add_argument("pczt", type=Path)
    p_sign = sub.add_parser("sign", help="sign a PCZT after showing what it does")
    p_sign.add_argument("pczt", type=Path)
    p_sign.add_argument("out", type=Path)
    p_sign.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    heights = json.loads(Path(args.regtest_heights).read_text()) if args.regtest_heights else None
    if args.command == "new":
        signer, mnemonic = ColdSigner.generate(network=args.network, account=args.account, regtest_heights=heights)
        print("Write these 24 words down and keep them offline. They control the funds.", file=sys.stderr)
        print(mnemonic.reveal(), file=sys.stderr)
        print(signer.watch_bundle().to_json())
        return 0
    signer = ColdSigner(_read_mnemonic(), network=args.network, account=args.account, regtest_heights=heights)

    if args.command == "bundle":
        print(signer.watch_bundle().to_json())
        return 0
    pczt = args.pczt.read_bytes()
    print(describe(signer.inspect(pczt)))
    if args.command == "inspect":
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to sign without confirmation; pass --yes", file=sys.stderr)
            return 1
        if input("Sign? [y/N] ").strip().lower() != "y":
            print("not signed")
            return 1
    args.out.write_bytes(signer.sign(pczt))
    print(f"signed PCZT written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
