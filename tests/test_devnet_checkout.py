"""The checkout example, driven over HTTP against the regtest chain."""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from zecpy import ColdSigner, Wallet

pytestmark = pytest.mark.devnet

SECRET = "test-webhook-secret"


class WebhookReceiver:
    """A minimal HTTP server that records POSTed webhook bodies and their signatures."""

    def __init__(self) -> None:
        self.received: list[tuple[dict, str]] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = dict(
            line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if ": " in line
        )
        headers = {k.lower(): v for k, v in headers.items()}
        body = await reader.readexactly(int(headers["content-length"]))
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.received.append((json.loads(body), "valid" if headers.get("x-zecpy-signature") == expected else "BAD"))
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()


async def test_checkout_invoice_is_paid_and_webhook_signed(tmp_path, devnet, monkeypatch):
    phrase = (await Wallet.create(
        tmp_path / "seed", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    ))[1]
    bundle = tmp_path / "bundle.json"
    bundle.write_text(ColdSigner(phrase, network="regtest", regtest_heights=devnet.heights).watch_bundle().to_json())
    heights = tmp_path / "heights.json"
    heights.write_text(json.dumps(devnet.heights))

    receiver = WebhookReceiver()
    server = await asyncio.start_server(receiver.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for key, value in {
        "ZECPY_WALLET": str(tmp_path / "checkout"),
        "ZECPY_WATCH_BUNDLE": str(bundle),
        "ZECPY_SERVER": devnet.server,
        "ZECPY_REGTEST_HEIGHTS": str(heights),
        "ZECPY_CONFIRMATIONS": "3",
        "ZECPY_POLL_SECONDS": "1",
        "ZECPY_WEBHOOK_URL": f"http://127.0.0.1:{port}/hook",
        "ZECPY_WEBHOOK_SECRET": SECRET,
    }.items():
        monkeypatch.setenv(key, value)

    from examples.checkout.app import app

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shop") as http:
            assert (await http.get("/health")).json()["can_spend"] is False

            created = await http.post("/invoices", json={"amount_zat": 150_000, "reference": "order-77"})
            assert created.status_code == 201
            invoice = created.json()
            assert invoice["status"] == "unpaid"
            assert invoice["uri"].startswith(f"zcash:{invoice['address']}?amount=0.0015")
            assert (await http.post("/invoices", json={"amount_zat": 1, "reference": "order-77"})).status_code == 409
            assert (await http.post("/invoices", json={"amount_zat": -5, "reference": "x"})).status_code == 422
            assert (await http.get("/invoices/nope")).status_code == 404

            devnet.pay(invoice["address"], 150_000)
            devnet.mine(3)
            for _ in range(90):
                status = (await http.get("/invoices/order-77")).json()
                if status["status"] == "paid":
                    break
                await asyncio.sleep(1)
            assert status["status"] == "paid" and status["confirmed_zat"] == 150_000

            for _ in range(30):
                if any(body["type"] == "payment.confirmed" for body, _ in receiver.received):
                    break
                await asyncio.sleep(0.5)
    server.close()

    types = [body["type"] for body, _ in receiver.received]
    assert "payment.confirmed" in types
    assert len(types) == len(set(types)), f"an event was delivered twice: {types}"
    assert all(sig == "valid" for _, sig in receiver.received)
    confirmed = next(body for body, _ in receiver.received if body["type"] == "payment.confirmed")
    assert confirmed["reference"] == "order-77" and confirmed["invoice_settled"] is True
