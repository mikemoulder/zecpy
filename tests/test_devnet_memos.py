"""Received memos on the local regtest chain: fetched only on request, never alone."""

import base64

import pytest

from tools.devnet import rpc
from tools.leak_audit import AuditProxy
from zecpy import PrivacyError, Wallet

pytestmark = pytest.mark.devnet

MEMO = "thanks for the coffee, order 77"
DECOYS = 3


def _display_txid(b64: str) -> str:
    return base64.b64decode(b64)[::-1].hex()


@pytest.fixture
async def proxy(tmp_path):
    audit = AuditProxy("127.0.0.1:0", "127.0.0.1:19067", log_path=tmp_path / "audit.jsonl")
    await audit.start()
    yield audit
    await audit.stop()


async def test_memo_is_fetched_among_decoys(tmp_path, devnet, proxy):
    receiver, _ = await Wallet.create(
        tmp_path / "receiver", network="regtest", server=proxy.url, regtest_heights=devnet.heights
    )
    bystander, _ = await Wallet.create(
        tmp_path / "bystander", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    # Other people's shielded payments around ours: the decoys.
    for _ in range(2):
        devnet.pay(bystander.address(), 10_000)
    devnet.pay(receiver.address(), 250_000, memo=MEMO)
    for _ in range(2):
        devnet.pay(bystander.address(), 10_000)
    devnet.mine(3)
    await receiver.sync()

    [tx] = receiver.transactions()
    [out] = tx.outputs
    assert out.memo is None and tx.memo_pending, "compact blocks carry no memo"
    assert not [r for r in proxy.records if r.method == "GetTransaction"], "sync never fetches by txid"

    report = await receiver.fetch_memos(decoys=DECOYS, max_delay=0)
    assert (report.fetched, report.waiting, report.unavailable) == (1, 0, 0), report

    tx = receiver.transaction(tx.txid)
    assert tx.outputs[0].memo == MEMO and not tx.memo_pending
    assert MEMO not in repr(tx.outputs[0])

    fetched = [_display_txid(r.request["hash"]) for r in proxy.records if r.method == "GetTransaction"]
    assert len(fetched) == DECOYS + 1 and len(set(fetched)) == DECOYS + 1, fetched
    assert tx.txid in fetched
    for decoy in set(fetched) - {tx.txid}:
        raw = rpc("getrawtransaction", [decoy, 1])
        assert not any("coinbase" in vin for vin in raw["vin"]), f"coinbase decoy {decoy}"

    # Nothing left to fetch: a second call sends nothing.
    before = len(proxy.records)
    again = await receiver.fetch_memos(decoys=DECOYS, max_delay=0)
    assert (again.fetched, again.waiting) == (0, 0) and len(proxy.records) == before


async def test_memo_fetch_refused_without_tor(tmp_path, devnet):
    wallet, _ = await Wallet.create(
        tmp_path / "w", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    remote = Wallet.open(wallet.path, server="https://lightwalletd.example:443", tor=False)
    with pytest.raises(PrivacyError, match="Tor"):
        await remote.fetch_memos()
    with pytest.raises(ValueError, match="decoys"):
        await wallet.fetch_memos(decoys=0)
