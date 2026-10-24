"""Fee preview: a proposal shows what a payment costs, and sending it sends exactly that."""

import pytest

from tools.leak_audit import AuditProxy
from zecpy import PrivacyError, Wallet, WalletError

pytestmark = pytest.mark.devnet

FUNDING = 2_000_000
PAID = 300_000
TRANSPARENT_REGTEST = "tmJymvcUCn1ctbghvTJpXBwHiMEB8P6wxNV"


@pytest.fixture
async def proxy(tmp_path):
    audit = AuditProxy("127.0.0.1:0", "127.0.0.1:19067", log_path=tmp_path / "audit.jsonl")
    await audit.start()
    yield audit
    await audit.stop()


async def _new(tmp_path, devnet, name, server=None):
    wallet, _ = await Wallet.create(
        tmp_path / name, network="regtest", server=server or devnet.server, regtest_heights=devnet.heights
    )
    return wallet


async def test_proposal_fee_is_the_fee_paid(tmp_path, devnet, proxy):
    sender = await _new(tmp_path, devnet, "sender", server=proxy.url)
    devnet.pay(sender.address(), FUNDING)
    devnet.mine(10)
    await sender.sync()
    receiver = await _new(tmp_path, devnet, "receiver")

    before = len(proxy.records)
    proposal = sender.propose(receiver.address(), PAID, memo="order 9")
    assert len(proxy.records) == before, "proposing is local"

    assert proposal.paying_out == PAID and 0 < proposal.fee <= 50_000
    assert proposal.payments == [("ironwood", PAID)]
    assert sum(v for _, v in proposal.spends) == FUNDING
    assert sum(v for _, v in proposal.change) == FUNDING - PAID - proposal.fee
    assert all(pool == "ironwood" for pool, _ in proposal.spends + proposal.change)
    assert (proposal.crossing_value, proposal.canonical_crossing) == (0, False)
    assert "order 9" not in repr(proposal)

    txid = await sender.send_proposal(proposal)
    devnet.mine(1)
    await sender.sync()
    await receiver.sync()
    assert sender.transaction(txid).fee == proposal.fee
    assert receiver.balance().total == PAID

    # Its notes are spent now: the same proposal cannot be sent twice.
    with pytest.raises(WalletError, match="changed since this proposal"):
        await sender.send_proposal(proposal)


async def test_proposal_belongs_to_its_wallet(tmp_path, devnet):
    sender = await _new(tmp_path, devnet, "a")
    devnet.pay(sender.address(), FUNDING)
    devnet.mine(10)
    await sender.sync()
    other = Wallet.open(sender.path, server=devnet.server)
    stranger = await _new(tmp_path, devnet, "b")
    proposal = sender.propose(stranger.address(), PAID)
    with pytest.raises(WalletError, match="watch-only"):
        await other.send_proposal(proposal)
    with pytest.raises(ValueError, match="different wallet"):
        await stranger.send_proposal(proposal)


async def test_proposal_applies_the_same_refusals(tmp_path, devnet):
    sender = await _new(tmp_path, devnet, "c")
    devnet.pay(sender.address(), FUNDING)
    devnet.mine(10)
    await sender.sync()
    with pytest.raises(PrivacyError, match="transparent"):
        sender.propose(TRANSPARENT_REGTEST, PAID)
    with pytest.raises(WalletError, match="cannot fund"):
        sender.propose(sender.address(), FUNDING * 10)


async def test_sweep_empties_the_wallet(tmp_path, devnet):
    sender = await _new(tmp_path, devnet, "sweeper")
    devnet.pay(sender.address(), FUNDING)
    devnet.mine(10)
    await sender.sync()
    devnet.pay(sender.address(), 50_000)  # one confirmation: not spendable yet
    await sender.sync()
    receiver = await _new(tmp_path, devnet, "sink")

    with pytest.raises(WalletError, match="cannot sweep"):
        sender.propose_sweep(receiver.address(), mode="everything")

    proposal = sender.propose_sweep(receiver.address(), memo="closing")
    assert proposal.change == [] and proposal.paying_out == FUNDING - proposal.fee
    await sender.send_proposal(proposal)
    devnet.mine(10)
    await sender.sync()
    await receiver.sync()
    assert sender.balance().total == 50_000, "the unconfirmed note is left, as asked"
    assert receiver.balance().total == FUNDING - proposal.fee

    final = sender.propose_sweep(receiver.address(), mode="everything")
    await sender.send_proposal(final)
    devnet.mine(1)
    await sender.sync()
    assert sender.balance().total == 0
    with pytest.raises(ValueError, match="mode"):
        sender.propose_sweep(receiver.address(), mode="all")
