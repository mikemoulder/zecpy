"""Cold signing on the regtest chain: the online wallet never holds a spending key."""

import pytest

from zecpy import ColdSigner, PrivacyError, Wallet, WalletError

pytestmark = pytest.mark.devnet

FUNDING = 2_000_000


async def _fresh_mnemonic(tmp_path, devnet, name):
    _, mnemonic = await Wallet.create(
        tmp_path / f"seed-{name}", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    return mnemonic


@pytest.fixture
async def cold_pair(tmp_path, devnet):
    """A cold signer and a funded watch-only hot wallet for the same account."""
    signer = ColdSigner(
        await _fresh_mnemonic(tmp_path, devnet, "cold"), network="regtest", regtest_heights=devnet.heights
    )
    hot = await Wallet.watch(
        tmp_path / "hot", signer.watch_bundle().to_json(),
        server=devnet.server, birthday=devnet.height(), regtest_heights=devnet.heights,
    )
    devnet.pay(hot.address(), FUNDING)
    devnet.mine(10)
    await hot.sync()
    assert hot.balance().spendable == FUNDING
    return signer, hot


async def test_hot_wallet_cannot_spend(cold_pair, tmp_path, devnet):
    _, hot = cold_pair
    assert not hot.can_send
    with pytest.raises(WalletError, match="watch-only"):
        await hot.send(hot.address(), 10_000)


async def test_cold_signed_payment_end_to_end(cold_pair, tmp_path, devnet):
    signer, hot = cold_pair
    receiver, _ = await Wallet.create(
        tmp_path / "receiver", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )

    pay_to = receiver.address()  # each call returns a fresh address, so keep this one
    pczt = hot.propose_cold(pay_to, 300_000, memo="cold-signed payout")

    summary = signer.inspect(pczt)
    assert summary.paying_out == 300_000
    assert summary.foreign_spends == 0
    assert 0 < summary.fee <= 50_000
    payments = [o for o in summary.outputs if not o.is_change]
    assert [(o.value, o.label) for o in payments] == [(300_000, pay_to)]
    assert sum(o.value for o in summary.outputs if o.is_change) == summary.spending - 300_000 - summary.fee

    signed = signer.sign(pczt)
    txid = await hot.broadcast_signed(signed)
    devnet.mine(1)
    await receiver.sync()
    await hot.sync()
    assert receiver.balance().total == 300_000
    assert hot.balance().total == FUNDING - 300_000 - summary.fee
    assert txid in {o.txid for o in receiver._native.received()}


async def test_signer_refuses_a_pczt_for_another_seed(cold_pair, tmp_path, devnet):
    _, hot = cold_pair
    stranger = ColdSigner(
        await _fresh_mnemonic(tmp_path, devnet, "stranger"), network="regtest", regtest_heights=devnet.heights
    )
    pczt = hot.propose_cold(hot.address(), 10_000)
    assert stranger.inspect(pczt).foreign_spends > 0
    with pytest.raises(PrivacyError, match="nothing that belongs"):
        stranger.sign(pczt)


async def test_unsigned_or_tampered_pczt_is_not_broadcast(cold_pair):
    signer, hot = cold_pair
    pczt = hot.propose_cold(hot.address(), 10_000)
    with pytest.raises(WalletError, match="incomplete or invalid"):
        await hot.broadcast_signed(pczt)  # never signed
    signed = bytearray(signer.sign(pczt))
    signed[len(signed) // 2] ^= 0xFF
    with pytest.raises((ValueError, WalletError)):
        await hot.broadcast_signed(bytes(signed))
    assert hot.balance().total == FUNDING


async def test_offline_cli_shows_and_signs(cold_pair, tmp_path, devnet):
    """The operator's path: the signer CLI reads the mnemonic on stdin, prints what the
    transaction does, and writes the signed PCZT."""
    import json
    import subprocess
    import sys

    _, hot = cold_pair
    heights = tmp_path / "heights.json"
    heights.write_text(json.dumps(devnet.heights))
    cli_signer_phrase = (await _fresh_mnemonic(tmp_path, devnet, "cli")).reveal()
    bundle = ColdSigner(cli_signer_phrase, network="regtest", regtest_heights=devnet.heights).watch_bundle()
    cli_hot = await Wallet.watch(
        tmp_path / "cli-hot",
        bundle.to_json(),
        server=devnet.server, birthday=devnet.height(), regtest_heights=devnet.heights,
    )
    devnet.pay(cli_hot.address(), FUNDING)
    devnet.mine(10)
    await cli_hot.sync()

    unsigned, signed = tmp_path / "pay.pczt", tmp_path / "pay.signed.pczt"
    unsigned.write_bytes(cli_hot.propose_cold(hot.address(), 120_000))
    cli = [sys.executable, "-m", "zecpy.cold", "--network", "regtest", "--regtest-heights", str(heights)]
    done = subprocess.run(
        [*cli, "sign", str(unsigned), str(signed), "--yes"],
        input=cli_signer_phrase + "\n", capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "PAY" in done.stdout and "0.00120000 ZEC" in done.stdout
    assert cli_signer_phrase not in done.stdout + done.stderr, "the mnemonic must never be echoed"

    await cli_hot.broadcast_signed(signed.read_bytes())
    devnet.mine(1)
    await hot.sync()
    assert hot.balance().total == FUNDING + 120_000
