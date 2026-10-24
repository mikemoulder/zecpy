"""A shielded checkout API for a shop backend, in one file.

The server holds a viewing key only: it can issue invoices and see payments arrive, but it
cannot move funds. Spending happens on a cold signer (see `python -m zecpy.cold`).

    POST /invoices            {"amount_zat": 150000, "reference": "order-77"}
                              -> a fresh shielded address and a ZIP-321 payment URI
    GET  /invoices/{ref}      -> unpaid | pending | partial | paid
    GET  /health              -> sync height

Each invoice gets its own address, so payments match without memos and without asking the
light server anything. A background task syncs and turns payment events into webhooks,
exactly once each, signed with HMAC-SHA256 so the shop can verify them.

Configuration (environment):
    ZECPY_WALLET            wallet directory (created from the bundle on first run)
    ZECPY_WATCH_BUNDLE      cold signer's watch bundle (JSON file), needed on first run
    ZECPY_SERVER            light server URL, e.g. https://zec.rocks:443
    ZECPY_TOR               "0" to connect directly (default: Tor)
    ZECPY_REGTEST_HEIGHTS   activation heights JSON (regtest only)
    ZECPY_CONFIRMATIONS     confirmations before an invoice is paid (default 3)
    ZECPY_POLL_SECONDS      seconds between syncs (default 10)
    ZECPY_WEBHOOK_URL       where to POST payment events (optional)
    ZECPY_WEBHOOK_SECRET    HMAC key for the X-ZecPy-Signature header

Run: uvicorn examples.checkout.app:app
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from zecpy import Invoice, PaymentEvent, Wallet

log = logging.getLogger("checkout")


class Settings:
    def __init__(self) -> None:
        env = os.environ
        self.wallet = Path(env["ZECPY_WALLET"])
        self.bundle = env.get("ZECPY_WATCH_BUNDLE")
        self.server = env["ZECPY_SERVER"]
        self.tor = env.get("ZECPY_TOR", "1") != "0"
        heights = env.get("ZECPY_REGTEST_HEIGHTS")
        self.regtest_heights = json.loads(Path(heights).read_text()) if heights else None
        self.confirmations = int(env.get("ZECPY_CONFIRMATIONS", "3"))
        self.poll_seconds = float(env.get("ZECPY_POLL_SECONDS", "10"))
        self.webhook_url = env.get("ZECPY_WEBHOOK_URL")
        self.webhook_secret = env.get("ZECPY_WEBHOOK_SECRET", "").encode()


async def open_wallet(settings: Settings) -> Wallet:
    if (settings.wallet / "wallet.db").exists():
        return Wallet.open(settings.wallet, server=settings.server, tor=settings.tor)
    if not settings.bundle:
        raise RuntimeError("first run needs ZECPY_WATCH_BUNDLE (from `python -m zecpy.cold bundle`)")
    return await Wallet.watch(
        settings.wallet, Path(settings.bundle).read_text(), server=settings.server,
        tor=settings.tor, regtest_heights=settings.regtest_heights,
    )


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


async def deliver(settings: Settings, client: httpx.AsyncClient, event: PaymentEvent) -> None:
    """POSTs one event, retrying with backoff. Events are already persisted as delivered by
    the invoice book, so a webhook that keeps failing is logged, not re-sent forever."""
    if not settings.webhook_url:
        return
    kind = {"detected": "payment.detected", "confirmed": "payment.confirmed", "reverted": "payment.reverted"}
    body = json.dumps(
        {
            "type": kind[event.state],
            "reference": event.invoice.reference,
            "txid": event.txid,
            "value_zat": event.value_zat,
            "confirmations": event.confirmations,
            "invoice_paid_zat": event.invoice_paid_zat,
            "invoice_settled": event.invoice_settled,
        },
        sort_keys=True,
    ).encode()
    headers = {"content-type": "application/json", "x-zecpy-signature": sign(settings.webhook_secret, body)}
    for attempt in range(4):
        try:
            response = await client.post(settings.webhook_url, content=body, headers=headers, timeout=10)
            if response.status_code < 300:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2**attempt)
    log.error("webhook for %s %s failed after retries", event.invoice.reference, event.state)


async def watch_payments(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    wallet: Wallet = app.state.wallet
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await wallet.sync()
                for event in wallet.invoices.poll(settings.confirmations):
                    zec = event.value_zat / 100_000_000
                    log.info(
                        "invoice %s %s %.8f ZEC (%d confirmations) tx %s",
                        event.invoice.reference, event.state.upper(), zec, event.confirmations, event.txid[:16],
                    )
                    await deliver(settings, client, event)
            except Exception:  # keep watching through a dropped connection or a server hiccup
                log.exception("sync failed; retrying")
            await asyncio.sleep(settings.poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.settings = Settings()
    app.state.wallet = await open_wallet(app.state.settings)
    if app.state.wallet.can_send:
        log.warning("this checkout holds a spending key; prefer a watch-only wallet")
    watcher = asyncio.create_task(watch_payments(app))
    try:
        yield
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


app = FastAPI(title="ZecPy checkout", lifespan=lifespan)


class InvoiceRequest(BaseModel):
    amount_zat: int = Field(gt=0, le=21_000_000 * 100_000_000)
    reference: str = Field(min_length=1, max_length=128)


def _view(invoice: Invoice, wallet: Wallet) -> dict[str, object]:
    status = wallet.invoices.status(invoice)
    return {
        "reference": invoice.reference,
        "amount_zat": invoice.amount_zat,
        "address": invoice.address,
        "uri": invoice.uri,
        "status": status.state,
        "confirmed_zat": status.confirmed_zat,
        "pending_zat": status.pending_zat,
    }


@app.post("/invoices", status_code=201)
async def create_invoice(request: InvoiceRequest) -> dict[str, object]:
    wallet: Wallet = app.state.wallet
    try:
        wallet.invoices.by_reference(request.reference)
    except KeyError:
        pass
    else:
        raise HTTPException(409, f"invoice {request.reference!r} already exists")
    return _view(wallet.invoices.create(request.amount_zat, request.reference), wallet)


@app.get("/invoices/{reference}")
async def get_invoice(reference: str) -> dict[str, object]:
    wallet: Wallet = app.state.wallet
    try:
        invoice = wallet.invoices.by_reference(reference)
    except KeyError:
        raise HTTPException(404, f"no invoice {reference!r}") from None
    return _view(invoice, wallet)


@app.get("/health")
async def health() -> dict[str, object]:
    wallet: Wallet = app.state.wallet
    balance = wallet.balance()
    return {"synced_height": balance.scanned_height, "chain_tip": balance.chain_tip, "can_spend": wallet.can_send}


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
