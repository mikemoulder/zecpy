"""Batch payouts on the regtest chain: many recipients, one transaction."""

import pytest

from zecpy import ColdSigner, Payout, PrivacyError, Wallet

pytestmark = pytest.mark.devnet

FUNDING = 3_000_000
TRANSPARENT_REGTEST = "tmJymvcUCn1ctbghvTJpXBwHiMEB8P6wxNV"


async def _wallet(tmp_path, devnet, name):
    return await Wallet.create(tmp_path / name, network="regtest", server=devnet.server, regtest_heights=devnet.heights)


async def test_one_transaction_pays_everyone(tmp_path, devnet):
    payer, _ = await _wallet(tmp_path, devnet, "payer")
    devnet.pay(payer.address(), FUNDING)
    devnet.mine(10)
    await payer.sync()
    staff = [(await _wallet(tmp_path, devnet, f"staff{i}"))[0] for i in range(3)]

    txid = await payer.send_many(
        [Payout(w.address(), 100_000 * (i + 1), f"salary {i}") for i, w in enumerate(staff)]
    )
    devnet.mine(1)
    for i, w in enumerate(staff):
        await w.sync()
        assert w.balance().total == 100_000 * (i + 1)
        assert {o.txid for o in w._native.received()} == {txid}, "all paid by the same transaction"


async def test_one_bad_entry_rejects_the_batch(tmp_path, devnet):
    payer, _ = await _wallet(tmp_path, devnet, "payer")
    good = payer.address()
    with pytest.raises(PrivacyError, match="payment 1: refusing"):
        await payer.send_many([(good, 10_000), (TRANSPARENT_REGTEST, 10_000), (good, 10_000)])
    with pytest.raises(ValueError, match="payment 2: amount"):
        await payer.send_many([(good, 10_000), (good, 10_000), (good, 0)])
    with pytest.raises(ValueError, match="between 1 and 50"):
        await payer.send_many([(good, 1)] * 51)


async def test_cold_signed_payroll(tmp_path, devnet):
    signer, _ = ColdSigner.generate(network="regtest", regtest_heights=devnet.heights)
    hot = await Wallet.watch(tmp_path / "hot", signer.watch_bundle(), server=devnet.server,
                             birthday=devnet.height(), regtest_heights=devnet.heights)
    devnet.pay(hot.address(), FUNDING)
    devnet.mine(10)
    await hot.sync()
    staff = [(await _wallet(tmp_path, devnet, f"cold{i}"))[0] for i in range(3)]
    addresses = [w.address() for w in staff]

    pczt = hot.propose_cold_many([(a, 200_000) for a in addresses])
    summary = signer.inspect(pczt)
    assert sorted(o.label for o in summary.outputs if not o.is_change) == sorted(addresses)
    assert summary.paying_out == 600_000

    await hot.broadcast_signed(signer.sign(pczt))
    devnet.mine(1)
    for w in staff:
        await w.sync()
        assert w.balance().total == 200_000
