"""Paying ZIP-321 requests: one wallet's invoice link paid by another, within a budget."""

import pytest

from zecpy import PrivacyError, Wallet

pytestmark = pytest.mark.devnet

FUNDING = 2_000_000
TRANSPARENT_REGTEST = "tmJymvcUCn1ctbghvTJpXBwHiMEB8P6wxNV"


async def _new(tmp_path, devnet, name):
    wallet, _ = await Wallet.create(
        tmp_path / name, network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    return wallet


@pytest.fixture
async def payer(tmp_path, devnet):
    wallet = await _new(tmp_path, devnet, "payer")
    devnet.pay(wallet.address(), FUNDING)
    devnet.mine(10)
    await wallet.sync()
    return wallet


async def test_agent_pays_a_merchant_invoice_link(tmp_path, devnet, payer):
    merchant = await _new(tmp_path, devnet, "merchant")
    invoice = merchant.invoices.create(125_000, "order-321")

    request = payer.parse_request(invoice.uri)
    assert request.total == 125_000
    [(address, amount, label, message, has_memo)] = request.payments
    assert (address, amount, message, has_memo) == (invoice.address, 125_000, "order-321", False)

    with pytest.raises(PrivacyError, match="max_zat"):
        await payer.pay(invoice.uri, max_zat=124_999)
    assert payer.balance().total == FUNDING, "an over-budget request builds nothing"

    txid = await payer.pay(invoice.uri, max_zat=200_000)
    devnet.mine(3)
    await merchant.sync()
    [event] = [e for e in merchant.invoices.poll(confirmations=3) if e.state == "confirmed"]
    assert event.invoice.reference == "order-321" and event.invoice_settled
    assert event.txid == txid


async def test_requests_that_must_be_refused(tmp_path, devnet, payer):
    shop = (await _new(tmp_path, devnet, "shop")).address()
    cases = {
        f"zcash:{shop}": ("no amount", ValueError),
        f"zcash:{shop}?amount=0.001&req-future=1": ("not a valid ZIP 321", ValueError),
        f"zcash:{TRANSPARENT_REGTEST}?amount=0.001": ("transparent", PrivacyError),
        "zcash:u1notanaddress?amount=1": ("not a valid ZIP 321", ValueError),
        f"zcash:?address={shop}&amount=0.001&address.1={shop}&amount.1=0.003": ("max_zat", PrivacyError),
    }
    for uri, (match, error) in cases.items():
        with pytest.raises(error, match=match):
            await payer.pay(uri, max_zat=300_000)
    assert payer.balance().total == FUNDING
