"""Fixtures for tests that run against the local regtest chain (devnet/).

Devnet tests are skipped when the chain is not running. Start it with
    devnet/up.sh "$(cat ~/.zecpy-dev/wallets/miner.addr)"
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEV = Path(os.environ.get("ZECPY_DEV", Path.home() / ".zecpy-dev"))
ZEBRA_RPC = "http://127.0.0.1:18232"
LWD = "http://127.0.0.1:19067"


def _rpc(method: str, params: list | None = None) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    req = urllib.request.Request(
        ZEBRA_RPC, body.encode(), {"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.load(resp)
    if reply.get("error"):
        raise RuntimeError(f"{method}: {reply['error']}")
    return reply["result"]


def _activation_heights() -> dict[str, int]:
    raw = tomllib.loads((REPO / "devnet" / "activation-heights.toml").read_text())
    return {k: v for k, v in raw.items() if isinstance(v, int)}


@dataclass
class Devnet:
    server: str
    heights: dict[str, int]

    def height(self) -> int:
        return int(_rpc("getblockcount"))

    def mine(self, n: int = 1) -> int:
        for _ in range(n):
            _rpc("generate", [1])
        return self.height()

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


@pytest.fixture(scope="session")
def devnet() -> Devnet:
    try:
        _rpc("getblockcount")
    except OSError:
        pytest.skip("devnet is not running (devnet/up.sh)")
    return Devnet(server=LWD, heights=_activation_heights())
