"""Invoice payments on the local regtest chain."""

import pytest

from zecpy import Wallet

pytestmark = pytest.mark.devnet


@pytest.fixture
async def merchant(tmp_path, devnet):
    wallet, _ = await Wallet.create(
        tmp_path / "merchant", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    return wallet


async def test_payment_is_matched_to_its_invoice_without_a_memo(merchant, devnet):
    paid = merchant.invoices.create(150_000, "order-77")
    unpaid = merchant.invoices.create(90_000, "order-78")
    assert paid.address != unpaid.address
    assert "Transparent" not in devnet.receivers(paid.address)

    devnet.pay(paid.address, 150_000)  # no memo
    devnet.pay(merchant.address(), 40_000)  # a payment to a non-invoice address
    await merchant.sync()

    events = merchant.invoices.poll(confirmations=3)
    assert [(e.state, e.invoice.reference, e.value_zat) for e in events] == [
        ("detected", "order-77", 150_000)
    ]
    assert not events[0].invoice_settled
    assert merchant.invoices.poll(confirmations=3) == [], "no duplicate events"

    devnet.mine(2)
    await merchant.sync()
    [confirmed] = merchant.invoices.poll(confirmations=3)
    assert confirmed.state == "confirmed"
    assert confirmed.confirmations >= 3
    assert confirmed.invoice_settled
    assert merchant.invoices.paid_zat(unpaid) == 0


async def test_restart_does_not_redeliver_payments(merchant, devnet):
    invoice = merchant.invoices.create(80_000, "order-90")
    devnet.pay(invoice.address, 80_000)
    devnet.mine(3)
    await merchant.sync()
    assert [e.state for e in merchant.invoices.poll()] == ["confirmed"]

    reopened = Wallet.open(merchant.path, server=devnet.server)
    await reopened.sync()
    assert reopened.invoices.poll() == []
    assert reopened.invoices.by_reference("order-90").address == invoice.address


async def test_partial_payments_add_up(merchant, devnet):
    invoice = merchant.invoices.create(100_000, "order-91")
    devnet.pay(invoice.address, 60_000)
    devnet.pay(invoice.address, 40_000)
    devnet.mine(3)
    await merchant.sync()
    events = merchant.invoices.poll()
    assert sorted(e.value_zat for e in events) == [40_000, 60_000]
    assert events[-1].invoice_settled
    assert merchant.invoices.paid_zat(invoice) == 100_000
