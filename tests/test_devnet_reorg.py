"""Real chain reorganisations on the regtest chain, through ZecPy's own sync loop.

A payment orphaned by a reorg is usually mined again on the new branch. zebrad's own
re-admission of orphaned transactions is unreliable (23 s once, never within 90 s another
time), so the tests rebroadcast the payer's transaction themselves, as its wallet would.
"""

import pytest

from zecpy import Wallet

pytestmark = pytest.mark.devnet


async def _confirmed_payment(tmp_path, devnet):
    merchant, _ = await Wallet.create(
        tmp_path / "merchant", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    invoice = merchant.invoices.create(250_000, "order-reorg")
    devnet.pay(invoice.address, 250_000)
    paid_at = devnet.height()
    devnet.mine(3)
    await merchant.sync()
    [confirmed] = merchant.invoices.poll(confirmations=3)
    assert (confirmed.state, confirmed.height) == ("confirmed", paid_at)
    return merchant, invoice, confirmed


async def test_reorg_to_a_shorter_branch_is_detected(tmp_path, devnet):
    """The new branch is shorter than what the wallet scanned, so no new height appears.
    Only comparing block hashes reveals the reorg."""
    merchant, invoice, confirmed = await _confirmed_payment(tmp_path, devnet)
    old_tip = devnet.height()

    devnet.invalidate(confirmed.height)
    devnet.rebroadcast(confirmed.txid)
    devnet.mine(1)  # the payment is re-mined at the same height, on a different block
    assert devnet.height() < old_tip

    await merchant.sync()
    assert merchant.balance().scanned_height == devnet.height()
    [event] = merchant.invoices.poll(confirmations=3)
    assert (event.state, event.height, event.confirmations) == ("detected", confirmed.height, 1)
    assert merchant.invoices.paid_zat(invoice) == 0, "not confirmed any more"

    devnet.mine(2)
    await merchant.sync()
    [again] = merchant.invoices.poll(confirmations=3)
    assert again.state == "confirmed"
    assert merchant.invoices.paid_zat(invoice) == 250_000


async def test_payment_dropped_by_a_reorg_reverts_then_reconfirms(tmp_path, devnet):
    """The payment is missing from the new branch for a while, then mined again higher up."""
    merchant, invoice, confirmed = await _confirmed_payment(tmp_path, devnet)

    devnet.invalidate(confirmed.height)
    devnet.mine(2)  # two blocks without it: ~5 s, well inside zebrad's re-admission delay

    await merchant.sync()
    assert merchant.balance().scanned_height == devnet.height()
    [reverted] = merchant.invoices.poll(confirmations=3)
    assert (reverted.state, reverted.height) == ("reverted", None), "zebrad re-admitted it early"
    assert merchant.invoices.paid_zat(invoice) == 0
    assert merchant.balance().spendable == 0

    devnet.rebroadcast(confirmed.txid)
    devnet.mine(3)
    await merchant.sync()
    [reconfirmed] = merchant.invoices.poll(confirmations=3)
    assert reconfirmed.state == "confirmed"
    assert reconfirmed.height == confirmed.height + 2
    assert merchant.invoices.paid_zat(invoice) == 250_000
