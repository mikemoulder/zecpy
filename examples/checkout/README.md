# Shielded checkout API

A shop backend that takes shielded ZEC with no spending key on the server. One file:
[`app.py`](app.py).

```text
cold machine                          checkout server (this app)
python -m zecpy.cold bundle  ──────►  ZECPY_WATCH_BUNDLE   (viewing key only)
                                      POST /invoices        fresh shielded address + URI
customer pays                         background sync       payment.detected / confirmed
                                      webhook to the shop   HMAC-SHA256 signed, once each
python -m zecpy.cold sign    ◄──────  refunds / payouts as PCZTs (wallet.propose_cold)
```

## Run it against the local regtest chain

```bash
pip install -e '.[examples]'
devnet/up.sh "$(cat ~/.zecpy-dev/wallets/miner.addr)"

# On the "cold" side (here, the same machine): make a seed and export its watch bundle.
python -m zecpy.cold --network regtest --regtest-heights heights.json bundle > bundle.json

ZECPY_WALLET=./checkout-wallet ZECPY_WATCH_BUNDLE=bundle.json \
ZECPY_SERVER=http://127.0.0.1:19067 ZECPY_REGTEST_HEIGHTS=heights.json \
ZECPY_WEBHOOK_URL=http://127.0.0.1:8080/hook ZECPY_WEBHOOK_SECRET=change-me \
uvicorn examples.checkout.app:app --port 8000

curl -X POST localhost:8000/invoices -H 'content-type: application/json' \
     -d '{"amount_zat": 150000, "reference": "order-77"}'
curl localhost:8000/invoices/order-77
```

For testnet or mainnet, set `ZECPY_SERVER` to a public light server (for example
`https://testnet.zec.rocks:443`) and drop `ZECPY_REGTEST_HEIGHTS`. Connections go over Tor
unless `ZECPY_TOR=0`.

## Verifying a webhook

```python
import hashlib, hmac

def verify(secret: bytes, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Events: `payment.detected` (in a block, not yet confirmed), `payment.confirmed` (reached
`ZECPY_CONFIRMATIONS`), `payment.reverted` (a reorg removed it; do not ship). Each is sent
once per payment and state; the body includes `invoice_settled`.

Tested end to end in `tests/test_devnet_checkout.py`.
