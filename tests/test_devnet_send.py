"""Shielded sends on the local regtest chain, and the payments ZecPy refuses to make."""

import re

import pytest

from zecpy import PrivacyError, Wallet, WalletError

pytestmark = pytest.mark.devnet

FUNDING = 2_000_000
# Zebra's built-in regtest miner address (zebra-rpc/src/config/mining.rs): transparent only.
TRANSPARENT_REGTEST = "tmJymvcUCn1ctbghvTJpXBwHiMEB8P6wxNV"


async def _new(tmp_path, devnet, name):
    return await Wallet.create(
        tmp_path / name, network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )


@pytest.fixture
async def funded(tmp_path, devnet):
    """A wallet holding FUNDING zatoshis, all of it spendable."""
    wallet, mnemonic = await _new(tmp_path, devnet, "funded")
    devnet.pay(wallet.address(), FUNDING)
    devnet.mine(10)
    await wallet.sync()
    assert wallet.balance().spendable == FUNDING
    return wallet, mnemonic


async def test_wallet_pays_another_wallet(tmp_path, devnet, funded):
    sender, _ = funded
    receiver, _ = await _new(tmp_path, devnet, "receiver")

    txid = await sender.send(receiver.address(), 300_000, memo="invoice 4411")
    assert re.fullmatch(r"[0-9a-f]{64}", txid)

    devnet.mine(1)
    await receiver.sync()
    await sender.sync()
    assert receiver.balance().total == 300_000

    spent = FUNDING - sender.balance().total
    fee = spent - 300_000
    assert 0 < fee <= 50_000, f"unexpected fee {fee}"


async def test_transparent_recipient_is_refused_by_default(funded):
    sender, _ = funded
    with pytest.raises(PrivacyError, match="transparent"):
        await sender.send(TRANSPARENT_REGTEST, 10_000)
    assert sender.balance().total == FUNDING, "nothing may be built for a refused payment"


async def test_watch_only_wallet_cannot_send(tmp_path, devnet, funded):
    sender, _ = funded
    watch_only = Wallet.open(sender.path, server=devnet.server)
    assert not watch_only.can_send
    with pytest.raises(WalletError, match="watch-only"):
        await watch_only.send(sender.address(), 10_000)


async def test_reopened_with_mnemonic_can_send(tmp_path, devnet, funded):
    sender, mnemonic = funded
    del sender
    reopened = Wallet.open(tmp_path / "funded", server=devnet.server, mnemonic=mnemonic)
    assert reopened.can_send
    receiver, _ = await _new(tmp_path, devnet, "receiver")
    await reopened.send(receiver.address(), 50_000)
    devnet.mine(1)
    await receiver.sync()
    assert receiver.balance().total == 50_000


async def test_wrong_mnemonic_is_rejected(tmp_path, devnet, funded):
    _, other = await _new(tmp_path, devnet, "other")
    with pytest.raises(PrivacyError, match="does not belong"):
        Wallet.open(tmp_path / "funded", server=devnet.server, mnemonic=other)


async def test_bad_requests_fail_before_anything_is_built(tmp_path, devnet, funded):
    sender, _ = funded
    with pytest.raises(ValueError, match="not a valid regtest address"):
        await sender.send("u1notanaddress", 10_000)
    with pytest.raises(ValueError, match="512 bytes"):
        await sender.send(sender.address(), 10_000, memo="x" * 513)
    with pytest.raises(ValueError, match="amount"):
        await sender.send(sender.address(), 0)
    assert sender.balance().total == FUNDING


async def test_broadcast_delay_is_bounded(funded):
    sender, _ = funded
    with pytest.raises(ValueError, match="max_broadcast_delay"):
        await sender.send(sender.address(), 10_000, max_broadcast_delay=-1)
    with pytest.raises(ValueError, match="max_broadcast_delay"):
        await sender.send(sender.address(), 10_000, max_broadcast_delay=7200)
