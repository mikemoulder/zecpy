"""End-to-end wallet behaviour on the local regtest chain."""

import pytest

from zecpy import Wallet

pytestmark = pytest.mark.devnet


async def test_new_wallet_receives_a_shielded_payment(tmp_path, devnet):
    wallet, mnemonic = await Wallet.create(
        tmp_path / "w", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    assert mnemonic.reveal() not in repr(mnemonic)
    assert len(mnemonic.reveal().split()) == 24

    address = wallet.address()
    assert address.startswith("uregtest1")
    assert devnet.receivers(address) and "Transparent" not in devnet.receivers(address)

    await wallet.sync()
    assert wallet.balance().total == 0

    devnet.pay(address, 150_000)
    synced_to = await wallet.sync()
    assert synced_to == devnet.height()
    assert wallet.balance().total == 150_000

    devnet.mine(10)
    await wallet.sync()
    balance = wallet.balance()
    assert balance.spendable == 150_000
    assert balance.ironwood + balance.orchard + balance.sapling == 150_000


async def test_reopened_wallet_keeps_its_state(tmp_path, devnet):
    wallet, _ = await Wallet.create(
        tmp_path / "w", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    address = wallet.address()
    devnet.pay(address, 70_000)
    await wallet.sync()
    del wallet

    reopened = Wallet.open(tmp_path / "w", server=devnet.server)
    assert reopened.network == "regtest"
    assert reopened.balance().total == 70_000


async def test_each_address_is_fresh(tmp_path, devnet):
    wallet, _ = await Wallet.create(
        tmp_path / "w", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    assert len({wallet.address() for _ in range(5)}) == 5
