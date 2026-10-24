"""Transaction history on the local regtest chain: what it reports, and that it is local."""

import pytest

from tools.leak_audit import AuditProxy
from zecpy import Wallet

pytestmark = pytest.mark.devnet

FUNDING = 2_000_000
PAID = 300_000
MEMO = "invoice 4411: two espressos"


async def _new(tmp_path, devnet, name, server=None):
    return await Wallet.create(
        tmp_path / name, network="regtest", server=server or devnet.server, regtest_heights=devnet.heights
    )


@pytest.fixture
async def proxy(tmp_path):
    audit = AuditProxy("127.0.0.1:0", "127.0.0.1:19067", log_path=tmp_path / "audit.jsonl")
    await audit.start()
    yield audit
    await audit.stop()


async def test_history_reports_both_sides_of_a_payment(tmp_path, devnet, proxy):
    sender, _ = await _new(tmp_path, devnet, "sender", server=proxy.url)
    devnet.pay(sender.address(), FUNDING)
    devnet.mine(10)
    await sender.sync()
    receiver, _ = await _new(tmp_path, devnet, "receiver")

    [funding] = sender.transactions()
    assert (funding.status, funding.net_value, funding.fee) == ("mined", FUNDING, None)
    assert funding.confirmations == 11  # devnet.pay mines its block, then 10 more
    [incoming] = [o for o in funding.outputs if o.direction == "received"]
    assert incoming.value == FUNDING and incoming.diversifier_index is not None

    receiver_address = receiver.address()
    txid = await sender.send(receiver_address, PAID, memo=MEMO)
    pending = sender.transaction(txid)
    assert pending is not None and pending.status == "pending" and pending.mined_height is None
    assert pending.expiry_height is not None

    devnet.mine(1)
    await sender.sync()
    await receiver.sync()

    sent = sender.transaction(txid)
    assert sent.status == "mined" and sent.confirmations == 1
    assert sent.fee is not None and 0 < sent.fee <= 50_000
    assert sent.net_value == -(PAID + sent.fee)
    assert sent.memo_count == 1
    [out] = [o for o in sent.outputs if o.direction == "sent"]
    assert (out.value, out.memo) == (PAID, MEMO)
    assert out.address == receiver_address
    change = [o for o in sent.outputs if o.direction == "change"]
    assert sum(o.value for o in change) == FUNDING - PAID - sent.fee
    assert [t.txid for t in sender.transactions()] == [txid, funding.txid], "newest first"
    assert [t.txid for t in sender.transactions(since=sent.mined_height)] == [txid]

    [received] = receiver.transactions()
    assert received.txid == txid and received.net_value == PAID and received.fee is None
    # The compact block holds only the start of the note ciphertext: the memo needs the
    # full transaction, which this wallet has not fetched.
    [got] = received.outputs
    assert got.direction == "received" and got.memo is None and got.memo_bytes is None

    # Memo contents never reach a repr; their presence does.
    assert MEMO not in repr(out) and "<hidden>" in repr(out)
    assert MEMO not in repr(sent) and MEMO not in repr(sent.outputs)
    assert repr(sent) == (
        f'Transaction(txid="{txid}", status="mined", mined_height={sent.mined_height}, '
        f"net_value={sent.net_value}, fee={sent.fee})"
    )

    # Reading history, however often, sends the server nothing.
    before = len(proxy.records)
    for _ in range(3):
        sender.transactions()
        sender.transaction(txid)
    assert len(proxy.records) == before


async def test_lookup_of_unknown_or_malformed_txid(tmp_path, devnet):
    wallet, _ = await _new(tmp_path, devnet, "empty")
    assert wallet.transactions() == []
    assert wallet.transaction("00" * 32) is None
    with pytest.raises(ValueError, match="64 hex"):
        wallet.transaction("not-a-txid")
