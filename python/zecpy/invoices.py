"""Invoices paid to their own shielded addresses, and exactly-once payment events.

Each invoice gets the address at its own diversifier index. When the wallet scans a block
and decrypts a note, the database records which address received it, so a payment maps to
its invoice without a memo and without asking the server anything.

Events are emitted on state changes only, and the last emitted state of every payment is
persisted, so a restarted process does not deliver the same payment twice:

    detected   mined, fewer than the required confirmations
    confirmed  at least the required confirmations
    reverted   a reorg removed the block that mined it; it may be confirmed again later
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, AsyncIterator, Literal

if TYPE_CHECKING:
    from .wallet import Wallet

# Invoice addresses come from their own diversifier range, far above anything the wallet's
# sequential `address()` calls reach, so no invoice address is ever handed out twice.
INVOICE_INDEX_BASE = 1 << 80

_DB = "invoices.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice (
    id          INTEGER PRIMARY KEY,
    reference   TEXT NOT NULL UNIQUE,
    amount_zat  INTEGER NOT NULL CHECK (amount_zat > 0),
    address     TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payment (
    txid          TEXT NOT NULL,
    pool          TEXT NOT NULL,
    output_index  INTEGER NOT NULL,
    invoice_id    INTEGER NOT NULL REFERENCES invoice(id),
    value_zat     INTEGER NOT NULL,
    state         TEXT NOT NULL,
    height        INTEGER,
    PRIMARY KEY (txid, pool, output_index)
);
"""

PaymentState = Literal["detected", "confirmed", "reverted"]


@dataclass(frozen=True)
class Invoice:
    id: int
    reference: str
    amount_zat: int
    address: str
    created_at: int

    @property
    def uri(self) -> str:
        """A ZIP-321 payment URI, for QR codes and wallet deep links."""
        amount = format(Decimal(self.amount_zat) / Decimal(100_000_000), "f").rstrip("0").rstrip(".")
        message = urllib.parse.quote(self.reference, safe="")
        return f"zcash:{self.address}?amount={amount}&message={message}"


@dataclass(frozen=True)
class PaymentEvent:
    state: PaymentState
    invoice: Invoice
    txid: str
    pool: str
    value_zat: int
    height: int | None
    confirmations: int
    #: Confirmed value paid to this invoice so far, across all of its payments.
    invoice_paid_zat: int

    @property
    def invoice_settled(self) -> bool:
        return self.invoice_paid_zat >= self.invoice.amount_zat


class Invoices:
    """Invoice book for one wallet, stored next to it in ``invoices.db``."""

    def __init__(self, wallet: Wallet) -> None:
        self._wallet = wallet
        self._db = sqlite3.connect(wallet.path / _DB)
        self._db.executescript(_SCHEMA)

    def create(self, amount_zat: int, reference: str) -> Invoice:
        """Creates an invoice with a fresh shielded address of its own."""
        if amount_zat <= 0:
            raise ValueError("amount must be positive")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO invoice (reference, amount_zat, address, created_at) VALUES (?, ?, '', ?)",
                (reference, amount_zat, int(time.time())),
            )
            invoice_id = cur.lastrowid
            assert invoice_id is not None
            address = self._wallet._native.address_at(INVOICE_INDEX_BASE + invoice_id)
            self._db.execute("UPDATE invoice SET address = ? WHERE id = ?", (address, invoice_id))
        return self.get(invoice_id)

    def get(self, invoice_id: int) -> Invoice:
        row = self._db.execute(
            "SELECT id, reference, amount_zat, address, created_at FROM invoice WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            raise KeyError(invoice_id)
        return Invoice(*row)

    def by_reference(self, reference: str) -> Invoice:
        row = self._db.execute("SELECT id FROM invoice WHERE reference = ?", (reference,)).fetchone()
        if row is None:
            raise KeyError(reference)
        return self.get(row[0])

    def paid_zat(self, invoice: Invoice) -> int:
        """Confirmed value received for an invoice."""
        (total,) = self._db.execute(
            "SELECT COALESCE(SUM(value_zat), 0) FROM payment WHERE invoice_id = ? AND state = 'confirmed'",
            (invoice.id,),
        ).fetchone()
        return int(total)

    def poll(self, confirmations: int = 3) -> list[PaymentEvent]:
        """Compares what the wallet has scanned with the events already emitted, records
        the differences and returns them. Call after ``wallet.sync()``."""
        if confirmations < 1:
            raise ValueError("confirmations must be at least 1")
        tip = self._wallet.balance().scanned_height
        by_index = {
            INVOICE_INDEX_BASE + row[0]: row[0]
            for row in self._db.execute("SELECT id FROM invoice")
        }
        events: list[PaymentEvent] = []
        for out in self._wallet._native.received():
            invoice_id = by_index.get(out.diversifier_index)
            if invoice_id is None:
                continue
            if out.mined_height is None:
                depth = 0
            else:
                depth = max(0, tip - out.mined_height + 1)
            key = (out.txid, out.pool, out.output_index)
            previous = self._db.execute(
                "SELECT state FROM payment WHERE txid = ? AND pool = ? AND output_index = ?", key
            ).fetchone()
            state = _next_state(previous[0] if previous else None, depth, confirmations)
            if state is None:
                continue
            with self._db:
                self._db.execute(
                    "INSERT INTO payment (txid, pool, output_index, invoice_id, value_zat, state, height)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (txid, pool, output_index) DO UPDATE SET state = excluded.state,"
                    " height = excluded.height",
                    (*key, invoice_id, out.value, state, out.mined_height),
                )
            invoice = self.get(invoice_id)
            events.append(
                PaymentEvent(
                    state=state,
                    invoice=invoice,
                    txid=out.txid,
                    pool=out.pool,
                    value_zat=out.value,
                    height=out.mined_height,
                    confirmations=depth,
                    invoice_paid_zat=self.paid_zat(invoice),
                )
            )
        return events

    async def watch(
        self, confirmations: int = 3, poll_interval: float = 10.0
    ) -> AsyncIterator[PaymentEvent]:
        """Syncs the wallet in a loop and yields payment events as they happen."""
        while True:
            await self._wallet.sync()
            for event in self.poll(confirmations):
                yield event
            await asyncio.sleep(poll_interval)


def _next_state(previous: str | None, depth: int, confirmations: int) -> PaymentState | None:
    """The state to emit for a payment, or None if nothing changed."""
    if depth == 0:
        # Unmined: only interesting if it had been seen in a block before.
        return "reverted" if previous in ("detected", "confirmed") else None
    current: PaymentState = "confirmed" if depth >= confirmations else "detected"
    return None if current == previous else current
