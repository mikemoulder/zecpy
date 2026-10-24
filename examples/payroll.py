"""Shielded payroll from a CSV: everyone paid in one transaction.

    name,address,amount_zec,memo
    Ada,u1...,1.25,October salary
    Lin,u1...,0.8,

One transaction instead of one per employee: lower fees, and no burst of separate payments
whose timing links them. Amounts and recipients stay shielded on chain.

Hot wallet (holds the mnemonic in ZECPY_MNEMONIC):
    python examples/payroll.py pay payroll.csv --wallet ./w --server https://zec.rocks:443

Watch-only wallet with a cold signer:
    python examples/payroll.py propose payroll.csv --wallet ./w --server ... --out pay.pczt
    zecpy-cold --network main sign pay.pczt pay.signed.pczt        # on the offline machine
    python examples/payroll.py broadcast pay.signed.pczt --wallet ./w --server ...
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from zecpy import Payout, Wallet

ZAT_PER_ZEC = Decimal(100_000_000)


@dataclass(frozen=True)
class Line:
    name: str
    payout: Payout


def zec_to_zat(text: str) -> int:
    """Exact decimal conversion; refuses amounts finer than one zatoshi."""
    try:
        zat = Decimal(text.strip()) * ZAT_PER_ZEC
    except InvalidOperation:
        raise ValueError(f"{text!r} is not an amount") from None
    if zat != zat.to_integral_value() or zat <= 0:
        raise ValueError(f"{text!r} must be a positive amount with at most 8 decimals")
    return int(zat)


def read_payroll(path: Path) -> list[Line]:
    lines, errors = [], []
    with path.open(newline="") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                lines.append(Line(row["name"], Payout(row["address"].strip(), zec_to_zat(row["amount_zec"]), row.get("memo") or None)))
            except (KeyError, ValueError) as e:
                errors.append(f"row {row_no}: {e}")
    if errors:
        raise ValueError("payroll file has errors:\n  " + "\n  ".join(errors))
    if not lines:
        raise ValueError("payroll file has no rows")
    return lines


def show(lines: list[Line]) -> None:
    for line in lines:
        print(f"  {line.name:<20} {Decimal(line.payout.amount_zat) / ZAT_PER_ZEC:>14} ZEC  {line.payout.address[:20]}...")
    total = sum(line.payout.amount_zat for line in lines)
    print(f"  {'total':<20} {Decimal(total) / ZAT_PER_ZEC:>14} ZEC  ({len(lines)} recipients, one transaction)")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["pay", "propose", "broadcast"])
    parser.add_argument("file", type=Path, help="payroll CSV, or the signed PCZT for broadcast")
    parser.add_argument("--wallet", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--no-tor", action="store_true")
    parser.add_argument("--out", type=Path, help="where `propose` writes the unsigned PCZT")
    args = parser.parse_args(argv)

    mnemonic = os.environ.get("ZECPY_MNEMONIC") if args.command == "pay" else None
    wallet = Wallet.open(args.wallet, server=args.server, tor=not args.no_tor, mnemonic=mnemonic)
    await wallet.sync()

    if args.command == "broadcast":
        print("txid", await wallet.broadcast_signed(args.file.read_bytes()))
        return 0

    lines = read_payroll(args.file)
    show(lines)
    payouts = [line.payout for line in lines]
    if args.command == "pay":
        if not wallet.can_send:
            print("this wallet is watch-only: use `propose`, sign with zecpy-cold, then `broadcast`", file=sys.stderr)
            return 1
        print("txid", await wallet.send_many(payouts))
    else:
        if args.out is None:
            parser.error("propose needs --out")
        args.out.write_bytes(wallet.propose_cold_many(payouts))
        print(f"unsigned PCZT written to {args.out}; sign it offline with zecpy-cold")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
