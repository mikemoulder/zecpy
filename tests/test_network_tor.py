"""ZecPy over Tor against the public Zcash testnet. Needs internet access; run with
`pytest -m network`."""

import pytest

from zecpy import Wallet

pytestmark = pytest.mark.network

TESTNET = "https://testnet.zec.rocks:443"


async def test_testnet_wallet_syncs_over_tor(tmp_path):
    wallet, _ = await Wallet.create(tmp_path / "w", network="test", server=TESTNET, tor=True)
    assert wallet.address().startswith("utest1")
    height = await wallet.sync()
    assert height > 4_465_026, "past NU7 activation on testnet"
    assert wallet.balance().scanned_height == height
    assert (tmp_path / "w" / "tor").is_dir(), "Tor state lives inside the wallet directory"

    reopened = Wallet.open(tmp_path / "w", server=TESTNET)  # Tor is the default
    assert await reopened.sync() >= height
