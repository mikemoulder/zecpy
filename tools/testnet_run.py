"""A recorded run of ZecPy on public testnet, end to end, with the server's view audited.

    python -m tools.testnet_run --dir ~/.zecpy-dev/testnet-run \\
        --shop-server http://127.0.0.1:19167 --log <audit proxy jsonl>

Cast:
- an offline signer (`ColdSigner`), which never touches the network;
- the shop, a watch-only ZecPy wallet that reaches the light server through the audit
  proxy (which itself reaches testnet.zec.rocks only over Tor), so every request it makes
  is recorded and classified;
- a payer, zcash-devtool's wallet: another codebase, paying like a customer would;
- an agent, a hot ZecPy wallet on ZecPy's own built-in Tor, with no proxy in its path.

Story: the payer pays an invoice with a memo; the shop sees it by address alone, confirms
it, and reads the memo among decoys; the shop refunds part of it to the agent through the
cold signer; the agent is handed a payment link, refuses it over budget, then pays it
within budget; the shop sees that payment too.

The run takes about an hour of real confirmations. Every step is recorded in
`<dir>/state.json` as it completes, so the script resumes where it stopped. Seeds live in
`<dir>/*.secret` (mode 0600) and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from zecpy import ColdSigner, PrivacyError, Wallet
from zecpy.cold import describe

TESTNET = "https://testnet.zec.rocks:443"
DEVTOOL = Path.home() / ".zecpy-dev/bin/zcash-devtool"
WALLETS = Path.home() / ".zecpy-dev/wallets"

ORDER_ZAT = 25_000_000      # 0.25 TAZ: the customer's order
REFUND_ZAT = 10_000_000     # 0.1 TAZ refunded to the agent through the cold signer
API_CALL_ZAT = 1_250_000    # 0.0125 TAZ: the link the agent is handed
LOW_BUDGET = 1_000_000      # refused: below what the link asks
BUDGET = 2_000_000          # paid


class Run:
    def __init__(self, root: Path, shop_server: str) -> None:
        self.root = root
        self.shop_server = shop_server
        self.state_path = root / "state.json"
        self.state: dict = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def done(self, step: str) -> bool:
        return step in self.state

    def record(self, step: str, **facts) -> None:
        self.state[step] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **facts}
        self.state_path.write_text(json.dumps(self.state, indent=2))
        print(f"[{step}] " + ", ".join(f"{k}={v}" for k, v in facts.items()), flush=True)

    def secret(self, name: str, value: str | None = None) -> str:
        path = self.root / f"{name}.secret"
        if value is not None:
            path.write_text(value)
            path.chmod(0o600)
        return path.read_text()


def devtool(*args: str) -> str:
    out = subprocess.run([str(DEVTOOL), "wallet", "-w", "payer", *args], cwd=WALLETS,
                         capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(f"zcash-devtool {args[0]} failed: {out.stderr[-500:]}")
    return out.stdout


async def until(label: str, check, every: float = 30.0, limit: float = 4 * 3600):
    """Polls `check` (async, returns a truthy value when done) and prints progress."""
    start = time.monotonic()
    while True:
        result = await check()
        if result:
            return result
        if time.monotonic() - start > limit:
            raise TimeoutError(label)
        print(f"  waiting: {label} ({int(time.monotonic() - start)} s)", flush=True)
        await asyncio.sleep(every)


async def main(args: argparse.Namespace) -> None:
    root = Path(args.dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    run = Run(root, args.shop_server)

    # 1. The signer is made offline; only its watch bundle goes to the shop.
    if not run.done("signer"):
        signer, mnemonic = ColdSigner.generate(network="test")
        run.secret("signer", mnemonic.reveal())
        run.record("signer", made="offline, no network")
    signer = ColdSigner(run.secret("signer"), network="test")

    # 2. The shop watches with a viewing key only, through the audit proxy.
    if not run.done("shop"):
        shop = await Wallet.watch(root / "shop", signer.watch_bundle(), server=run.shop_server,
                                  birthday=args.birthday)
        run.record("shop", can_send=shop.can_send, server="audit proxy -> Tor -> testnet.zec.rocks")
    shop = Wallet.open(root / "shop", server=run.shop_server)

    # 3. The agent: a hot wallet on ZecPy's own Tor, nothing in between.
    if not run.done("agent"):
        agent, mnemonic = await Wallet.create(root / "agent", network="test", server=TESTNET)
        run.secret("agent", mnemonic.reveal())
        run.record("agent", tor=True, server=TESTNET)
    agent = Wallet.open(root / "agent", server=TESTNET, mnemonic=run.secret("agent"))

    # 4. The order: an invoice with its own address.
    if not run.done("invoice"):
        inv = shop.invoices.create(ORDER_ZAT, "testnet-order-1")
        run.record("invoice", id=inv.id, address=inv.address, uri=inv.uri)
    invoice = shop.invoices.by_reference("testnet-order-1")

    # 5. The customer pays from another codebase, with a memo the shop does not need.
    if not run.done("customer_paid"):
        devtool("sync", "-s", "testnet.zec.rocks:443")
        out = devtool("send", "-s", "testnet.zec.rocks:443", "-i", str(WALLETS / "payer.age"),
                      "--address", invoice.address, "--value", str(ORDER_ZAT),
                      "--memo", "order 1: two coffees, table 4")
        txids = re.findall(r"\b[0-9a-f]{64}\b", out)
        if not txids:
            raise RuntimeError(f"no txid in zcash-devtool output: {out[-300:]}")
        txid = txids[-1]
        run.record("customer_paid", txid=txid, by="zcash-devtool", value_zat=ORDER_ZAT)

    # 6. The shop sees it by address, then confirms it.
    async def event(state: str):
        await shop.sync()
        for e in shop.invoices.poll(confirmations=3):
            if e.invoice.id == invoice.id and e.state == state:
                return e
        return None

    if not run.done("detected"):
        e = await until("payment detected", lambda: event("detected"))
        run.record("detected", txid=e.txid, height=e.height, pool=e.pool, value_zat=e.value_zat)
    if not run.done("confirmed"):
        e = await until("payment confirmed", lambda: event("confirmed"))
        run.record("confirmed", txid=e.txid, height=e.height, confirmations=e.confirmations,
                   settled=e.invoice_settled)

    # 7. The memo, among decoys from other people's transactions.
    if not run.done("memo"):
        async def memo():
            await shop.sync()
            report = await shop.fetch_memos(decoys=7, max_delay=10.0)
            print(f"  memo fetch: {report.fetched} fetched, {report.waiting} waiting", flush=True)
            tx = shop.transaction(run.state["customer_paid"]["txid"])
            texts = [o.memo for o in (tx.outputs if tx else []) if o.memo]
            return (report, texts[0]) if texts else None
        report, text = await until("memo with enough decoys", memo, every=60)
        run.record("memo", fetched=report.fetched, decoys_per_fetch=7, memo_length=len(text))

    # 8. A refund through the cold signer: the shop proposes and proves, the signer signs.
    if not run.done("refund"):
        async def spendable():
            await shop.sync()
            return shop.balance().spendable >= REFUND_ZAT + 10_000
        await until("order funds spendable (10 confirmations)", spendable)
        pczt = shop.propose_cold(agent.address(), REFUND_ZAT, memo="refund: one coffee")
        summary = signer.inspect(pczt)
        print(describe(summary, "test"), flush=True)
        txid = await shop.broadcast_signed(signer.sign(pczt))
        run.record("refund", txid=txid, value_zat=REFUND_ZAT, fee_zat=summary.fee,
                   change_found_by_key=sum(o.is_change for o in summary.outputs))

    # 9. The agent is handed a link: refused over budget, paid within it.
    if not run.done("link"):
        link = shop.invoices.create(API_CALL_ZAT, "agent-api-call-1")
        run.record("link", uri=link.uri)
    link = shop.invoices.by_reference("agent-api-call-1")
    if not run.done("agent_paid"):
        async def agent_funded():
            await agent.sync()
            return agent.balance().spendable >= BUDGET
        await until("refund spendable for the agent", agent_funded)
        try:
            await agent.pay(link.uri, max_zat=LOW_BUDGET)
            raise AssertionError("pay() accepted a link over budget")
        except PrivacyError as e:
            refusal = str(e)
        txid = await agent.pay(link.uri, max_zat=BUDGET)
        run.record("agent_paid", refused_with=refusal, txid=txid, max_zat=BUDGET)

    # 10. The shop sees the agent's payment arrive at the link's own address.
    if not run.done("link_confirmed"):
        async def link_event():
            await shop.sync()
            for e in shop.invoices.poll(confirmations=3):
                if e.invoice.id == link.id and e.state == "confirmed":
                    return e
            return None
        e = await until("agent payment confirmed", link_event)
        run.record("link_confirmed", txid=e.txid, height=e.height, settled=e.invoice_settled)

    print("run complete", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="python -m tools.testnet_run", description=__doc__)
    p.add_argument("--dir", required=True)
    p.add_argument("--shop-server", required=True)
    p.add_argument("--birthday", type=int, required=True, help="a recent testnet height")
    asyncio.run(main(p.parse_args()))
