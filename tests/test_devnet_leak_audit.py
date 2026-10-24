"""What the light server learns, recorded by the leak-audit proxy on the regtest chain.

Two runs through the same proxy:
- ZecPy doing a merchant's full cycle must produce no identifying request.
- zcash-devtool, the reference wallet on the same librustzcash crates, must be caught
  leaking. That control shows the audit can see leaks, so ZecPy's clean result means
  something.
"""

import asyncio

import pytest

from tests.conftest import DEV, REPO
from tools.leak_audit import AuditProxy, Label
from zecpy import Wallet

pytestmark = pytest.mark.devnet


@pytest.fixture
async def proxy(tmp_path):
    audit = AuditProxy("127.0.0.1:0", "127.0.0.1:19067", log_path=tmp_path / "audit.jsonl")
    await audit.start()
    yield audit
    await audit.stop()


def _report(audit: AuditProxy) -> str:
    return "\n".join(f"{r.label.value:<11} {r.method:<20} {r.reason}" for r in audit.records)


async def test_merchant_cycle_reveals_nothing_identifying(tmp_path, devnet, proxy):
    merchant, _ = await Wallet.create(
        tmp_path / "merchant", network="regtest", server=proxy.url, regtest_heights=devnet.heights
    )
    invoice = merchant.invoices.create(1_500_000, "order-1")
    devnet.pay(invoice.address, 1_500_000)  # the customer's wallet talks to the server directly
    devnet.mine(10)
    await merchant.sync()
    [event] = merchant.invoices.poll(confirmations=3)
    assert event.state == "confirmed"

    customer, _ = await Wallet.create(
        tmp_path / "customer", network="regtest", server=devnet.server, regtest_heights=devnet.heights
    )
    await merchant.send(customer.address(), 200_000, memo="partial refund")
    devnet.mine(1)
    await merchant.sync()

    # A restarted process must pick up where it left off without asking the server where.
    reopened = Wallet.open(merchant.path, server=proxy.url)
    devnet.mine(2)
    await reopened.sync()
    assert reopened.balance().scanned_height == devnet.height()

    counts = proxy.summary()
    assert counts["identifying"] == 0, _report(proxy)
    assert counts["broadcast"] == 1, _report(proxy)
    tree_states = [r for r in proxy.records if r.method == "GetTreeState"]
    assert tree_states and all(r.label is Label.COARSE for r in tree_states), _report(proxy)


async def _devtool(*args: str, stdin: bytes = b"") -> None:
    proc = await asyncio.create_subprocess_exec(
        str(DEV / "bin" / "zcash-devtool"), *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(stdin), timeout=300)
    assert proc.returncode == 0, err.decode()[-2000:]


async def test_audit_catches_the_reference_wallets_leaks(tmp_path, devnet, proxy):
    server = proxy.listen  # devtool takes host:port
    wallet = str(tmp_path / "devtool")
    await _devtool(
        "wallet", "-w", wallet, "init", "--name", "control", "--identity", str(tmp_path / "id.age"),
        "-n", "regtest", "--activation-heights", str(REPO / "devnet" / "activation-heights.toml"),
        "-s", server, stdin=b"\n",
    )
    await _devtool("wallet", "-w", wallet, "sync", "-s", server)

    identifying = {r.method for r in proxy.records if r.label is Label.IDENTIFYING}
    assert "GetTreeState" in identifying, _report(proxy)  # exact birthday
    assert identifying & {"GetAddressUtxos", "GetAddressUtxosStream", "GetTaddressTxids",
                          "GetTaddressTransactions"}, _report(proxy)
