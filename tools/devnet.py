"""Client for the local regtest chain (devnet/): node RPC, mining, reorgs, and the
zcash-devtool miner wallet that funds tests and the privacy report."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEV = Path(os.environ.get("ZECPY_DEV", Path.home() / ".zecpy-dev"))
ZEBRA_RPC = "http://127.0.0.1:18232"
LWD = "http://127.0.0.1:19067"


def rpc(method: str, params: list | None = None) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    req = urllib.request.Request(
        ZEBRA_RPC, body.encode(), {"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.load(resp)
    if reply.get("error"):
        raise RuntimeError(f"{method}: {reply['error']}")
    return reply["result"]


def activation_heights() -> dict[str, int]:
    raw = tomllib.loads((REPO / "devnet" / "activation-heights.toml").read_text())
    return {k: v for k, v in raw.items() if isinstance(v, int)}


@dataclass
class Devnet:
    server: str
    heights: dict[str, int]

    def height(self) -> int:
        return int(rpc("getblockcount"))

    def mine(self, n: int = 1) -> int:
        for _ in range(n):
            rpc("generate", [1])
        return self.height()

    def invalidate(self, height: int) -> None:
        """Marks the block at `height` invalid, rewinding the node's chain to just below it.
        Mining afterwards builds a competing branch: a reorg for every wallet that saw it."""
        block_hash = rpc("getblockhash", [height])
        rpc("invalidateblock", [block_hash])

    def mempool(self) -> list[str]:
        return list(rpc("getrawmempool"))

    def rebroadcast(self, txid: str) -> None:
        """Resubmits a transaction the miner wallet created, as a wallet would after a reorg."""
        import sqlite3

        db = sqlite3.connect(f"file:{DEV / 'wallets' / 'miner' / 'data.sqlite'}?mode=ro", uri=True)
        row = db.execute(
            "SELECT raw FROM transactions WHERE txid = ?", (bytes.fromhex(txid)[::-1],)
        ).fetchone()
        db.close()
        if row is None or row[0] is None:
            raise RuntimeError(f"miner wallet has no raw transaction {txid}")
        if txid not in self.mempool():
            rpc("sendrawtransaction", [row[0].hex()])

    def devtool(self, *args: str) -> str:
        """Runs zcash-devtool against the miner wallet, which funds tests."""
        cmd = [str(DEV / "bin" / "zcash-devtool"), "wallet", "-w", "miner", *args]
        done = subprocess.run(
            cmd, cwd=DEV / "wallets", capture_output=True, text=True, timeout=600
        )
        if done.returncode != 0:
            raise RuntimeError(f"zcash-devtool {' '.join(args)} failed:\n{done.stderr[-2000:]}")
        return done.stdout

    def pay(self, address: str, zatoshis: int, memo: str | None = None) -> None:
        """Sends a shielded payment from the miner wallet and mines it into a block."""
        self.devtool("sync", "-s", "127.0.0.1:19067")
        extra = ["--memo", memo] if memo else []
        self.devtool(
            "send", "-i", "miner.age", "--address", address, "--value", str(zatoshis),
            "-s", "127.0.0.1:19067", *extra,
        )
        self.mine(1)

    def receivers(self, address: str) -> list[str]:
        """Names of the receivers inside a Unified Address, as zcash-devtool decodes them."""
        done = subprocess.run(
            [str(DEV / "bin" / "zcash-devtool"), "inspect", address],
            capture_output=True, text=True, timeout=60,
        )
        out = done.stdout + done.stderr  # devtool prints the decoding to stderr
        return [
            line.strip()[2:].split(" ")[0]
            for line in out.splitlines()
            if line.startswith("   - ")
        ]
